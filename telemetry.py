import time
from collections import deque
from dataclasses import dataclass
from typing import Deque


def _truncate(text: str, limit: int = 160) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def error_message(exc: BaseException) -> str:
    if hasattr(exc, "user_message"):
        try:
            message = exc.user_message()
            return _truncate(str(message))
        except Exception:
            pass
    return _truncate(str(exc) or exc.__class__.__name__)


@dataclass
class ApiStatus:
    last_ok_at: float | None = None
    last_error_at: float | None = None
    last_error_message: str | None = None

    def to_dict(self) -> dict:
        return {
            "last_ok_at": self.last_ok_at,
            "last_error_at": self.last_error_at,
            "last_error_message": self.last_error_message,
        }


@dataclass
class WebhookStatus:
    last_signal_id: str | None = None
    last_received_at: float | None = None

    def to_dict(self) -> dict:
        return {
            "last_signal_id": self.last_signal_id,
            "last_received_at": self.last_received_at,
        }


@dataclass
class Event:
    ts: float
    message: str
    level: str = "info"

    def to_dict(self) -> dict:
        return {"ts": self.ts, "message": self.message, "level": self.level}


class AppTelemetry:
    def __init__(self, max_events: int = 5) -> None:
        self.api = ApiStatus()
        self.webhook = WebhookStatus()
        self._events: Deque[Event] = deque(maxlen=max_events)

    def record_api_ok(self) -> None:
        self.api.last_ok_at = time.time()

    def record_api_error(self, message: str) -> None:
        self.api.last_error_at = time.time()
        self.api.last_error_message = _truncate(message)

    def record_webhook(self, signal_id: str) -> None:
        self.webhook.last_signal_id = signal_id
        self.webhook.last_received_at = time.time()

    def add_event(self, message: str, level: str = "info") -> None:
        self._events.append(Event(time.time(), _truncate(message), level))

    def get_events(self) -> list[dict]:
        return [event.to_dict() for event in self._events]
