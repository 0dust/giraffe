"""Launch notes and traffic headlines preserve warning and count semantics."""

from tests.test_ux_catalogue import browser_logic


def test_migration_notes_use_neutral_status_and_keep_warning_styles():
    browser_logic(r'''
state.migrationNotes=['Earlier Giraffe versions <example>'];
const markup=renderMigrationNotes();
assert.match(markup,/<div class="notice migration-notes" role="status">/);
assert.match(markup,/<strong>Imported from an earlier Giraffe version<\/strong>/);
assert.match(markup,/&lt;example&gt;/);
assert.doesNotMatch(markup,/notice warning/);
assert.match(renderSettingIssues([{msg:'Real warning',fields:['concurrency']}],'warning'),/notice warning setting-issue/);
state.dismissedMigrationNotes=[...state.migrationNotes];
assert.equal(renderMigrationNotes(),'');
''')


def test_unknown_request_headline_and_other_count_labels():
    browser_logic(r'''
for(const count_kind of ['bounded','exact','estimate']){
 assert.equal(setupPlanCount({request_count_label:'Unknown',count_kind}),'Requests depend on response time');
}
for(const [plan,expected] of [
 [{request_count_label:'1',count_kind:'exact',planned_total:1},'1 request planned'],
 [{request_count_label:'20',count_kind:'exact',planned_total:20},'20 requests planned'],
 [{request_count_label:'≈20',count_kind:'estimate'},'≈20 requests'],
 [{request_count_label:'Unknown + 20',count_kind:'bounded'},'20 known requests + adaptive checks'],
 [{request_count_label:'Unknown + ≈20',count_kind:'bounded'},'≈20 expected requests + adaptive checks'],
 [{count_kind:'exact',planned_total:1},'1 request planned'],
 [{count_kind:'estimate',planned_total:20},'About 20 requests'],
 [{count_kind:'bounded',known_total:20},'20 known requests + adaptive checks'],
])assert.equal(setupPlanCount(plan),expected);
''')


def legacy_removed_load():
    return {'targets': [{'name': 'local', 'url': 'http://localhost/v1', 'model': 'test'}],
            'checks': ['access', 'buckets', 'concurrency'],
            'concurrent_workloads': {}, 'max_duration_seconds': 1}


REMOVED_LOAD = ("Load test won't run: the imported Maximum duration (1 s) can't fit its sending windows. "
                "Raise Maximum duration, then select Load test again. Only Endpoint response will run.")


