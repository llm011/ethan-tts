"""Small SQLite persistence layer: no external database daemon."""
from __future__ import annotations

import json
import sqlite3
import threading
import wave
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from config import DB_PATH, DEFAULT_SETTINGS, MIN_REFERENCE_SECONDS, PRESET_SPEAKERS, SAMPLE_RATE, VOICE_DIR

_write_lock = threading.RLock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, fallback: Any = None) -> Any:
    try:
        return json.loads(value) if value else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def reference_ready(reference_path: str | None) -> bool:
    """A WAV header alone is not a usable voice reference (78-byte empty file)."""
    if not reference_path:
        return False
    try:
        path = VOICE_DIR / reference_path
        # Pipe-produced WAV headers may claim a huge data length even when the
        # file contains no samples. Check the real bytes as well as the header.
        if path.stat().st_size < SAMPLE_RATE * MIN_REFERENCE_SECONDS * 2:
            return False
        with wave.open(str(path), "rb") as wav:
            return wav.getnframes() >= SAMPLE_RATE * MIN_REFERENCE_SECONDS
    except (OSError, EOFError, wave.Error):
        return False


def init_db() -> None:
    with _write_lock, connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS voices (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                speaker TEXT, language TEXT NOT NULL DEFAULT 'Chinese',
                description TEXT NOT NULL DEFAULT '', instruct TEXT,
                reference_path TEXT, reference_text TEXT,
                is_preset INTEGER NOT NULL DEFAULT 0, is_favorite INTEGER NOT NULL DEFAULT 0,
                dialect INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, text TEXT NOT NULL,
                voice_id TEXT NOT NULL, model_key TEXT NOT NULL,
                params TEXT NOT NULL DEFAULT '{}', error TEXT,
                artifact_id TEXT, created_at TEXT NOT NULL, started_at TEXT,
                finished_at TEXT, duration_seconds REAL
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY, task_id TEXT, title TEXT NOT NULL,
                filename TEXT NOT NULL UNIQUE, text TEXT NOT NULL, voice_id TEXT NOT NULL,
                voice_name TEXT NOT NULL, model_key TEXT NOT NULL,
                audio_seconds REAL NOT NULL, generation_seconds REAL NOT NULL,
                size_bytes INTEGER NOT NULL, sample_rate INTEGER NOT NULL,
                params TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_artifacts_created ON artifacts(created_at DESC);
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                level TEXT NOT NULL, category TEXT NOT NULL, message TEXT NOT NULL,
                task_id TEXT, metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(id DESC);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pronunciations (
                id TEXT PRIMARY KEY, term TEXT NOT NULL, say_as TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '', match_case INTEGER NOT NULL DEFAULT 0,
                is_enabled INTEGER NOT NULL DEFAULT 1, hits INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pronunciations_term ON pronunciations(term);
            """
        )
        stamp = now()
        for speaker, name, description, dialect, language in PRESET_SPEAKERS:
            db.execute(
                """INSERT OR IGNORE INTO voices
                (id,name,kind,speaker,language,description,is_preset,dialect,created_at,updated_at)
                VALUES (?,?,?,?,?,?,1,?,?,?)""",
                (f"preset:{speaker}", name, "custom", speaker, language, description,
                 int(dialect), stamp, stamp),
            )
            db.execute("UPDATE voices SET language=? WHERE id=?", (language, f"preset:{speaker}"))
        for key, value in DEFAULT_SETTINGS.items():
            db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, dumps(value)))
        db.execute("UPDATE tasks SET status='interrupted', error='服务重启，任务中断' WHERE status IN ('queued','running')")


# ---------------------------------------------------------------- pronunciation


def list_pronunciations(enabled_only: bool = False) -> list[dict[str, Any]]:
    clause = "WHERE is_enabled=1" if enabled_only else ""
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM pronunciations {clause} ORDER BY length(term) DESC, term"
        ).fetchall()
    return [dict(row) for row in rows]


def pronunciation(pron_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        return row_dict(conn.execute(
            "SELECT * FROM pronunciations WHERE id=?", (pron_id,),
        ).fetchone())


def create_pronunciation(term: str, say_as: str, note: str = "",
                         match_case: bool = False, is_enabled: bool = True) -> dict[str, Any]:
    import uuid
    pron_id = f"pron:{uuid.uuid4().hex[:12]}"
    stamp = now()
    with _write_lock, connect() as conn:
        conn.execute(
            """INSERT INTO pronunciations
            (id,term,say_as,note,match_case,is_enabled,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (pron_id, term.strip(), say_as.strip(), note.strip(), int(match_case),
             int(is_enabled), stamp, stamp),
        )
    return pronunciation(pron_id)


def update_pronunciation(pron_id: str, **values: Any) -> dict[str, Any] | None:
    if not values:
        return pronunciation(pron_id)
    values["updated_at"] = now()
    sql = ",".join(f"{key}=?" for key in values)
    with _write_lock, connect() as conn:
        conn.execute(f"UPDATE pronunciations SET {sql} WHERE id=?",
                     (*values.values(), pron_id))
    return pronunciation(pron_id)


def delete_pronunciation(pron_id: str) -> None:
    with _write_lock, connect() as conn:
        conn.execute("DELETE FROM pronunciations WHERE id=?", (pron_id,))


def bump_pronunciation_hits(ids: list[str]) -> None:
    if not ids:
        return
    with _write_lock, connect() as conn:
        conn.executemany(
            "UPDATE pronunciations SET hits=hits+1 WHERE id=?", [(i,) for i in ids],
        )


def log(level: str, category: str, message: str, task_id: str | None = None,
        metadata: dict | None = None) -> None:
    with _write_lock, connect() as db:
        db.execute(
            "INSERT INTO logs(created_at,level,category,message,task_id,metadata) VALUES(?,?,?,?,?,?)",
            (now(), level.upper(), category, message, task_id, dumps(metadata or {})),
        )


def get_settings() -> dict[str, Any]:
    with connect() as db:
        values = {r["key"]: loads(r["value"]) for r in db.execute("SELECT * FROM settings")}
    result = DEFAULT_SETTINGS.copy()
    result.update(values)
    return result


def update_settings(values: dict[str, Any]) -> dict[str, Any]:
    allowed = set(DEFAULT_SETTINGS)
    with _write_lock, connect() as db:
        for key, value in values.items():
            if key in allowed:
                db.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, dumps(value)),
                )
    return get_settings()


