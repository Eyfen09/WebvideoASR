from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import VideoCandidate, format_duration, is_placeholder_title


AUTH_SNAPSHOT_DEFAULTS = {
    "status": "",
    "message": "",
    "qrcode": "",
    "platform": "",
    "username": "",
}


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class WebVideoRepository:
    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path = database_path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(
            database_path, timeout=30, check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    input_url TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'parsing',
                    phase TEXT NOT NULL DEFAULT 'parsing',
                    worker_pid INTEGER NOT NULL DEFAULT 0,
                    auth_json TEXT NOT NULL DEFAULT '{}',
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    browser_action TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS media_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    identity TEXT NOT NULL UNIQUE,
                    extractor TEXT NOT NULL,
                    media_id TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL,
                    webpage_url TEXT NOT NULL,
                    source_page TEXT NOT NULL,
                    title TEXT NOT NULL,
                    author TEXT NOT NULL DEFAULT '',
                    duration_seconds INTEGER NOT NULL DEFAULT 0,
                    thumbnail_url TEXT NOT NULL DEFAULT '',
                    media_kind TEXT NOT NULL DEFAULT 'video',
                    request_headers_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'discovered',
                    progress REAL NOT NULL DEFAULT 0,
                    transcript_text TEXT NOT NULL DEFAULT '',
                    detected_language TEXT NOT NULL DEFAULT '',
                    output_path TEXT NOT NULL DEFAULT '',
                    audio_cache_path TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS task_items (
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
                    selected INTEGER NOT NULL DEFAULT 1,
                    ordinal INTEGER NOT NULL,
                    PRIMARY KEY (task_id, item_id)
                );
                CREATE INDEX IF NOT EXISTS task_items_order
                    ON task_items(task_id, ordinal);
                """
            )
            task_columns = {
                str(row[1]) for row in self.conn.execute("PRAGMA table_info(tasks)")
            }
            if "browser_action" not in task_columns:
                self.conn.execute(
                    "ALTER TABLE tasks ADD COLUMN browser_action TEXT NOT NULL DEFAULT ''"
                )
            if "phase" not in task_columns:
                self.conn.execute(
                    "ALTER TABLE tasks ADD COLUMN phase TEXT NOT NULL DEFAULT 'parsing'"
                )
            if "worker_pid" not in task_columns:
                self.conn.execute(
                    "ALTER TABLE tasks ADD COLUMN worker_pid INTEGER NOT NULL DEFAULT 0"
                )
            if "auth_json" not in task_columns:
                self.conn.execute(
                    "ALTER TABLE tasks ADD COLUMN auth_json TEXT NOT NULL DEFAULT '{}'"
                )
            self.conn.commit()

    def initialize_runtime_session(self) -> None:
        """Drop transient task state while preserving reusable media assets."""
        now = _now()
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute("DELETE FROM task_items")
                self.conn.execute("DELETE FROM tasks")
                self.conn.execute(
                    """
                    UPDATE media_items SET
                        status=CASE
                            WHEN status='completed' THEN 'completed'
                            ELSE 'discovered'
                        END,
                        progress=CASE
                            WHEN status='completed' THEN 100
                            ELSE 0
                        END,
                        last_error='',
                        completed_at=CASE
                            WHEN status='completed' THEN completed_at
                            ELSE ''
                        END,
                        updated_at=?
                    """,
                    (now,),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def create_task(self, input_url: str) -> int:
        now = _now()
        with self._lock:
            cursor = self.conn.execute(
                "INSERT INTO tasks(input_url, created_at, updated_at) VALUES (?, ?, ?)",
                (input_url, now, now),
            )
            self.conn.commit()
            return int(cursor.lastrowid)

    def latest_task(self) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM tasks ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._task_dict(row) if row else None

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
        return self._task_dict(row) if row else None

    def _task_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        task = dict(row)
        task.pop("auth_json", None)
        task["stop_requested"] = bool(task["stop_requested"])
        with self._lock:
            counts = self.conn.execute(
                """
                SELECT m.status, COUNT(*) AS count
                FROM task_items ti JOIN media_items m ON m.id=ti.item_id
                WHERE ti.task_id=? GROUP BY m.status
                """,
                (task["id"],),
            ).fetchall()
            selected = self.conn.execute(
                "SELECT COUNT(*) FROM task_items WHERE task_id=? AND selected=1",
                (task["id"],),
            ).fetchone()[0]
            total = self.conn.execute(
                "SELECT COUNT(*) FROM task_items WHERE task_id=?",
                (task["id"],),
            ).fetchone()[0]
            items_updated_at = self.conn.execute(
                """
                SELECT COALESCE(MAX(m.updated_at), '')
                FROM task_items ti JOIN media_items m ON m.id=ti.item_id
                WHERE ti.task_id=?
                """,
                (task["id"],),
            ).fetchone()[0]
        task["counts"] = {str(item["status"]): int(item["count"]) for item in counts}
        task["selected_count"] = int(selected)
        task["item_count"] = int(total)
        task["items_updated_at"] = str(items_updated_at)
        return task

    def set_task_status(self, task_id: int, status: str, error: str = "") -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE tasks SET status=?, error=?, updated_at=? WHERE id=?",
                (status, error[:2000], _now(), task_id),
            )
            self.conn.commit()

    def set_task_phase(
        self, task_id: int, phase: str, *, worker_pid: int = 0
    ) -> None:
        if phase not in {"parsing", "transcription"}:
            raise ValueError("无效的任务阶段")
        with self._lock:
            self.conn.execute(
                "UPDATE tasks SET phase=?, worker_pid=?, updated_at=? WHERE id=?",
                (phase, max(0, int(worker_pid)), _now(), task_id),
            )
            self.conn.commit()

    def clear_worker(self, task_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE tasks SET worker_pid=0, updated_at=? WHERE id=?",
                (_now(), task_id),
            )
            self.conn.commit()

    def set_auth_snapshot(self, task_id: int, snapshot: dict[str, str]) -> None:
        payload = {
            key: str(snapshot.get(key) or "")
            for key in AUTH_SNAPSHOT_DEFAULTS
        }
        with self._lock:
            self.conn.execute(
                "UPDATE tasks SET auth_json=?, updated_at=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), _now(), task_id),
            )
            self.conn.commit()

    def auth_snapshot(self, task_id: int) -> dict[str, str]:
        with self._lock:
            row = self.conn.execute(
                "SELECT auth_json FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
        if row is None:
            return dict(AUTH_SNAPSHOT_DEFAULTS)
        try:
            stored = json.loads(str(row[0] or "{}"))
        except (TypeError, ValueError):
            stored = {}
        return {
            key: str(stored.get(key) or "")
            for key in AUTH_SNAPSHOT_DEFAULTS
        }

    def request_stop(self, task_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE tasks SET stop_requested=1, updated_at=? WHERE id=?",
                (_now(), task_id),
            )
            self.conn.commit()

    def clear_stop(self, task_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE tasks SET stop_requested=0, updated_at=? WHERE id=?",
                (_now(), task_id),
            )
            self.conn.commit()

    def reset_browser_action(self, task_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE tasks SET browser_action='', updated_at=? WHERE id=?",
                (_now(), task_id),
            )
            self.conn.commit()

    def set_browser_action(self, task_id: int, action: str) -> None:
        if action not in {"continue", "skip"}:
            raise ValueError("无效的浏览器操作")
        with self._lock:
            self.conn.execute(
                "UPDATE tasks SET browser_action=?, updated_at=? WHERE id=?",
                (action, _now(), task_id),
            )
            self.conn.commit()

    def get_browser_action(self, task_id: int) -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT browser_action FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
        return str(row[0]) if row else ""

    def should_stop(self, task_id: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT stop_requested FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
        return bool(row and row[0])

    @staticmethod
    def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
        blocked = {"authorization", "cookie", "proxy-authorization"}
        return {
            str(key): str(value)
            for key, value in headers.items()
            if str(key).casefold() not in blocked
        }

    def add_candidate(self, task_id: int, candidate: VideoCandidate) -> int:
        now = _now()
        headers = json.dumps(
            self._safe_headers(candidate.request_headers), ensure_ascii=False
        )
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM media_items WHERE identity=?", (candidate.identity,)
            ).fetchone()
            if row is None:
                cursor = self.conn.execute(
                    """
                    INSERT INTO media_items(
                        identity, extractor, media_id, url, webpage_url,
                        source_page, title, author, duration_seconds,
                        thumbnail_url, media_kind, request_headers_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate.identity,
                        candidate.extractor,
                        candidate.media_id,
                        candidate.url,
                        candidate.webpage_url,
                        candidate.source_page,
                        candidate.title,
                        candidate.author,
                        candidate.duration_seconds,
                        candidate.thumbnail_url,
                        candidate.media_kind,
                        headers,
                        now,
                        now,
                    ),
                )
                item_id = int(cursor.lastrowid)
            else:
                current = dict(row)
                item_id = int(current["id"])
                current_title = str(current["title"] or "")
                candidate_title = candidate.title.strip()
                replace_title = is_placeholder_title(
                    current_title,
                    media_id=str(current["media_id"] or ""),
                    url=str(current["webpage_url"] or current["url"] or ""),
                ) and not is_placeholder_title(
                    candidate_title,
                    media_id=candidate.media_id,
                    url=candidate.webpage_url or candidate.url,
                )
                self.conn.execute(
                    """
                    UPDATE media_items SET
                        url=?, webpage_url=?, source_page=?,
                        title=?, author=?, duration_seconds=?, thumbnail_url=?,
                        media_kind=?, request_headers_json=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        candidate.url or current["url"],
                        candidate.webpage_url or current["webpage_url"],
                        candidate.source_page or current["source_page"],
                        candidate_title if replace_title else current_title,
                        candidate.author or current["author"],
                        candidate.duration_seconds or current["duration_seconds"],
                        candidate.thumbnail_url or current["thumbnail_url"],
                        candidate.media_kind or current["media_kind"],
                        headers if headers != "{}" else current["request_headers_json"],
                        now,
                        item_id,
                    ),
                )
            ordinal = self.conn.execute(
                "SELECT COUNT(*) FROM task_items WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            self.conn.execute(
                """
                INSERT OR IGNORE INTO task_items(task_id, item_id, ordinal)
                VALUES (?, ?, ?)
                """,
                (task_id, item_id, int(ordinal)),
            )
            self.conn.execute(
                "UPDATE tasks SET updated_at=? WHERE id=?", (now, task_id)
            )
            self.conn.commit()
        return item_id

    def list_items(
        self, task_id: int, *, offset: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT m.*, ti.selected, ti.ordinal
                FROM task_items ti JOIN media_items m ON m.id=ti.item_id
                WHERE ti.task_id=? ORDER BY ti.ordinal
                LIMIT ? OFFSET ?
                """,
                (task_id, limit, offset),
            ).fetchall()
        return [self._item_dict(row) for row in rows]

    def list_storage_items(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT *, 0 AS selected, 0 AS ordinal
                FROM media_items
                WHERE output_path != ''
                ORDER BY completed_at DESC, updated_at DESC, id DESC
                """
            ).fetchall()
        return [self._item_dict(row) for row in rows]

    def storage_output_records(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT id, output_path
                FROM media_items
                WHERE output_path != ''
                ORDER BY id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def selected_items(self, task_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT m.*, ti.selected, ti.ordinal
                FROM task_items ti JOIN media_items m ON m.id=ti.item_id
                WHERE ti.task_id=? AND ti.selected=1 ORDER BY ti.ordinal
                """,
                (task_id,),
            ).fetchall()
        return [self._item_dict(row) for row in rows]

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT *, 1 AS selected, 0 AS ordinal FROM media_items WHERE id=?",
                (item_id,),
            ).fetchone()
        return self._item_dict(row) if row else None

    @staticmethod
    def _item_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["selected"] = bool(item.get("selected", 1))
        item["duration_text"] = format_duration(item.get("duration_seconds"))
        item["has_artifacts"] = bool(
            str(item.get("transcript_text") or "").strip()
            or str(item.get("output_path") or "").strip()
            or str(item.get("audio_cache_path") or "").strip()
        )
        try:
            item["request_headers"] = json.loads(
                item.pop("request_headers_json", "{}")
            )
        except (TypeError, ValueError):
            item["request_headers"] = {}
        item.pop("transcript_text", None)
        return item

    def set_selected(self, task_id: int, item_id: int, selected: bool) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE task_items SET selected=? WHERE task_id=? AND item_id=?",
                (int(selected), task_id, item_id),
            )
            self.conn.commit()

    def set_all_selected(self, task_id: int, selected: bool) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE task_items SET selected=? WHERE task_id=?",
                (int(selected), task_id),
            )
            self.conn.commit()

    def update_item(
        self,
        item_id: int,
        *,
        status: str,
        progress: float | None = None,
        error: str = "",
        audio_cache_path: str | None = None,
    ) -> None:
        fields = ["status=?", "last_error=?", "updated_at=?"]
        values: list[Any] = [status, error[:2000], _now()]
        if progress is not None:
            fields.append("progress=?")
            values.append(max(0.0, min(100.0, float(progress))))
        if audio_cache_path is not None:
            fields.append("audio_cache_path=?")
            values.append(audio_cache_path)
        values.append(item_id)
        with self._lock:
            self.conn.execute(
                f"UPDATE media_items SET {', '.join(fields)} WHERE id=?", values
            )
            self.conn.commit()

    def refresh_item_media(
        self, item_id: int, candidate: VideoCandidate
    ) -> dict[str, Any] | None:
        headers = json.dumps(
            self._safe_headers(candidate.request_headers), ensure_ascii=False
        )
        with self._lock:
            self.conn.execute(
                """
                UPDATE media_items SET
                    url=?, webpage_url=?, title=?, author=?, duration_seconds=?,
                    thumbnail_url=?, media_kind=?, request_headers_json=?, updated_at=?
                WHERE id=?
                """,
                (
                    candidate.url,
                    candidate.webpage_url,
                    candidate.title,
                    candidate.author,
                    candidate.duration_seconds,
                    candidate.thumbnail_url,
                    candidate.media_kind,
                    headers,
                    _now(),
                    item_id,
                ),
            )
            self.conn.commit()
        return self.get_item(item_id)

    def store_transcript(
        self, item_id: int, text: str, language: str, output_path: Path
    ) -> None:
        now = _now()
        with self._lock:
            self.conn.execute(
                """
                UPDATE media_items SET status='completed', progress=100,
                    transcript_text=?, detected_language=?, output_path=?,
                    audio_cache_path='', last_error='', completed_at=?, updated_at=?
                WHERE id=?
                """,
                (text, language, str(output_path), now, now, item_id),
            )
            self.conn.commit()

    def transcript_record(self, item_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM media_items WHERE id=?", (item_id,)
            ).fetchone()
        return dict(row) if row else None

    def transcript_records(self, item_ids: list[int]) -> list[dict[str, Any]]:
        unique_ids = sorted({int(item_id) for item_id in item_ids if int(item_id) > 0})
        if not unique_ids:
            return []
        placeholders = ",".join("?" for _ in unique_ids)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM media_items WHERE id IN ({placeholders})",
                unique_ids,
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_item_artifacts(self, item_id: int) -> None:
        with self._lock:
            self.conn.execute(
                """
                UPDATE media_items SET
                    status='discovered', progress=0,
                    transcript_text='', detected_language='', output_path='',
                    audio_cache_path='', last_error='', completed_at='', updated_at=?
                WHERE id=?
                """,
                (_now(), item_id),
            )
            self.conn.commit()

    def delete_items(self, item_ids: list[int]) -> int:
        unique_ids = sorted({int(item_id) for item_id in item_ids if int(item_id) > 0})
        if not unique_ids:
            return 0
        placeholders = ",".join("?" for _ in unique_ids)
        with self._lock:
            cursor = self.conn.execute(
                f"DELETE FROM media_items WHERE id IN ({placeholders})",
                unique_ids,
            )
            self.conn.commit()
            return int(cursor.rowcount)

    def reset_failed(self, task_id: int) -> int:
        with self._lock:
            cursor = self.conn.execute(
                """
                UPDATE media_items SET status='discovered', progress=0,
                    last_error='', updated_at=?
                WHERE id IN (
                    SELECT item_id FROM task_items
                    WHERE task_id=? AND selected=1
                ) AND status IN ('failed', 'unsupported', 'cancelled')
                """,
                (_now(), task_id),
            )
            self.conn.commit()
            return int(cursor.rowcount)
