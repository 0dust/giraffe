# Brooks-Lint Review

**Mode:** PR Review
**Scope:** Targeted draft-validation follow-up only: `ConfigDraft`, `DraftTarget`, shared default checks and strict check cardinality in `models.py`; bootstrap and draft API in `web.py`; Apply/import/export and strict snapshot feedback in `app.js`; asset version in `index.html`; README contract; related web and UI regressions. Earlier uncommitted traffic and Form/JSON work is outside this review.
**Health Score:** 100/100
**Trend:** Stable at 100 (prior reviews had different scopes).

The fix cleanly separates editing a draft from preparing an executable run, preserving strict execution validation without introducing a second configuration format.

## Findings

No remaining actionable Critical, Warning, or Suggestion findings in the reviewed scope.

The first inspected version used CLI-only wording inside `validatedSnapshot`, which also serves Run. The parent independently corrected it to “before running or copying a CLI command”; the corrected source was reread before scoring. No unresolved deduction remains.

## Review evidence

- Change propagation: model, API, UI, documentation, and regression edits address one end-to-end configuration behavior; they are not unrelated responsibilities.
- Cognitive load and accidental complexity: two small boundary model specializations remove bootstrap placeholder substitution and retain existing normalization. The draft option on snapshot validation expresses the actual editing-versus-execution distinction without duplicating validation rules.
- Duplication and dependencies: shared defaults and inherited validators preserve one source for traffic, target relationships, option constraints, and sparse overrides. No new package dependency or import cycle was introduced.
- Domain integrity: empty model strings and zero selected tests are explicit draft states. Execution endpoints still receive `RunConfig`; strict `Target.model` and check cardinality remain enforced. The UI never submits `ConfigDraft` instances directly into execution.
- Agent parity: agents can use the documented JSON draft endpoint and discover its schema through OpenAPI. CLI validation and executable command generation remain strict; blank model feedback identifies the target and focuses its editable representation.
- Quick Test Check: new API tests start from real bootstrap configuration, preserve blank models and sparse settings, verify draft round-tripping, reject malformed settings, and check strict validation/CLI/run rejection without starting traffic. UI regressions cover applying/exporting incomplete drafts and focusing first and secondary missing models. Assertions describe observable behavior; existing VM fakes isolate browser APIs rather than replacing configuration normalization coverage.

## Independent verification

`pytest -q tests/test_web.py tests/test_ui_forms.py tests/test_models.py -k 'not real_api_to_runner_to_http_endpoint'`: **62 passed, 1 deselected** in 1.51s.

An initial run including that integration test produced the same 62 passes and one sandbox `PermissionError` while binding its local fake HTTP server. This is an environment limitation, not evidence that the integration test passes; the parent is performing broader verification with the appropriate permissions. This reviewer did not perform a live browser check or send model traffic.

## Reviewed source hashes

- `src/giraffe/models.py`: `10ee3ebfe7502b5569e9a452a2f059d96124d8ee9a26b89cd4ec2ffa823f55b0`
- `src/giraffe/web.py`: `075a1df8eb379e8215ee7b0a56d9b83b84785e2a1e30d28970a94f4daf39e4c1`
- `src/giraffe/static/app.js`: `b2bfd7cc2d4f0ddb89df3e7fb90bb632fba5bae560ef96d8b3ed440e106e4b2e`
- `src/giraffe/static/index.html`: `661a5a1c381c3ed3d699e3f348b431fd209cc16360fba0dd5190cf47fbbe94ee`
- `README.md`: `3052f7236f8e3c9953298b19353abe8407611041d730afb265a28122669200b7`
- `tests/test_web.py`: `96b25d06aa91a284f78791fe09c70d7f499181a52845b8ecf64b9c83a0c975e4`
- `tests/test_ui_forms.py`: `8319481dae67c779afbee4cec40bfb7c562c4c8eb058f8201db45f9aaa0acac3`

## Summary

The user-visible failure is fixed at the correct boundary: drafts can be edited and exported before they are runnable. The score follows the skill's 100-minus-findings formula and is limited to this targeted change; it is not a guarantee of absence of defects or a reassessment of the full uncommitted feature.
