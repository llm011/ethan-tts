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
from engine import split_sentences, to_pcm16


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
