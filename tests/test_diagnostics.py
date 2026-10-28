"""Startup diagnostics remain visible while run tracebacks stay scoped."""

import io
import logging
import stat
import sys
from contextvars import Context

from giraffe.diagnostics import configure_logging, run_logging


def test_startup_installs_real_warning_stderr_handler_with_existing_handlers(monkeypatch):
    import giraffe.diagnostics as diagnostics

    package = logging.getLogger("giraffe")
    ancestor = logging.NullHandler()
    package.addHandler(ancestor)
    previous = diagnostics._stderr_handler
    previous_stream = previous.stream if previous is not None else None
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    try:
        configure_logging()
        first = diagnostics._stderr_handler
        configure_logging()
        assert diagnostics._stderr_handler is first
        package.warning("startup warning")
        package.info("not a warning")
        assert stream.getvalue().count("startup warning") == 1
        assert "not a warning" not in stream.getvalue()
    finally:
        package.removeHandler(ancestor)
        if previous is None:
            package.removeHandler(diagnostics._stderr_handler)
            diagnostics._stderr_handler.close()
            diagnostics._stderr_handler = None
        else:
            previous.stream = previous_stream


def test_run_handlers_share_resolved_directory_and_keep_unrelated_tracebacks_out(tmp_path):
    package = logging.getLogger("giraffe")
    before = [handler for handler in package.handlers if isinstance(handler, logging.FileHandler)]
    with run_logging(tmp_path):
        with run_logging(tmp_path / "."):
            files = [handler for handler in package.handlers if isinstance(handler, logging.FileHandler)]
            assert len(files) == len(before) + 1
            try:
                raise RuntimeError("scoped failure")
            except RuntimeError:
                package.exception("run traceback")
            Context().run(package.warning, "unrelated web request")
        package.warning("outer scope still alive")
    text = (tmp_path / "giraffe.log").read_text()
    assert text.count("run traceback") == 1
    assert "Traceback" in text and "RuntimeError: scoped failure" in text
    assert "outer scope still alive" in text
    assert "unrelated web request" not in text
    assert stat.S_IMODE((tmp_path / "giraffe.log").stat().st_mode) == 0o600
    assert [handler for handler in package.handlers if isinstance(handler, logging.FileHandler)] == before


def test_configure_logging_keeps_root_debug_reachable(monkeypatch, caplog):
    package = logging.getLogger('giraffe')
    monkeypatch.setattr(package, 'level', logging.NOTSET)
    with caplog.at_level(logging.DEBUG):
        configure_logging()
        package.debug('explicit root debug enabled')
    assert 'explicit root debug enabled' in caplog.text


def test_invalid_log_level_is_ignored_with_one_warning(monkeypatch, caplog):
    monkeypatch.setenv('GIRAFFE_LOG_LEVEL', 'invalid-round-three')
    configure_logging()
    configure_logging()
    messages = [record for record in caplog.records if 'GIRAFFE_LOG_LEVEL' in record.getMessage()]
    assert len(messages) == 1
    assert messages[0].levelno == logging.WARNING