def voice(voice_id: str) -> dict | None:
    with connect() as db:
        item = row_dict(db.execute("SELECT * FROM voices WHERE id=?", (voice_id,)).fetchone())
    if item:
        item["reference_path"] = reference_ready(item["reference_path"])
    return item


def voice_for_engine(voice_id: str) -> dict | None:
    with connect() as db:
        return row_dict(db.execute("SELECT * FROM voices WHERE id=?", (voice_id,)).fetchone())


def task(task_id: str) -> dict | None:
    with connect() as db:
        item = row_dict(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
    if item:
        item["params"] = loads(item["params"], {})
    return item


def create_task(text: str, voice_id: str, model_key: str, params: dict) -> str:
    import uuid
    task_id = uuid.uuid4().hex[:12]
    with _write_lock, connect() as db:
        db.execute(
            "INSERT INTO tasks(id,status,text,voice_id,model_key,params,created_at) VALUES(?,?,?,?,?,?,?)",
            (task_id, "queued", text, voice_id, model_key, dumps(params), now()),
        )
    return task_id


def update_task(task_id: str, **values: Any) -> None:
    if not values:
        return
    if "params" in values:
        values["params"] = dumps(values["params"])
    sql = ",".join(f"{key}=?" for key in values)
    with _write_lock, connect() as db:
        db.execute(f"UPDATE tasks SET {sql} WHERE id=?", (*values.values(), task_id))


def add_artifact(data: dict[str, Any]) -> dict[str, Any]:
    with _write_lock, connect() as db:
        columns = list(data)
        db.execute(
            f"INSERT INTO artifacts({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            [data[c] for c in columns],
        )
    return data


def delete_files(paths: list[str]) -> None:
    """Delete only runtime files known by their resolved paths."""
    for path in paths:
        try:
            target = Path(path).resolve()
            if target.is_file() and target.is_relative_to(DB_PATH.parent):
                target.unlink()
        except OSError:
            pass
