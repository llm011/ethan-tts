"""Voice chat: SSE turn pipeline, sentence chunking and conversation storage.

No real model is loaded here: the LLM and TTS stages are replaced with fakes,
and the ASR stage is either skipped (typed turns) or monkeypatched.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

TEST_DATA = Path(tempfile.mkdtemp(prefix="ethan-tts-chat-"))
os.environ["ETHAN_TTS_DATA_DIR"] = str(TEST_DATA)

import pytest
from fastapi.testclient import TestClient

import app as application
import chat
import db
import llm

REPLY = "这是第一句。这是第二句，稍微长一点。最后一句。"


@pytest.fixture()
def client():
    with TestClient(application.app) as test_client:
        yield test_client
    for session in db.list_chat_sessions():
        db.delete_chat_session(session["id"])


@pytest.fixture()
def fake_pipeline(monkeypatch):
    """Fake LLM + TTS so a turn runs end to end without weights."""
    def stream(config, messages, cancel=None):
        for char in REPLY:
            if cancel and cancel():
                return
            yield char

    def speak(params, sentence, cancelled=None):
        yield b"\0\0" * 240
        yield b"\0\0" * 240

    monkeypatch.setattr(llm.ChatConfig, "generate", lambda self, messages, cancel=None: stream(self, messages, cancel))
    monkeypatch.setattr(chat.engine_module, "speak_stream", speak)
    monkeypatch.setattr(chat.engine_module, "model_installed", lambda _: True)
    monkeypatch.setattr(chat.engine_module, "resolve_model_key", lambda _: "custom-0.6b-4bit")
    db.update_chat_settings({"llm_base_url": "http://fake/v1", "llm_model": "fake",
                             "tts_first_chars": 4, "tts_max_chars": 60})
    return REPLY


def read_events(response) -> list[tuple[str, dict]]:
    events = []
    name = None
    for line in response.iter_lines():
        if line.startswith("event:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and name:
            events.append((name, json.loads(line.split(":", 1)[1].strip())))
    return events


# ------------------------------------------------------------------ chunking

def test_split_ready_keeps_unfinished_tail():
    parts, tail = chat.split_ready("你好，我在。今天天气", first=True)
    assert parts == ["你好，我在。"] and tail == "今天天气"
    # A full stop always completes a clause; a short comma clause waits.
    assert chat.split_ready("好。", first=False, min_chars=6)[0] == ["好。"]
    assert chat.split_ready("这是第二句，", first=False, min_chars=4)[0] == []
    assert chat.split_ready("这是第二句，稍微长一点。", first=False, min_chars=4)[0] == ["这是第二句，稍微长一点。"]


def test_split_ready_force_cuts_runaway_sentence():
    parts, tail = chat.split_ready("这是一句没有任何标点的长句子需要被强制切分", first=True, max_chars=10)
    assert len(parts) >= 2
    assert all(len(part) <= 10 for part in parts)
    assert tail


def test_speech_text_strips_markdown_and_emoji():
    assert chat.to_speech_text("**重点**是 `code` 和 [链接](http://x.com) 😀") == "重点是 code 和 链接"
    assert chat.to_speech_text("# 标题\n- 列表项") == "标题 列表项"


# ------------------------------------------------------------------ settings

def test_model_settings_round_trip_and_key_masking(client):
    result = client.patch("/api/chat/settings", json={
        "llm_base_url": "https://api.deepseek.com/v1", "llm_model": "deepseek-chat",
        "llm_api_key": "sk-abcdefghijkl",
    }).json()
    assert result["configured"] is True
    assert result["has_api_key"] is True
    assert "llm_api_key" not in result and "sk-" not in json.dumps(result)
    # An empty key keeps the stored one; "__clear__" removes it.
    assert client.patch("/api/chat/settings", json={"llm_api_key": ""}).json()["has_api_key"] is True
    assert client.patch("/api/chat/settings", json={"llm_api_key": "__clear__"}).json()["has_api_key"] is False
    assert client.patch("/api/chat/settings", json={"llm_base_url": "not-a-url"}).status_code == 422
    assert client.patch("/api/chat/settings", json={"asr_model": "nope"}).status_code == 400


def test_bootstrap_exposes_what_the_page_needs(client):
    data = client.get("/api/chat/bootstrap").json()
    assert {"settings", "models", "voices", "presets", "default_prompt"} <= set(data)
    assert {"tts", "asr"} == set(data["models"])
    assert any(model["key"] == "qwen3-asr-0.6b-4bit" for model in data["models"]["asr"])
    assert "voice chat" not in data["settings"]  # prompt lives in its own field
    assert "system_prompt" not in data["settings"]


# ------------------------------------------------------------------ sessions

def test_session_crud(client):
    created = client.post("/api/chat/sessions", json={"title": "旅行"}).json()
    session_id = created["id"]
    db.add_chat_message(session_id, "user", "你好")
    detail = client.get(f"/api/chat/sessions/{session_id}").json()
    assert detail["title"] == "旅行" and len(detail["messages"]) == 1
    assert client.patch(f"/api/chat/sessions/{session_id}", json={"title": "改过"}).json()["title"] == "改过"
    sessions = client.get("/api/chat/sessions").json()["items"]
    assert sessions[0]["message_count"] == 1
    assert sessions[0]["preview"] == "你好"
    assert client.delete(f"/api/chat/sessions/{session_id}").json()["ok"] is True
    assert client.get(f"/api/chat/sessions/{session_id}").status_code == 404


# ------------------------------------------------------------------ a full turn

def test_turn_streams_text_then_audio(client, fake_pipeline):
    session_id = client.post("/api/chat/sessions", json={}).json()["id"]
    with client.stream("POST", "/api/chat/turn",
                       data={"session_id": session_id, "transcript": "你好"}) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        events = read_events(response)
    names = [name for name, _ in events]
    assert names[0] == "user"
    assert names.count("sentence") == 3
    assert names.count("audio") == 6          # two chunks per sentence
    assert "first_audio" in names and names[-1] == "done"
    # Sentences are spoken in order and in full.
    spoken = [data["text"] for name, data in events if name == "sentence"]
    assert spoken == ["这是第一句。", "这是第二句，稍微长一点。", "最后一句。"]
    saved = client.get(f"/api/chat/sessions/{session_id}").json()["messages"]
    assert [message["role"] for message in saved] == ["user", "assistant"]
    assert saved[1]["content"] == "这是第一句。这是第二句，稍微长一点。最后一句。"
    # The first question names the conversation.
    assert client.get(f"/api/chat/sessions/{session_id}").json()["title"] == "你好"


def test_typed_turn_skips_asr(client, fake_pipeline):
    response = client.post("/api/chat/turn", data={"transcript": "直接打字"})
    events = read_events(response)
    names = [name for name, _ in events]
    assert "asr" not in names and "status" in names


def test_unconfigured_model_is_a_clean_sse_error(client):
    db.update_chat_settings({"llm_base_url": "", "llm_model": ""})
    events = read_events(client.post("/api/chat/turn", data={"transcript": "你好"}))
    assert events[0][0] == "error"
    assert events[0][1]["action"] == "open_settings"


def test_missing_asr_model_asks_to_download(client, monkeypatch):
    db.update_chat_settings({"llm_base_url": "http://fake/v1", "llm_model": "fake"})
    monkeypatch.setattr(chat.stt, "asr_model_installed", lambda _: False)
    import io
    events = read_events(client.post("/api/chat/turn", data={"session_id": ""},
                                     files={"audio": ("turn.wav", io.BytesIO(b"\0" * 400), "audio/wav")}))
    assert events[0][0] == "error"
    assert events[0][1]["action"] == "download_model"


def test_interrupted_turn_keeps_what_was_spoken(client, fake_pipeline):
    session_id = client.post("/api/chat/sessions", json={}).json()["id"]
    seen = 0
    with client.stream("POST", "/api/chat/turn",
                       data={"session_id": session_id, "transcript": "讲故事"}) as response:
        for line in response.iter_lines():
            if line.startswith("event: audio"):
                seen += 1
                if seen == 2:
                    break  # the page pressed stop
    # The server saves the partial reply from its own thread right after the
    # disconnect, so poll instead of assuming a fixed delay.
    messages = []
    for _ in range(40):
        messages = client.get(f"/api/chat/sessions/{session_id}").json()["messages"]
        if len(messages) > 1:
            break
        time.sleep(.1)
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"].startswith("这是第一句。")


# ------------------------------------------------------------------ utilities

def test_llm_sse_parsing():
    assert llm.parse_sse_line('data: {"choices":[{"delta":{"content":"你好"}}]}') == "你好"
    assert llm.parse_sse_line("data: [DONE]") is None
    assert llm.parse_sse_line(": keep-alive") is None
    assert llm.ChatConfig(base_url="http://h:1/v1", model="m").endpoint() == "http://h:1/v1/chat/completions"
    with pytest.raises(llm.LLMError):
        list(llm.ChatConfig(base_url="", model="").generate([]))


def test_network_and_manifest(client):
    network = client.get("/api/network").json()
    assert isinstance(network["urls"], list) and "port" in network
    assert client.get("/chat.webmanifest").status_code == 200
    assert client.get("/chat").status_code == 200


def test_placeholder_base_url_is_flagged_without_false_positives():
    """占位地址要能认出来，但 localhost / 带 test- 的真实域名不能误伤。"""
    assert application.config_warning("http://fake/v1")
    assert application.config_warning("https://example.com/v1")
    for url in ("http://localhost:11434/v1",           # Ollama 默认
                "https://test-api.company.com/v1",     # 真实域名里有 test
                "https://api.deepseek.com/v1",
                "http://127.0.0.1:8787/v1",
                ""):
        assert application.config_warning(url) == "", url


def test_bootstrap_reports_config_warning(client):
    db.update_chat_settings({"llm_base_url": "http://fake/v1", "llm_model": "fake"})
    assert client.get("/api/chat/bootstrap").json()["config_warning"]
    db.update_chat_settings({"llm_base_url": "http://127.0.0.1:8787/v1", "llm_model": "real"})
    assert client.get("/api/chat/bootstrap").json()["config_warning"] == ""
