"""Only allowlisted classifications cross the public or log boundaries."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

MESSAGES = {
    "stopped": "Orcaは停止中です",
    "no_models": "利用可能なモデルがありません",
    "queue_full": "待機件数の上限に達しました",
    "invalid_input": "入力内容を確認してください",
    "unavailable": "サービスを一時的に利用できません",
    "backend_error": "サービスを利用できません",
    "cancelled": "処理はキャンセルされました",
    "internal": "処理を完了できませんでした",
    "config": "設定内容を確認してください",
}

TERMINAL_MESSAGES = {
    "stopped": "Orca is stopped",
    "no_models": "No models are available",
    "queue_full": "The pending request limit has been reached",
    "invalid_input": "Check the request input",
    "unavailable": "The service is temporarily unavailable",
    "backend_error": "The service is unavailable",
    "cancelled": "The request was cancelled",
    "internal": "The operation could not be completed",
    "config": "Check the configuration",
}


class OrcaError(Exception):
    def __init__(self, code: str, http_status: int | None = None):
        self.code = code if code in MESSAGES else "internal"
        self.http_status = http_status
        super().__init__(MESSAGES[self.code])

    def public(self) -> dict:
        result = {"code": self.code, "message": MESSAGES[self.code]}
        if type(self.http_status) is int and 400 <= self.http_status <= 599:
            result["http_status"] = self.http_status
        return result

    def terminal(self) -> str:
        return TERMINAL_MESSAGES[self.code]


class BackendError(OrcaError):
    def __init__(self, temporary: bool = False, http_status: int | None = None):
        self.temporary = temporary
        super().__init__("unavailable" if temporary else "backend_error", http_status)


class CredentialError(BackendError):
    """Internal distinction; keep the same sanitized public backend error."""


class ErrorLog:
    """Never formats exception messages, traceback locals, headers or bodies."""
    def __init__(self, path: Path):
        self.path = path
        self.failed = False

    def record(self, category: str) -> None:
        if category not in {"config", "storage", "backend", "ipc", "lifecycle", "internal"}:
            category = "internal"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                         "category": category}) + "\n")
        except OSError:
            self.failed = True
