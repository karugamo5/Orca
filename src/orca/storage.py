"""One database; exactly two application tables; no request/response bodies."""
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from .config import Model


def now() -> float:
    return time.time()


def timestamp(value: float | None = None) -> str:
    return datetime.fromtimestamp(now() if value is None else value, timezone.utc).isoformat()


_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    value = (int(now() * 1000) << 80) | secrets.randbits(80)
    return "".join(_ALPHABET[(value >> shift) & 31] for shift in range(125, -1, -5))


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        # Credentials stay in memory, including the identity used for change detection.
        self._identities = {}
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if version not in (0, 1) or tables - {"models", "requests"}:
            self.db.close()
            raise ValueError("unsupported schema")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS models (
                model_id TEXT PRIMARY KEY, api_model TEXT NOT NULL, base_url TEXT NOT NULL,
                keychain_reference TEXT, extra_headers TEXT NOT NULL,
                max_concurrent_requests INTEGER NOT NULL, configured_tokens_per_second REAL,
                measured_tokens_per_second REAL, measurement_at TEXT,
                enabled INTEGER NOT NULL DEFAULT 1, health_status TEXT NOT NULL DEFAULT 'unknown',
                last_checked_at TEXT, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY, source TEXT NOT NULL, status TEXT NOT NULL,
                received_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, model_id TEXT,
                input_tokens INTEGER, output_tokens INTEGER, attempt_count INTEGER NOT NULL DEFAULT 0,
                error_code TEXT, http_status INTEGER);
            CREATE INDEX IF NOT EXISTS requests_finished ON requests(finished_at);
            CREATE INDEX IF NOT EXISTS requests_status ON requests(status, received_at);
            PRAGMA user_version = 1;
        """)

    def close(self):
        self.db.close()

    def recover(self):
        with self.db:
            self.db.execute("UPDATE requests SET status='failed', finished_at=?, error_code='interrupted' WHERE finished_at IS NULL", (timestamp(),))
            self.db.execute("UPDATE models SET health_status='unknown', last_checked_at=NULL")

    def sync(self, models: list[Model]):
        old = {row["model_id"]: dict(row) for row in self.db.execute("SELECT * FROM models")}
        with self.db:
            for model in models:
                previous = old.get(model.id)
                reference = json.dumps(model.keychain) if model.keychain else None
                headers = json.dumps(dict(model.headers))
                same = previous and (previous["api_model"], previous["base_url"], previous["keychain_reference"], previous["extra_headers"]) == (model.model, model.base_url, reference, headers)
                same = same and (self._identities[model.id] == model.identity if model.id in self._identities
                                 else model.token is None)
                self.db.execute("""INSERT INTO models VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(model_id) DO UPDATE SET api_model=excluded.api_model,
                    base_url=excluded.base_url, keychain_reference=excluded.keychain_reference,
                    extra_headers=excluded.extra_headers, max_concurrent_requests=excluded.max_concurrent_requests,
                    configured_tokens_per_second=excluded.configured_tokens_per_second,
                    measured_tokens_per_second=excluded.measured_tokens_per_second,
                    measurement_at=excluded.measurement_at, health_status=excluded.health_status,
                    last_checked_at=excluded.last_checked_at, updated_at=excluded.updated_at""",
                    (model.id, model.model, model.base_url, reference, headers, model.max_concurrent_requests,
                     model.tokens_per_second, previous["measured_tokens_per_second"] if same else None,
                     previous["measurement_at"] if same else None, previous["enabled"] if previous else 1,
                     previous["health_status"] if same else "unknown", previous["last_checked_at"] if same else None, timestamp()))
            for model_id in old.keys() - {m.id for m in models}:
                self.db.execute("DELETE FROM models WHERE model_id=?", (model_id,))
        self._identities = {model.id: model.identity for model in models}

    def models(self):
        return {row["model_id"]: dict(row) for row in self.db.execute("SELECT * FROM models")}

    def health(self, model_id: str, online: bool):
        with self.db:
            self.db.execute("UPDATE models SET health_status=?, last_checked_at=?, updated_at=? WHERE model_id=?",
                            ("online" if online else "offline", timestamp(), timestamp(), model_id))

    def enabled(self, model_id: str, value: bool):
        with self.db:
            self.db.execute("UPDATE models SET enabled=?, updated_at=? WHERE model_id=?", (int(value), timestamp(), model_id))

    def measure(self, model_id: str, speed: float):
        with self.db:
            self.db.execute("UPDATE models SET measured_tokens_per_second=?, measurement_at=?, updated_at=? WHERE model_id=?", (speed, timestamp(), timestamp(), model_id))

    def accept(self, request_id: str):
        with self.db:
            self.db.execute("INSERT INTO requests(request_id,source,status,received_at) VALUES(?,'codex','pending',?)", (request_id, timestamp()))

    def start(self, request_id: str, model_id: str):
        with self.db:
            self.db.execute("UPDATE requests SET status='running', started_at=COALESCE(started_at,?), model_id=?, attempt_count=attempt_count+1 WHERE request_id=?", (timestamp(), model_id, request_id))

    def retry(self, request_id: str):
        with self.db:
            self.db.execute("UPDATE requests SET status='retry_wait' WHERE request_id=?", (request_id,))

    def finish(self, request_id: str, status: str, *, input_tokens=None, output_tokens=None, error=None):
        with self.db:
            self.db.execute("UPDATE requests SET status=?, finished_at=?, input_tokens=?, output_tokens=?, error_code=?, http_status=? WHERE request_id=? AND finished_at IS NULL",
                            (status, timestamp(), input_tokens, output_tokens, error.code if error else None, error.http_status if error else None, request_id))

    def active(self, limit: int | None = None):
        # Wall-clock adjustments must not change the acceptance order shown by clients.
        query = "SELECT * FROM requests WHERE finished_at IS NULL ORDER BY rowid"
        parameters = () if limit is None else (limit,)
        return [dict(row) for row in self.db.execute(query + (" LIMIT ?" if limit is not None else ""), parameters)]

    def active_counts(self):
        row = self.db.execute("SELECT COUNT(*), COALESCE(SUM(status != 'running'),0) FROM requests WHERE finished_at IS NULL").fetchone()
        return row[0], row[1]

    def prune(self, at: float | None = None):
        with self.db:
            self.db.execute("DELETE FROM requests WHERE finished_at IS NOT NULL AND finished_at < ?", (timestamp((now() if at is None else at) - 30 * 86400),))
