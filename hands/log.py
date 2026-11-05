"""Structured logging for Hands.

Human-facing progress still goes to stdout through the ``log`` callables the agent uses. This
module is for diagnostics: ``hands --debug`` turns on DEBUG records on stderr, and
``hands --log-json FILE`` (or ``HANDS_LOG_JSON=FILE``) appends one JSON object per record, so a
session can be inspected or attached to a bug report. Secrets are never logged: values of
environment variables that look like keys are redacted by :func:`redact`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

LOGGER_NAME = "hands"
_SECRET = re.compile(r"(sk-ant-[A-Za-z0-9_\-]{6,}|sk-[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9._\-]{8,})")


def redact(text: str) -> str:
    """Replace anything that looks like an API key or bearer token with ``[redacted]``."""
    return _SECRET.sub("[redacted]", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": round(record.created, 3),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        extra = getattr(record, "data", None)
        if isinstance(extra, dict):
            out["data"] = json.loads(redact(json.dumps(extra, default=str)))
        if record.exc_info:
            out["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(out, ensure_ascii=False)


class _PlainFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def setup(debug: bool = False, json_path: str | None = None) -> logging.Logger:
    """Configure the ``hands`` logger. Safe to call more than once (handlers are replaced)."""
    root = logging.getLogger(LOGGER_NAME)
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.setLevel(logging.DEBUG)
    root.propagate = False
    err = logging.StreamHandler()
    err.setLevel(logging.DEBUG if debug else logging.WARNING)
    err.setFormatter(_PlainFormatter("[%(levelname)s %(name)s] %(message)s"))
    root.addHandler(err)
    json_path = json_path or os.environ.get("HANDS_LOG_JSON") or None
    if json_path:
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(json_path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
    return root
