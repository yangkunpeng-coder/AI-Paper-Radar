"""Process-owned, bounded application logging with redaction before queuing."""

from __future__ import annotations

import atexit
import copy
import logging
import logging.handlers
import os
import queue
import re
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

LOGGER_NAME = "embodied_ai_radar"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3
QUEUE_SIZE = 256
MAX_RECORD_CHARS = 64 * 1024
_LOCK = Lock()
_SECRET_FIELD = re.compile(
    r"""(?ix)(\b(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|
    password|client[_-]?secret|secret|token)\b["']?\s*[:=]\s*)
    (?:"[^"\r\n]*"|'[^'\r\n]*'|(?:Bearer\s+|Basic\s+)?[^\s,;&}\]\r\n]+)"""
)
_BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
_API_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]+")
_URL_CREDENTIALS = re.compile(r"(?i)(https?://)[^/\s@]+@")


def redact(text: str) -> str:
    text = _SECRET_FIELD.sub(r"\1[REDACTED]", text)
    text = _BEARER.sub(r"\1 [REDACTED]", text)
    text = _API_KEY.sub("[REDACTED]", text)
    return _URL_CREDENTIALS.sub(r"\1[REDACTED]@", text)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Formatting exception chains can populate exc_text; leave other handlers'
        # records untouched and redact both message and formatted traceback.
        return redact(super().format(copy.copy(record)))


class _SafeFileHandler(logging.handlers.RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        # logging's default error path can dump the original record to stderr.
        if not getattr(self, "_warned", False):
            self._warned = True
            _warn_unavailable()


def _warn_unavailable() -> None:
    try:
        if sys.stderr is not None:
            sys.stderr.write("Application file logging unavailable.\n")
    except Exception:
        pass  # Logging must not prevent application startup or user operations.


class _QueueHandler(logging.handlers.QueueHandler):
    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # Do not retain raw args, exceptions or arbitrary extra fields in memory.
        text = self.format(record)
        if len(text) > MAX_RECORD_CHARS:
            text = text[:MAX_RECORD_CHARS] + " [truncated]"
        return logging.LogRecord(
            record.name,
            record.levelno,
            record.pathname,
            record.lineno,
            text,
            (),
            None,
        )

    def enqueue(self, record: logging.LogRecord) -> None:
        # Bounded diagnostic loss is preferable to blocking the UI.
        with suppress(queue.Full):
            self.queue.put_nowait(record)

    def handleError(self, record: logging.LogRecord) -> None:
        _warn_unavailable()


class _Listener(logging.handlers.QueueListener):
    def enqueue_sentinel(self) -> None:
        # Shutdown drains the bounded queue even when it is currently full.
        self.queue.put(self._sentinel)


@dataclass
class _Runtime:
    handler: _QueueHandler
    listener: _Listener
    sink: logging.Handler
    path: Path | None
    previous_level: int
    previous_propagate: bool


_runtime: _Runtime | None = None


def configure_logging() -> Path | None:
    """Initialize once per process without replacing root/third-party handlers."""
    global _runtime
    with _LOCK:
        if _runtime is not None:
            return _runtime.path
        path = None
        try:
            root = Path(
                os.environ.get("FLET_APP_STORAGE_DATA") or Path.cwd() / ".flet/storage/data"
            )
            target = root / "logs" / "application.log"
            target.parent.mkdir(parents=True, exist_ok=True)
            sink = _SafeFileHandler(
                target,
                maxBytes=MAX_BYTES,
                backupCount=BACKUP_COUNT,
                encoding="utf-8",
            )
            path = target
        except (OSError, ValueError):
            # Only a generic warning; paths and exception messages may contain secrets.
            _warn_unavailable()
            sink = logging.StreamHandler()
        sink.setFormatter(logging.Formatter("%(message)s"))
        handler = _QueueHandler(queue.Queue(maxsize=QUEUE_SIZE))
        handler.setFormatter(
            RedactingFormatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
            )
        )
        listener = _Listener(handler.queue, sink)
        logger = logging.getLogger(LOGGER_NAME)
        runtime = _Runtime(handler, listener, sink, path, logger.level, logger.propagate)
        listener.start()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        _runtime = runtime
        return path


def shutdown_logging() -> None:
    """Drain and close application-owned resources; safe to call repeatedly."""
    global _runtime
    with _LOCK:
        runtime, _runtime = _runtime, None
        if runtime is None:
            return
        logger = logging.getLogger(LOGGER_NAME)
        logger.removeHandler(runtime.handler)
        runtime.listener.stop()
        runtime.handler.close()
        runtime.sink.close()
        logger.setLevel(runtime.previous_level)
        logger.propagate = runtime.previous_propagate


atexit.register(shutdown_logging)
