import errno
from pathlib import Path

import pytest

from giraffe import reporting
from giraffe.reporting import ReportWriteError, save_baseline_report, save_failure_summary, write_report
from tests.test_report_write_failures import tiny_report


def test_export_directory_creation_failure_reports_unsaved_evidence(tmp_path, monkeypatch):
    directory = tmp_path / "unwritable"
    mkdir = Path.mkdir

    def fail_export_directory(path, *args, **kwargs):
        if path == directory:
            raise OSError(errno.EACCES, "permission denied", str(path))
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_export_directory)
    report = tiny_report()
    with pytest.raises(ReportWriteError) as raised:
        write_report(report, directory, html=True)

    error = raised.value
    assert error.failed_file == "report.json"
    assert error.json_complete is False
    assert error.errno == errno.EACCES
    assert save_failure_summary(report, directory, error) == (
        "Report could not be saved (permission denied). Request evidence for 1 requests was lost; "
        "no summary could be saved.")
    assert not directory.exists()


def test_failed_fallback_baseline_publication_removes_empty_reservation(tmp_path, monkeypatch):
    source, _ = write_report(tiny_report(), tmp_path / "run")
    original = source.read_bytes()
    destination = tmp_path / "baselines" / "reference.json"
    replace = Path.replace
    reservations = []

    def unsupported_link(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "hard links unsupported")

    def fail_publication(path, target):
        if Path(target) == destination:
            reservations.append(destination.read_bytes())
            raise OSError(errno.ENOSPC, "disk full", str(destination))
        return replace(path, target)

    monkeypatch.setattr(reporting.os, "link", unsupported_link)
    monkeypatch.setattr(Path, "replace", fail_publication)
    with pytest.raises(OSError) as raised:
        save_baseline_report(source, destination)

    assert raised.value.errno == errno.ENOSPC
    assert reservations == [b""]
    assert not destination.exists()
    assert not list(destination.parent.glob("*.tmp"))
    assert source.read_bytes() == original
