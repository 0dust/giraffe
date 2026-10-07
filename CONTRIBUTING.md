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
