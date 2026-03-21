from __future__ import annotations

from vox_cli.db import cleanup_tasks, complete_task, connect, create_task, init_db


def test_cleanup_tasks_marks_running_rows_stale(tmp_path) -> None:
    db_path = tmp_path / 'vox.db'
    init_db(db_path)

    with connect(db_path) as conn:
        handle = create_task(conn, 'asr_session_server', 'qwen-asr-0.6b-4bit', {'port': 8765})
        summary = cleanup_tasks(conn)
        row = conn.execute('SELECT status, error_message, ended_at FROM tasks WHERE id = ?', (handle.id,)).fetchone()

    assert summary == {'staled': 1, 'deleted': 0}
    assert row is not None
    assert row['status'] == 'stale'
    assert row['error_message'] == 'cleaned up stale task state'
    assert row['ended_at'] is not None


def test_cleanup_tasks_can_delete_finished_rows(tmp_path) -> None:
    db_path = tmp_path / 'vox.db'
    init_db(db_path)

    with connect(db_path) as conn:
        handle = create_task(conn, 'tts_clone', 'qwen-tts-0.6b-base-8bit')
        complete_task(conn, handle.id, {'ok': True})
        summary = cleanup_tasks(conn, stale_running=False, delete_finished=True)
        rows = conn.execute('SELECT id FROM tasks').fetchall()

    assert summary == {'staled': 0, 'deleted': 1}
    assert rows == []