async def test_removed_load_warning_survives_all_import_and_bootstrap_paths(tmp_path):
    import httpx
    from giraffe.cli import migrated_configuration
    from giraffe.web import create_app
    expected = [{'msg': REMOVED_LOAD, 'removed_check': 'load'}]
    app = create_app(tmp_path, defaults=legacy_removed_load())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
        for path in ['/api/config/draft', '/api/config/validate', '/api/config/preview', '/api/config/plan']:
            body = (await client.post(path, json=legacy_removed_load())).json()
            assert body['config']['checks'] == ['access']
            assert body['migration_warnings'] == expected
            assert all('won\'t run' not in note and 'unselected' not in note for note in body['migration_notes'])
        assert (await client.get('/api/bootstrap')).json()['migration_warnings'] == expected
    resolved = migrated_configuration(legacy_removed_load())
    app = create_app(tmp_path, defaults=resolved.model_dump(mode='json'),
                     migration_notes=resolved._migration_notes, migration_warnings=resolved._migration_warnings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
        body = (await client.get('/api/bootstrap')).json()
        assert body['migration_warnings'] == expected
    import json
    browser_logic('''
const imported=IMPORT;
state.config=imported.config;
acceptMigrationNotes(imported.migration_notes,imported.migration_warnings);
assert.equal(activeMigrationWarnings().length,1);
let launch=renderRunBlueprint(state.config);
assert.equal((launch.match(/notice warning setting-issue/g)||[]).length,1);
assert(launch.includes(escape(imported.migration_warnings[0].msg)));
assert(!renderMigrationNotes().includes('sending windows'));
state.dismissedMigrationNotes=[...state.migrationNotes];acceptMigrationNotes();
assert.equal(renderMigrationNotes(),'');
assert.equal(activeMigrationWarnings().length,1);
assert.match(renderRunBlueprint(state.config),/notice warning setting-issue/);
state.config.checks.push('load');
assert.equal(renderMigrationWarnings(),'');
assert.equal(activeMigrationWarnings().length,0);
// Partial-workload warnings are independent of check-removal warnings.
acceptMigrationNotes([], [{msg:'Some combinations will not run <example>',removed_check:null}]);
assert.match(renderMigrationWarnings(),/will not run &lt;example&gt;/);
'''.replace('IMPORT', json.dumps(body)), config=resolved)


def test_cli_preview_prints_removal_as_warning(tmp_path, capsys):
    import json
    from giraffe.cli import main
    source = tmp_path / 'legacy.json'
    source.write_text(json.dumps(legacy_removed_load()))
    assert main(['preview', '--config', str(source)]) == 0
    stderr = capsys.readouterr().err
    assert 'Warning: ' + REMOVED_LOAD in stderr
    assert 'Migration: ' + REMOVED_LOAD not in stderr
    assert 'Migration: Earlier Giraffe versions' in stderr


def test_routine_defaults_remain_one_neutral_note():
    import json
    from giraffe.cli import migrated_configuration
    config = migrated_configuration({'targets': legacy_removed_load()['targets'], 'checks': ['access']})
    assert config._migration_warnings == []
    assert len(config._migration_notes) == 1
    browser_logic('''
acceptMigrationNotes(NOTES, []);
assert.equal(renderMigrationWarnings(),'');
assert.match(renderMigrationNotes(),/notice migration-notes/);
assert.equal((renderMigrationNotes().match(/<li>/g)||[]).length,1);
'''.replace('NOTES', json.dumps(config._migration_notes)), config=config)


async def test_start_keeps_warning_metadata_in_report(tmp_path, monkeypatch):
    import httpx
    from giraffe.cli import migrated_configuration
    from giraffe.web import create_app
    from tests.test_web import report, finished
    config = migrated_configuration(legacy_removed_load())
    async def run(config, **kwargs):
        saved = report(config)
        saved.manifest['config_migration_warnings'] = config._migration_warnings
        return saved
    monkeypatch.setattr('giraffe.web.run_suite', run)
    app = create_app(tmp_path)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
        response = await client.post('/api/runs', json={'config': config.model_dump(mode='json'),
            'migration_notes': config._migration_notes, 'migration_warnings': config._migration_warnings})
        assert response.status_code == 202, response.text
        assert response.json()['migration_warnings'] == config._migration_warnings
        detail = await finished(client, response.json()['id'])
        assert detail['report']['manifest']['config_migration_warnings'] == config._migration_warnings


def test_browser_preserves_warnings_through_normalization_draft_and_start():
    import json
    from giraffe.cli import migrated_configuration
    config = migrated_configuration(legacy_removed_load())
    browser_logic(r'''
const warning=WARNING;
state.jsonDraft=state.jsonBase='synced';state.setupStep=3;state.routeVersion=2;
const form={elements:{baseline_id:{value:''},mode:{value:'normal'},restart_target:{value:''}}};nodes['#new-run-form']=form;
for(const id of ['form-error','start-run','request-plan'])nodes['#'+id]={innerHTML:'',hidden:false};
readForm=()=>({config:structuredClone(state.config),mode:'normal'});validateRunForm=()=>true;setActive=()=>{};
let first=true,sent;
api=async(path,options)=>{
 if(path==='/api/runs'){sent=JSON.parse(options.body);return {id:'new-run'};}
 const warnings=first?[warning]:[];first=false;
 return {config:JSON.parse(options.body),migration_notes:[],migration_warnings:warnings,request_plan:{rows:[],errors:[]}};
};
await refreshRequestPreview(undefined,{draft:true});
await refreshRequestPreview();await validatedSnapshot();
assert.deepEqual(activeMigrationWarnings(),[warning]);
saveDraft();state.migrationWarnings=[];await restoreDraft();
assert.deepEqual(activeMigrationWarnings(),[warning]);
await startRun({preventDefault(){},target:form});
assert.deepEqual(sent.migration_warnings,[warning]);
assert.equal(sent.config.migration_warnings,undefined);
'''.replace('WARNING', json.dumps(config._migration_warnings[0])), config=config)


def test_ui_startup_prints_warning_and_passes_explicit_severity(tmp_path, monkeypatch, capsys):
    import json
    import uvicorn
    import giraffe.web
    from giraffe.cli import main
    source = tmp_path / 'legacy.json'
    source.write_text(json.dumps(legacy_removed_load()))
    captured = {}
    class Listener:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def setsockopt(self, *args): pass
        def bind(self, *args): pass
        def listen(self, *args): pass
    def app(**kwargs):
        captured.update(kwargs)
        return 'app'
    monkeypatch.setattr('giraffe.cli.socket.socket', lambda *args: Listener())
    monkeypatch.setattr(giraffe.web, 'create_app', app)
    monkeypatch.setattr(uvicorn.Server, 'run', lambda *args, **kwargs: None)
    assert main(['ui', '--config', str(source), '--runs-dir', str(tmp_path)]) == 0
    assert 'Warning: ' + REMOVED_LOAD in capsys.readouterr().err
    assert captured['migration_warnings'] == [{'msg': REMOVED_LOAD, 'removed_check': 'load'}]
    assert REMOVED_LOAD not in captured['migration_notes']


def test_cli_run_prints_removal_as_warning(tmp_path, monkeypatch, capsys):
    import json
    from giraffe import cli
    from tests.test_web import report
    source = tmp_path / 'legacy.json'
    source.write_text(json.dumps(legacy_removed_load()))
    async def run(config, **kwargs):
        assert config.checks == ['access']
        assert config._migration_warnings == [{'msg': REMOVED_LOAD, 'removed_check': 'load'}]
        return report(config)
    monkeypatch.setattr(cli, '_run', run)
    assert cli.main(['run', '--config', str(source), '--skip-preflight', '--output', str(tmp_path / 'output')]) == 0
    assert 'Warning: ' + REMOVED_LOAD in capsys.readouterr().err
