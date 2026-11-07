"""Fast offline tests. Real model inference is never loaded by this suite."""
from __future__ import annotations

import os
import tempfile
import uuid
import wave
from pathlib import Path

TEST_DATA = Path(tempfile.mkdtemp(prefix="ethan-tts-test-"))
os.environ["ETHAN_TTS_DATA_DIR"] = str(TEST_DATA)

import pytest
from fastapi.testclient import TestClient

import app as application
import db
from config import ARTIFACT_DIR, VOICE_DIR
from engine import change_speed, sentence_pause_seconds, split_sentences, to_pcm16, trim_edge_silence


@pytest.fixture()
def client():
    with TestClient(application.app) as test_client:
        for table in ("artifacts", "tasks", "logs"):
            with db.connect() as conn:
                conn.execute(f"DELETE FROM {table}")
        yield test_client


def test_split_sentences_preserves_punctuation():
    parts = split_sentences("你好。今天怎么样？很好！", max_len=40)
    assert parts == ["你好。", "今天怎么样？", "很好！"]
    assert all(len(part) <= 5 for part in split_sentences("一" * 12, max_len=5))


def test_edge_trimming_removes_padding_but_keeps_inner_silence():
    import numpy as np
    rate = 24000
    t = np.arange(int(rate * .2)) / rate
    tone = (.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    audio = np.concatenate([
        np.zeros(int(rate * .3), dtype=np.float32), tone,
        np.zeros(int(rate * .5), dtype=np.float32), tone,
        np.zeros(int(rate * .3), dtype=np.float32),
    ])
    trimmed = trim_edge_silence(audio, "测试。")
    assert len(trimmed) < len(audio) - int(rate * .25)
    assert len(trimmed) > len(audio) - int(rate * .4)
    # The 500 ms pause between spoken phrases must remain untouched.
    inner_start = int(rate * .30)
    inner_end = int(rate * .70)
    assert np.count_nonzero(trimmed[inner_start:inner_end]) == 0
    assert np.count_nonzero(trimmed[-int(rate * .2):]) == 0
    assert sentence_pause_seconds("短停顿，") < sentence_pause_seconds("长停顿。")
    assert len(to_pcm16(trimmed)) == len(trimmed) * 2


def test_speed_changes_tempo_and_keeps_pitch():
    """Speed must be real: ffmpeg atempo shortens audio without pitch shift."""
    import shutil
    import numpy as np
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required for tempo changes")
    rate = 24000
    t = np.arange(int(rate * 2.0)) / rate
    tone = (.4 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
    assert len(change_speed(tone, 1.0)) == len(tone)
    faster = change_speed(tone, 1.5)
    slower = change_speed(tone, 0.75)
    assert len(faster) < len(tone) * .8
    assert len(slower) > len(tone) * 1.2
    # Dominant frequency must stay near 300 Hz: pitch is preserved.
    spectrum = np.abs(np.fft.rfft(faster * np.hanning(len(faster))))
    peak = np.argmax(spectrum) * rate / len(faster)
    assert abs(peak - 300) < 25


def test_speed_is_exposed_on_the_api(client, monkeypatch):
    monkeypatch.setattr(application, "model_installed", lambda _: True)
    captured = {}

    def fake_generate(params, task_id):
        captured["speed"] = params.speed
        raise RuntimeError("stop after capturing")

    monkeypatch.setattr(application.engine, "generate_file", fake_generate)
    response = client.post("/api/tts", json={
        "text": "语速测试。", "voice_id": "preset:Vivian", "speed": 1.25,
    })
    assert response.status_code == 500  # fake engine intentionally fails
    assert captured["speed"] == 1.25
    assert client.post("/api/tts", json={
        "text": "越界。", "voice_id": "preset:Vivian", "speed": 3,
    }).status_code == 422


def test_health_voices_and_settings(client):
    assert client.get("/health").json()["ok"] is True
    assert client.get("/api/overview").json()["artifacts"] == 0
    import mlx.core as mx
    assert to_pcm16(mx.array([0.0, 1.0, -1.0])) == b"\x00\x00\xff\x7f\x01\x80"
    voices = client.get("/api/voices").json()["items"]
    assert len(voices) == 9
    assert not any(v["reference_path"] for v in voices)
    response = client.patch("/api/settings", json={"sentence_chars": 55, "autoplay": False})
    assert response.status_code == 200
    assert response.json()["sentence_chars"] == 55
    assert response.json()["autoplay"] is False
    assert client.patch("/api/settings", json={"sentence_chars": 500}).status_code == 422


def test_voice_crud_and_validation(client):
    response = client.post("/api/voices", json={
        "name": "测试设计声音", "kind": "design", "instruct": "沉稳、温柔的男声",
    })
    assert response.status_code == 201
    voice_id = response.json()["id"]
    assert client.patch(f"/api/voices/{voice_id}", json={"is_favorite": True}).json()["is_favorite"] == 1
    assert client.get("/api/voices?favorite=true").json()["items"][0]["id"] == voice_id
    assert client.delete(f"/api/voices/{voice_id}").json()["ok"] is True
    assert client.post("/api/voices", json={"name": "无描述", "kind": "design"}).status_code == 400
    assert client.delete("/api/voices/preset:Vivian").status_code == 400


def test_synthesize_with_fake_engine(client, monkeypatch):
    monkeypatch.setattr(application, "model_installed", lambda _: True)

    def fake_generate(params, task_id):
        artifact_id = uuid.uuid4().hex[:12]
        filename = f"{artifact_id}.wav"
        with wave.open(str(ARTIFACT_DIR / filename), "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
            wav.writeframes(b"\0\0" * 24000)
        return {
            "id": artifact_id, "task_id": task_id, "filename": filename, "title": "测试",
            "text": params.text, "voice_id": params.voice["id"], "voice_name": params.voice["name"],
            "model_key": params.model_key, "audio_seconds": 1.0, "generation_seconds": .5,
            "size_bytes": 48044, "sample_rate": 24000, "params": "{}", "created_at": db.now(),
        }

    monkeypatch.setattr(application.engine, "generate_file", fake_generate)
    response = client.post("/api/tts", json={"text": "测试一句话。", "voice_id": "preset:Vivian"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    artifact_id = response.headers["X-Artifact-ID"]
    assert client.get(f"/api/artifacts/{artifact_id}/audio").status_code == 200
    assert client.get("/api/tasks").json()["items"][0]["status"] == "succeeded"
    assert client.delete(f"/api/artifacts/{artifact_id}").json()["ok"] is True
    assert client.get(f"/api/artifacts/{artifact_id}/audio").status_code == 404


def test_queue_cancel_retry_and_log_filter(client, monkeypatch):
    monkeypatch.setattr(application, "model_installed", lambda _: True)
    # Keep this API test deterministic: do not let the real worker consume the
    # job between the POST and cancellation assertions.
    monkeypatch.setattr(application.engine, "enqueue", lambda _: None)
    response = client.post("/api/tasks", json={"text": "后台合成。", "voice_id": "preset:Vivian"})
    assert response.status_code == 200
    task_id = response.json()["id"]
    cancelled = client.post(f"/api/tasks/{task_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    retried = client.post(f"/api/tasks/{task_id}/retry")
    assert retried.status_code == 200
    assert retried.json()["id"] != task_id
    db.log("ERROR", "model", "测试错误")
    logs = client.get("/api/logs?level=ERROR&category=model").json()
    assert logs["total"] == 1
    assert logs["items"][0]["message"] == "测试错误"
    assert client.delete("/api/logs").json()["ok"] is True


def test_m4a_reference_upload_uses_seekable_input(client):
    """Regression: valid M4A through pipe:0 used to produce a 78-byte WAV."""
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is optional for the non-clone test suite")
    source = TEST_DATA / "reference.m4a"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
        "-i", "sine=frequency=440:duration=3.2", "-c:a", "aac", str(source),
    ], check=True)
    created = client.post("/api/voices", json={
        "name": "M4A 上传测试", "kind": "clone", "reference_text": "测试录音",
    })
    voice_id = created.json()["id"]
    with source.open("rb") as audio:
        response = client.post(
            f"/api/voices/{voice_id}/reference",
            files={"file": ("voice.m4a", audio, "audio/mp4")},
            data={"reference_text": "测试录音"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["reference_path"] is True
    with wave.open(str(VOICE_DIR / f"{voice_id.removeprefix('voice:')}.wav")) as wav:
        assert abs(wav.getnframes() / wav.getframerate() - 3.2) < .2
        assert wav.getframerate() == 24000
        assert wav.getnchannels() == 1


def test_empty_reference_is_not_ready():
    empty = VOICE_DIR / "empty-reference.wav"
    with wave.open(str(empty), "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
        wav.writeframes(b"")
    assert empty.stat().st_size < 100
    assert db.reference_ready(empty.name) is False
    # Regression: ffmpeg's non-seekable WAV header can claim audio that does
    # not exist in the file. The real byte-size check must reject it too.
    content = bytearray(empty.read_bytes())
    content[40:44] = (240000).to_bytes(4, "little")
    empty.write_bytes(content)
    with wave.open(str(empty)) as wav:
        assert wav.getnframes() >= 24000 * 3
    assert db.reference_ready(empty.name) is False


def test_path_traversal_and_missing_artifacts(client):
    assert client.get("/api/artifacts/not-found/audio").status_code == 404
    assert client.get("/api/artifacts/..%2F..%2Fetc/audio").status_code == 404
    assert client.post("/api/models/download", json={"model_key": "unknown"}).status_code == 400


def test_pronunciation_rewrites_terms_without_chains():
    from engine import apply_pronunciations
    rules = [
        {"id": "a", "term": "Qwen", "say_as": "千问", "is_enabled": 1, "match_case": 0},
        {"id": "b", "term": "千问", "say_as": "错误", "is_enabled": 1, "match_case": 0},
    ]
    spoken, used = apply_pronunciations("Qwen 和 qwen 都要读对。", rules)
    # The replacement output must not be re-scanned by the second rule.
    assert spoken == "千问 和 千问 都要读对。"
    assert used == ["a", "a"]


def test_pronunciation_respects_word_boundaries():
    from engine import apply_pronunciations
    rules = [{"id": "a", "term": "Qwen", "say_as": "千问", "is_enabled": 1, "match_case": 0}]
    assert apply_pronunciations("QwenTTS", rules)[0] == "QwenTTS"
    # Digits after the term are allowed: Qwen3 / Qwen3-TTS are the common
    # spellings and must still hit the plain "Qwen" rule.
    assert apply_pronunciations("Qwen3", rules)[0] == "千问3"
    assert apply_pronunciations("Qwen3-TTS", rules)[0] == "千问3-TTS"
    assert apply_pronunciations("用 Qwen 做语音", rules)[0] == "用 千问 做语音"


def test_pronunciation_case_sensitive_and_longest_first():
    from engine import apply_pronunciations
    rules = [
        {"id": "a", "term": "qwen", "say_as": "千问", "is_enabled": 1, "match_case": 1},
        {"id": "b", "term": "Qwen3-TTS", "say_as": "千问三语音", "is_enabled": 1, "match_case": 0},
    ]
    assert apply_pronunciations("Qwen 和 qwen", rules)[0] == "Qwen 和 千问"
    # Longest term wins even though the shorter one would also match.
    assert apply_pronunciations("Qwen3-TTS 很好", rules)[0] == "千问三语音 很好"


def test_pronunciation_disabled_rules_are_ignored():
    from engine import apply_pronunciations
    rules = [{"id": "a", "term": "Qwen", "say_as": "千问", "is_enabled": 0, "match_case": 0}]
    assert apply_pronunciations("Qwen", rules)[0] == "Qwen"


def test_pronunciation_api_crud_and_preview(client):
    created = client.post("/api/pronunciations", json={
        "term": "Qwen", "say_as": "千问", "note": "模型总读错",
    })
    assert created.status_code == 201
    pron_id = created.json()["id"]
    assert client.post("/api/pronunciations", json={
        "term": "qwen", "say_as": "重复",
    }).status_code == 400
    preview = client.post("/api/pronunciations/preview", json={"text": "Qwen3-TTS 用 Qwen"})
    assert preview.status_code == 200
    assert preview.json()["spoken"] == "千问3-TTS 用 千问"
    assert preview.json()["changed"] is True
    assert client.patch(f"/api/pronunciations/{pron_id}", json={"is_enabled": False}).json()["is_enabled"] == 0
    assert client.post("/api/pronunciations/preview", json={"text": "Qwen"}).json()["spoken"] == "Qwen"
    assert client.delete(f"/api/pronunciations/{pron_id}").json()["ok"] is True
    assert client.delete(f"/api/pronunciations/{pron_id}").status_code == 404


def test_pronunciation_case_variants_can_coexist(client):
    """A case-insensitive rule and a case-sensitive variant are not duplicates."""
    base = client.post("/api/pronunciations", json={"term": "Qwen", "say_as": "千问"})
    assert base.status_code == 201
    variant = client.post("/api/pronunciations", json={
        "term": "qwen", "say_as": "小写读法", "match_case": True,
    })
    assert variant.status_code == 201
    preview = client.post("/api/pronunciations/preview", json={"text": "Qwen 和 qwen"})
    assert preview.json()["spoken"] == "千问 和 小写读法"
    assert client.post("/api/pronunciations", json={
        "term": "QWEN", "say_as": "重复",
    }).status_code == 400


def test_streaming_applies_speed(client, monkeypatch):
    """Streaming must honour speed too; it used to be ignored entirely."""
    import shutil
    import numpy as np
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required for tempo changes")
    monkeypatch.setattr(application, "model_installed", lambda _: True)

    class FakeResult:
        def __init__(self, audio):
            self.audio = audio

    def fake_generate(model, params, sentence, stream):
        # 2 s of tone per sentence, mimicking one full segment.
        t = np.arange(24000 * 2) / 24000
        yield FakeResult((.4 * np.sin(2 * np.pi * 220 * t)).astype(np.float32))

    monkeypatch.setattr(application.engine, "_generate", fake_generate)
    monkeypatch.setattr(application.engine, "load", lambda key: object())

    def run(speed):
        response = client.post("/api/tts/stream", json={
            "text": "一句话。", "voice_id": "preset:Vivian", "speed": speed,
        })
        assert response.status_code == 200
        return len(response.content) - 44

    normal = run(1.0)
    faster = run(1.6)
    assert faster < normal * .8
