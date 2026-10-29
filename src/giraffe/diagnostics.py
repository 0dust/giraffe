"""Run-scoped tracebacks with context preserved across executor calls."""

from contextlib import contextmanager
from contextvars import ContextVar
import logging
import os
from pathlib import Path
import sys
import threading


logger = logging.getLogger(__name__)
_run_directory = ContextVar("giraffe_log_directory", default=None)
_handler_lock = threading.RLock()
_run_handlers = {}
_stderr_handler = None
_invalid_levels = set()
_FORMAT = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")


class _WarningHandler(logging.StreamHandler):
    def emit(self, record):
        if getattr(record, "giraffe_file_only", False):
            return
        self.stream = sys.stderr
        super().emit(record)


def configure_logging():
    """Keep WARNING diagnostics available throughout CLI and UI startup."""
    global _stderr_handler
    package = logging.getLogger("giraffe")
    with _handler_lock:
        if _stderr_handler is None:
            _stderr_handler = _WarningHandler(sys.stderr)
            _stderr_handler.setLevel(logging.WARNING)
            _stderr_handler.setFormatter(_FORMAT)
            package.addHandler(_stderr_handler)
        else:
            # Repeated CLI invocations may replace stderr (including capture and
            # broken-pipe wrappers); never retain a closed stream from a prior call.
            _stderr_handler.stream = sys.stderr
        configured = os.environ.get("GIRAFFE_LOG_LEVEL")
        if configured:
            try:
                package.setLevel(configured.upper())
            except ValueError:
                if configured not in _invalid_levels:
                    _invalid_levels.add(configured)
                    logger.warning("Ignoring invalid GIRAFFE_LOG_LEVEL")


class _RunFilter(logging.Filter):
    def __init__(self, directory):
        super().__init__()
        self.directory = directory

    def filter(self, record):
        return _run_directory.get() == self.directory


@contextmanager
def run_logging(directory):
    """Capture this run's warnings without capturing another run's secrets."""
    directory = Path(directory).resolve()
    package = logging.getLogger("giraffe")
    handler = None
    with _handler_lock:
        existing = _run_handlers.get(directory)
        if existing is not None:
            handler, references = existing
            _run_handlers[directory] = (handler, references + 1)
        else:
            try:
                path = directory / "giraffe.log"
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                os.close(descriptor)
                handler = logging.FileHandler(path, encoding="utf-8")
                handler.setLevel(logging.WARNING)
                handler.addFilter(_RunFilter(directory))
                handler.setFormatter(_FORMAT)
                package.addHandler(handler)
                _run_handlers[directory] = (handler, 1)
            except OSError:
                logger.exception("Could not open run log for %s", directory.name)
    token = _run_directory.set(directory)
    try:
        yield
    finally:
        _run_directory.reset(token)
        if handler is not None:
            with _handler_lock:
                _, references = _run_handlers[directory]
                if references > 1:
                    _run_handlers[directory] = (handler, references - 1)
                else:
                    del _run_handlers[directory]
                    package.removeHandler(handler)
                    handler.close()
