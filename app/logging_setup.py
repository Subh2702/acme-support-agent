"""Structured JSON logging. Every message and string field is PII-masked before output."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from typing import Any

_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {"message"}
_masker: Callable[[str], str] = lambda text: text  # noqa: E731 - replaced in configure_logging


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": _masker(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = _masker(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exc"] = _masker(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    global _masker  # noqa: PLW0603 - module-level hook set once at startup
    from app.guardrails.pii import mask_pii

    _masker = mask_pii
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "chromadb", "urllib3", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
