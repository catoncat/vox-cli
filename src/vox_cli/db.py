from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        conn.executescript(
            '''
            CREATE TABLE IF NOT EXISTS profiles (
              id TEXT PRIMARY KEY,
              name TEXT UNIQUE NOT NULL,
              language TEXT NOT NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS profile_samples (
              id TEXT PRIMARY KEY,
              profile_id TEXT NOT NULL,
              audio_path TEXT NOT NULL,
              reference_text TEXT NOT NULL,
              duration_sec REAL NOT NULL,
              rms REAL NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(profile_id) REFERENCES profiles(id)
            );

            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY,
              task_type TEXT NOT NULL,
              status TEXT NOT NULL,
              model_id TEXT,
              payload_json TEXT,
              result_json TEXT,
              error_message TEXT,
              started_at TEXT NOT NULL,
              ended_at TEXT
            );
            '''
        )


@dataclass
class TaskHandle:
    id: str
    task_type: str


def create_task(
    conn: sqlite3.Connection,
    task_type: str,
    model_id: str | None,
    payload: dict | None = None,
) -> TaskHandle:
    task_id = str(uuid.uuid4())
    conn.execute(
        '''
        INSERT INTO tasks (id, task_type, status, model_id, payload_json, started_at)
        VALUES (?, ?, 'running', ?, ?, ?)
        ''',
        (
            task_id,
            task_type,
            model_id,
            json.dumps(payload or {}, ensure_ascii=False),
            _utc_now(),
        ),
    )
    conn.commit()
    return TaskHandle(id=task_id, task_type=task_type)


def complete_task(conn: sqlite3.Connection, task_id: str, result: dict | None = None) -> None:
    conn.execute(
        '''
        UPDATE tasks
        SET status = 'completed', result_json = ?, ended_at = ?
        WHERE id = ?
        ''',
        (json.dumps(result or {}, ensure_ascii=False), _utc_now(), task_id),
    )
    conn.commit()


def fail_task(conn: sqlite3.Connection, task_id: str, error_message: str) -> None:
    conn.execute(
        '''
        UPDATE tasks
        SET status = 'failed', error_message = ?, ended_at = ?
        WHERE id = ?
        ''',
        (error_message, _utc_now(), task_id),
    )
    conn.commit()


def resolve_profile(conn: sqlite3.Connection, profile_ref: str) -> sqlite3.Row | None:
    row = conn.execute('SELECT * FROM profiles WHERE id = ? OR name = ?', (profile_ref, profile_ref)).fetchone()
    return row


def create_profile(conn: sqlite3.Connection, name: str, language: str) -> sqlite3.Row:
    profile_id = str(uuid.uuid4())
    conn.execute(
        'INSERT INTO profiles (id, name, language, created_at) VALUES (?, ?, ?, ?)',
        (profile_id, name, language, _utc_now()),
    )
    conn.commit()
    row = conn.execute('SELECT * FROM profiles WHERE id = ?', (profile_id,)).fetchone()
    assert row is not None
    return row


def list_profiles(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        '''
        SELECT p.*, COUNT(s.id) AS sample_count
        FROM profiles p
        LEFT JOIN profile_samples s ON p.id = s.profile_id
        GROUP BY p.id
        ORDER BY p.created_at DESC
        '''
    ).fetchall()


def add_profile_sample(
    conn: sqlite3.Connection,
    profile_id: str,
    audio_path: str,
    reference_text: str,
    duration_sec: float,
    rms: float,
) -> sqlite3.Row:
    sample_id = str(uuid.uuid4())
    conn.execute(
        '''
        INSERT INTO profile_samples (id, profile_id, audio_path, reference_text, duration_sec, rms, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ''',
        (sample_id, profile_id, audio_path, reference_text, duration_sec, rms, _utc_now()),
    )
    conn.commit()
    row = conn.execute('SELECT * FROM profile_samples WHERE id = ?', (sample_id,)).fetchone()
    assert row is not None
    return row


def list_profile_samples(conn: sqlite3.Connection, profile_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        'SELECT * FROM profile_samples WHERE profile_id = ? ORDER BY created_at DESC',
        (profile_id,),
    ).fetchall()


def list_tasks(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        'SELECT * FROM tasks ORDER BY started_at DESC LIMIT ?',
        (limit,),
    ).fetchall()


def get_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    return conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()


@contextmanager
def tracked_task(
    conn: sqlite3.Connection,
    task_type: str,
    model_id: str | None,
    payload: dict | None = None,
) -> Iterator[TaskHandle]:
    handle = create_task(conn, task_type, model_id, payload)
    yield handle
