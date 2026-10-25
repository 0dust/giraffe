# Contributing

Create a branch and open a pull request targeting `main`. Every change requires
approval from either `@aiswarya797` or `@0dust` before merging. This includes
changes to the license, code owners, and repository rules. Authors cannot approve
their own pull requests; a pull request by either owner needs the other owner's
approval. Pushes that change the diff invalidate earlier approvals.

## Activate enforcement (repository admin)

The CODEOWNERS file selects reviewers. It does not enforce approvals on its own.
The JSON file below is a ruleset configuration; committing it does not activate
GitHub enforcement.

Review and merge the pull request adding `.github/CODEOWNERS`, then apply the
ruleset from the repository root while authenticated as a repository admin:

```sh
gh api --method POST repos/0dust/giraffe/rulesets \
  --input .github/main-ruleset.json
gh api repos/0dust/giraffe/rules/branches/main
```

Alternatively, import `.github/main-ruleset.json` under Settings → Rules →
Rulesets. Keep enforcement **Active** and the bypass list empty. The ruleset
requires pull requests with one code-owner approval and blocks force pushes and
deletion of `main`, including for administrators. An administrator can still
edit repository settings.

If the ruleset already exists, update it instead of creating a duplicate:

```sh
gh api repos/0dust/giraffe/rulesets
gh api --method PUT repos/0dust/giraffe/rulesets/RULESET_ID \
  --input .github/main-ruleset.json
```

## Automated checks

Pull requests and pushes to `main` run separate Ruff and pytest checks. CI uses
Python 3.12 because the validation observer tests require `sys.monitoring`, plus
Node 22 for browser behavior tests. Dependencies come from `uv.lock`:

```sh
uv sync --frozen --extra dev --extra sglang-tracing --python 3.12
uv run --no-sync ruff check .
uv run --no-sync python -m pytest -q -ra --tb=short
```

Tests use fake/local endpoints and require no LLM service, GPU or repository
secrets. CI disables Hugging Face downloads. Optional browser layout checks and
an uncached tokenizer integration may skip; the job summary lists their names
and reasons, and pytest logs retain full failure output. Skips are untested
coverage, not passes. This workflow does not activate branch-protection rules.

## Publish a release

The distribution is `giraffe-llm`; its import and command remain `giraffe`.
`.github/workflows/publish.yml` builds on pull requests without publishing.
A manual run from `main` publishes to TestPyPI; publishing a GitHub release
publishes to PyPI. Neither path uses a stored API token.

### One-time account setup

Create separate [PyPI](https://pypi.org/account/register/) and
[TestPyPI](https://test.pypi.org/account/register/) accounts, verify your email,
and enable two-factor authentication. Save recovery codes securely.

Add a pending GitHub trusted publisher under each account's **Publishing** page:
[PyPI](https://pypi.org/manage/account/publishing/) and
[TestPyPI](https://test.pypi.org/manage/account/publishing/).

| Field | PyPI | TestPyPI |
| --- | --- | --- |
| Project name | `giraffe-llm` | `giraffe-llm` |
| Owner | `0dust` | `0dust` |
| Repository | `giraffe` | `giraffe` |
| Workflow filename | `publish.yml` | `publish.yml` |
| Environment | `pypi` | `testpypi` |

These names must match exactly. A pending publisher creates the project only
when its first upload succeeds; it does not reserve the name. After that first
upload, manage it under the project's **Publishing** settings.

In GitHub **Settings → Environments**, maintain these protections:

- `pypi`: selected deployment tags `v*` only.
- `testpypi`: selected deployment branch `main` only.
- Both: approval from `0dust` or `aiswarya797`, prevent self-review, and disallow
  administrator bypass. The person who starts a release needs the other owner
  to approve its publishing job.

Environment settings are configured in GitHub, not enforced by this file.
Keep them in place before registering the trusted publishers.

### Rehearse, then publish

1. Update the version in `pyproject.toml` and `src/giraffe/__init__.py` together,
   run `uv lock`, and merge the reviewed change. For the first alpha, both are
   `0.1.0a1`. Keep the README version badge in sync when changing versions.
2. Run **Actions → Publish package → Run workflow** on `main`. The workflow
   checks versions, runs Ruff and pytest, builds a source archive and a wheel
   from that archive, validates metadata, and checks the installed CLI and UI
   assets in a fresh environment. Approve the `testpypi` deployment after
   reviewing its commit and results.
3. In a fresh virtual environment, download just Giraffe from TestPyPI, then
   install that wheel with dependencies from the normal PyPI index:

   ```sh
   python -m pip download --no-deps --only-binary=:all: \
     --index-url https://test.pypi.org/simple/ --dest test-dist \
     "giraffe-llm==0.1.0a1"
   python -m pip install test-dist/giraffe_llm-0.1.0a1-py3-none-any.whl
   giraffe --help
   giraffe ui
   ```

   Open the UI and confirm it loads. Avoid combining TestPyPI and PyPI with
   `--extra-index-url`; dependencies should come from the normal PyPI index.
4. Create a GitHub release tagged `v0.1.0a1` at the same tested commit on `main`
   and mark this alpha as a prerelease. Publishing the release reruns verification
   and waits for approval of the `pypi` deployment. A draft release does not upload.
5. After upload, verify `python -m pip install "giraffe-llm==0.1.0a1"` and the CLI
   in another fresh environment. Then update README installation instructions
   to use the published package.

Use a new version for changed artifacts; uploaded distribution filenames cannot
be reused. Upload failures remain failures instead of silently skipping existing
files. Inspect the registry before retrying a partially completed upload.

Publishing jobs download only the distributions built in that run. They do not
check out or execute repository code, and only those jobs receive `id-token:
write`. Build tooling is pinned in the `release` dependency group and `uv.lock`;
GitHub Actions are pinned to commit SHAs. PyPI's publish action also produces
attestations through Trusted Publishing.

For local package verification:

```sh
uv sync --locked --group release --extra dev --extra sglang-tracing --python 3.12
uv run --no-sync python -m build --no-isolation
uv run --no-sync twine check --strict dist/*
```

See [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/) and
[its security guidance](https://docs.pypi.org/trusted-publishers/security-model/).
