"""Configuration normalization survives removal of historical source bridges."""

from copy import deepcopy

from giraffe.reporting import _comparison_manifest
from tests.test_reporting import make_report


def test_migrated_default_mode_normalization_preserves_explicit_nested_modes():
    old = make_report()
    settings = {"workloads": {"interference": {}}}
    old.manifest["effective_checks"] = {"load": {
        **deepcopy(settings), "migrated_tests": [{"settings": deepcopy(settings)}]}}
    normalized = _comparison_manifest(old.manifest)
    for settings in [normalized["effective_checks"]["load"], *[
            item["settings"] for item in normalized["effective_checks"]["load"]["migrated_tests"]]]:
        assert settings["workloads"]["interference"]["mode"] == "long_input_during_short"
    changed = deepcopy(old.manifest)
    changed["effective_checks"]["load"]["migrated_tests"][0]["settings"]["workloads"]["interference"]["mode"] = "short_during_long"
    assert (_comparison_manifest(changed)["effective_checks"]["load"]["migrated_tests"][0]
            ["settings"]["workloads"]["interference"]["mode"]) == "short_during_long"
