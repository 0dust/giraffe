"""Saved pre-release restart scripts are removed without arming commands."""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from giraffe.models import RunConfig, migrate_removed_restart_script
from giraffe.web import create_app
from tests.test_ux_catalogue import browser_logic


GENERIC_NOTE = "Removed restart_script settings saved by an earlier Giraffe build; this version does not use them."
SCRIPT_NOTE = "Endpoint local: restart_script (/opt/restart.sh) is no longer supported and was removed. To use cold start, set restart_command for this endpoint."


def saved_config(script=None):
    return {
        "config_version": 2,
        "targets": [{"name": "local", "url": "http://localhost/v1", "model": "test", "restart_script": script}],
        "checks": ["access"],
        "restart_script_timeout_seconds": 600,
    }


def assert_removed(config):
    assert "restart_script_timeout_seconds" not in config
    assert "restart_script" not in config["targets"][0]
    assert config["targets"][0]["restart_command"] is None


@pytest.mark.parametrize("script,note", [(None, GENERIC_NOTE), ("/opt/restart.sh", SCRIPT_NOTE)])
def test_model_removes_saved_restart_fields(script, note):
    source = saved_config(script)
    original = deepcopy(source)
    config = RunConfig.model_validate(source)
    assert_removed(config.model_dump(mode="json"))
    assert config._migration_notes == [note]
    assert type(config._migration_notes[0]) is str
    assert source == original


@pytest.mark.parametrize("path", ["/api/config/draft", "/api/config/validate", "/api/config/preview"])
@pytest.mark.parametrize("script,note", [(None, GENERIC_NOTE), ("/opt/restart.sh", SCRIPT_NOTE)])
def test_api_removes_saved_restart_fields(path, script, note, tmp_path, local_tokenizer):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8787") as client:
        response = client.post(path, json=saved_config(script))
    assert response.status_code == 200, response.text
    body = response.json()
    assert_removed(body["config"])
    assert body["migration_notes"] == [note]
    assert body["migration_limit_notes"] == []


def test_config_without_removed_fields_has_no_restart_note():
    source = saved_config()
    del source["restart_script_timeout_seconds"]
    del source["targets"][0]["restart_script"]
    assert RunConfig.model_validate(source)._migration_notes == []


@pytest.mark.parametrize("source", [None, [], "config", {}, {"targets": [None, {}]}])
def test_migration_without_removed_fields_preserves_identity(source):
    migrated, notes = migrate_removed_restart_script(source)
    assert migrated is source
    assert notes == []


@pytest.mark.parametrize("source", [
    {"restart_script_timeout_seconds": 600},
    {"targets": [{"restart_script": ""}, {"restart_script": None}]},
])
def test_migration_with_only_empty_settings_has_generic_note(source):
    original = deepcopy(source)
    migrated, notes = migrate_removed_restart_script(source)
    assert migrated is not source
    assert source == original
    assert "restart_script_timeout_seconds" not in migrated
    assert all("restart_script" not in target for target in migrated.get("targets", []))
    assert notes == [GENERIC_NOTE]


def test_migration_covers_all_targets_and_fallback_names():
    source = {"targets": [
        {"restart_script": "/one.sh"},
        {"name": 2, "restart_script": "/two.sh"},
        {"name": "local", "restart_script": "/opt/restart.sh"},
        {"restart_script": None}, None,
    ], "restart_script_timeout_seconds": 600}
    migrated, notes = migrate_removed_restart_script(source)
    assert notes == [
        "Endpoint #1: restart_script (/one.sh) is no longer supported and was removed. To use cold start, set restart_command for this endpoint.",
        "Endpoint #2: restart_script (/two.sh) is no longer supported and was removed. To use cold start, set restart_command for this endpoint.",
        SCRIPT_NOTE,
    ]
    assert "restart_script_timeout_seconds" not in migrated
    assert all("restart_script" not in target for target in migrated["targets"] if isinstance(target, dict))


def test_cli_prevalidation_keeps_restart_fields_for_model_migration():
    from giraffe.cli import migrated_configuration

    config = migrated_configuration(saved_config("/opt/restart.sh"))
    assert_removed(config.model_dump(mode="json"))
    assert config._migration_notes == [SCRIPT_NOTE]


def test_rerun_normalizes_v2_saved_restart_fields():
    browser_logic(r'''
renderRequestPreview=()=>{};
const saved=structuredClone(config);saved.config_version=2;
saved.targets[0].restart_script=null;saved.restart_script_timeout_seconds=600;
const note='Removed restart_script settings saved by an earlier Giraffe build; this version does not use them.';
const warning={msg:'Saved warning',removed_check:null};
state.detail={report:{manifest:{config:saved,config_migration_warnings:[warning]}},baseline_id:null};
let paths=[];
api=async(path,options)=>{
 paths.push(path);const normalized=JSON.parse(options.body);
 delete normalized.restart_script_timeout_seconds;delete normalized.targets[0].restart_script;
 return {config:normalized,migration_notes:[note],migration_warnings:[]};
};
await rerun();assert.deepEqual(paths,['/api/config/draft']);
assert.equal(state.config.targets[0].restart_script,undefined);
assert.equal(state.config.restart_script_timeout_seconds,undefined);
assert.equal(state.config.targets[0].restart_command,null);
assert.deepEqual(state.migrationNotes,[note]);assert.deepEqual(state.migrationWarnings,[warning]);
assert.equal(location.hash,'#new');
''')


def test_rerun_explains_unsupported_fields_and_stays_on_run():
    browser_logic(r'''
location.hash='#runs/saved';state.detail={report:{manifest:{config}},baseline_id:null};
let message;toast=text=>{message=text;};
api=async()=>{const error=new Error('Extra inputs are not permitted');
 error.errors=[{type:'extra_forbidden',field:'targets.0.foo',msg:error.message},
 {type:'extra_forbidden',field:'targets.0.foo',msg:error.message}];throw error;};
await rerun();
assert.equal(message,'This run was saved by an earlier Giraffe version with settings this version no longer supports: targets.0.foo. Start a new run from Setup instead.');
assert.equal(location.hash,'#runs/saved');
''')


def test_rerun_preserves_route_guard():
    browser_logic(r'''
location.hash='#runs/saved';state.detail={config};const original=state.config;
api=async()=>{state.routeVersion++;location.hash='#runs/other';return {config,migration_notes:['Old run']};};
await rerun();assert.equal(location.hash,'#runs/other');assert.equal(state.config,original);
assert.deepEqual(state.migrationNotes,[]);
''')


def test_rerun_keeps_other_error_message_and_stays_on_run():
    browser_logic(r'''
location.hash='#runs/saved';state.detail={config};let message;toast=text=>{message=text;};
api=async()=>{throw new Error('Draft request failed');};
await rerun();assert.equal(message,'Draft request failed');assert.equal(location.hash,'#runs/saved');
''')
