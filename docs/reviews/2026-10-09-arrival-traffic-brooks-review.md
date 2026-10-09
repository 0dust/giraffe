# Brooks-Lint Review

**Mode:** PR Review
**Scope:** Tracked and untracked implementation changes against `289dd7738ced2a4d81a90f9560e0bf5e5e5fdd40` on `codex/arrival-traffic-test-controls` (25 files before this review artifact). Sampled large-diff review, concentrating on arrival scheduling, cancellation, capacity qualification and comparison, selected-test configuration, and the UI configuration boundary.
**Health Score:** 100/100
**Trend:** Stable at 100 after the final selection and comparison follow-up

The frozen implementation has no unresolved Brooks findings after two configuration-contract defects identified by this independent review were corrected and retested.

---

## Findings

No unresolved Critical, Warning, or Suggestion findings in the reviewed scope. The score follows the skill's calculation: 100 minus 15 per unresolved Critical, 5 per Warning, and 1 per Suggestion. A score of 100 reflects this sampled review's remaining findings; it is not a guarantee of defect-free software or production infrastructure capacity.

### Resolved during this review

**Domain Model Distortion — Recovery ignored its editable correctness allowance (resolved)**

Symptom: Recovery exposed `test_options.recovery.limits.min_correctness` but its verdict failed on any incorrect answer, including answers within the configured allowance.

Source: *Domain-Driven Design* — Ubiquitous Language and invariant ownership. The named acceptance setting did not govern the corresponding domain decision.

Consequence: An operator could configure a tolerated error fraction and receive an inconsistent failed verdict; custom settings did not faithfully describe the test being executed.

Remedy: Recovery now compares its measured scored-answer fraction with the effective correctness threshold, preserves independent protocol/timing failures, and treats missing scores as inconclusive. The summary no longer attributes an independent failure to tolerated incorrect answers. Verified in [runner.py](/Users/0dust/Documents/giraffe/src/giraffe/runner.py:814) and the new recovery regression tests in [test_runner.py](/Users/0dust/Documents/giraffe/tests/test_runner.py:175).

**Change Propagation — Disabled custom fixtures blocked traffic comparisons (resolved)**

Symptom: The runner stopped loading custom fixtures when correctness was deselected, while baseline comparison still required a content hash whenever a saved custom-fixture path existed. The web preflight likewise still attempted to load that inactive file.

Source: *A Philosophy of Software Design* — Information Hiding and Leakage. The rule for whether custom fixtures participate in a run had diverged across execution, validation, and comparison boundaries.

Consequence: Identical capacity-only runs could produce an inconclusive upgrade comparison, or fail before execution in the UI, because of a fixture file that their selected tests never used.

Remedy: Web preflight and hash requirements now follow the selected correctness check. Regression tests cover unused missing-file drafts and both unchanged and reduced traffic capacity. Verified in [reporting.py](/Users/0dust/Documents/giraffe/src/giraffe/reporting.py:96), [web.py](/Users/0dust/Documents/giraffe/src/giraffe/web.py:317), and [test_traffic_reporting.py](/Users/0dust/Documents/giraffe/tests/test_traffic_reporting.py:135).

---

## Review evidence

The review applied all six production decay-risk scans and the PR review quick test check. No project `.brooks-lint.yaml` was present, so no risks or paths were disabled. No generated files were included in the implementation diff.

- **Change propagation and dependencies:** Configuration, runner, traffic, reporting, CLI, and UI changes implement one requested workflow. The scheduler depends on validated models and narrow execution callbacks; the UI receives supported fields and schemas from the backend. No new dependency cycle, unrelated subsystem, or speculative plugin layer was identified.
- **Cognitive load and complexity:** The async lifecycle is substantial, but the scheduler contains the admission, cancellation, and drain details behind one stage operation. Its boundaries are exercised by independent timing, drop, budget, drain, and cancellation scenarios. Long routines were considered in context instead of treated as automatic defects.
- **Domain and duplicated knowledge:** Offered arrivals, dispatched requests, local drops, lateness, service outcomes, and correct-and-timely goodput remain distinct. Capacity requires sufficient scored samples and a user-waiting limit. Missing or generator-limited evidence cannot establish a comparable capacity delta.
- **Test quality:** New tests assert observable outcomes rather than only mocked call wiring. Names describe behavior, the real loopback tests cover arrival independence and a slower service's capacity decrease, and serializer tests execute the browser's actual configuration module. Multiple assertions describe coherent failure stories.

After the parent declared code freeze, this reviewer reread the fixes, selected-ceiling validation, Poisson cluster/stall handling, and UI inheritance changes, then independently ran:

```text
.venv/bin/python -m pytest -q tests/test_models.py tests/test_traffic.py tests/test_traffic_reporting.py tests/test_ui_forms.py tests/test_runner.py
148 passed in 9.77s

git diff --check
passed
```

The parent integrator owns the complete-suite, lint, and browser acceptance checks. This review does not claim a live on-prem GPU/model qualification run. The included fake-service tests prove runner behavior; deployments still require representative traffic settings and their own acceptance thresholds.

### Final follow-up review

The integrator subsequently corrected inherited request tags that could make baseline comparison include deselected checks, and clarified the capacity-only baseline view. This reviewer reread the centralized filtering in `_Run.request`, its selected-check and baseline-group tests, and the UI's conditional metric/stage presentation. The filter retains selected checks while restricting capacity and generation evidence to their dedicated scenarios; no new review finding was identified.

The reviewer independently reran the affected runner, reporting, traffic-reporting and browser-behavior tests after those final edits:

```text
.venv/bin/python -m pytest -q tests/test_runner.py tests/test_reporting.py tests/test_traffic_reporting.py tests/test_ui_forms.py
143 passed in 7.09s
```

The final score remains **100/100**, with zero unresolved deductions on the updated implementation.

## Summary

The two defects found during review are fixed and covered by regression tests, leaving no unresolved score deductions. This is a large change, well above 500 lines, which is itself a Change Propagation signal; the sampled review found that its coordinated files serve the requested end-to-end feature rather than unrelated responsibilities. Future deployment claims should remain bounded to the tested rates, request mix, and observed correctness and timing evidence.
