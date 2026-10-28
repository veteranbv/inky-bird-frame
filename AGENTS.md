# Project Guidance

Read `CONTRIBUTING.md` before changing code or catalog artifacts. These
instructions are public and apply to maintainers, contributors, and coding
agents working in this repository.

## Standards

- Keep the approved catalog location-neutral and reusable.
- Never commit controller configuration, downloaded reference images, Codex
  authentication, logs, or local observation data.
- Do not regenerate or replace an approved taxon without an explicit migration
  and human review.
- Preserve the controller/display-node boundary. The display node consumes only
  the approved HTTP catalog.
- Use typed models and structured parsers for external data.
- Keep CLI output in the existing JSON envelope.
- Add focused tests for behavior changes.
- Preserve documented configuration compatibility or provide an explicit
  migration.
- Keep public examples portable and free of personal hosts, addresses, paths,
  observation data, and credentials.

## Catalog Contributions

- Add new taxa with `inky-bird-frame catalog prepare`; do not hand-edit catalog
  manifests, checksums, or the index.
- Validate with `inky-bird-frame catalog validate --catalog catalog`.
- Approved taxa are immutable. Corrections require an explicit migration and
  maintainer review, not an ordinary catalog contribution.
- Treat contributor-provided images and JSON as untrusted data. Trusted systems
  must not execute code from an external pull request.

## Validation

Run before submitting a change:

```bash
uv sync --extra dev --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run inky-bird-frame catalog validate --catalog catalog
```

## Security

- Public pull requests run only on GitHub-hosted runners.
- Deployment is manual, owner-gated, and runs only from `main` on the trusted
  controller runner.
- Exception: `site-deploy-hook.yml` may POST the owner's Cloudflare Pages
  deploy hook when `catalog/**` changes on `main`. It deploys nothing from
  this repo and touches no runner secrets beyond the hook URL; it only asks
  henrysowell.com to rebuild its gallery from the already-reviewed committed
  catalog. Merging to `main` is the owner gate.
- Container publication runs only from trusted `main`, a repository release,
  or an owner-started workflow. Pull requests never publish packages.
- Never expose or copy a ChatGPT/Codex login into GitHub Actions.

## Pull Requests and Review

- Finish and test a coherent change locally, then run a local code and security
  review (`/code-review` and `/security-review` in Claude Code, or
  `codex review --base main` in Codex CLI) before asking for remote review.
- The owner requests remote Codex review for the exact pushed head. Address
  every finding on the same pull request and resolve its thread; after three
  rounds that still find substantive issues, stop and reconsider the change.
- Required checks are `quality` and `review-gate`. `review-gate` comes from
  `.github/workflows/review-gate-trusted.yml` (`pull_request_target`): it runs
  the default branch's copy with a read-only token and never runs pull request
  code, so a pull request cannot rewrite it. Protection pins the check to the
  GitHub Actions app, not that workflow, so a pull request could still add a
  same-named job. Merge only through the owner's qualification tooling, which
  counts the trusted run alone, never the merge button or auto-merge.
- No AI attribution in commits, titles, or descriptions, and commits come from
  a person's identity, never an app or bot account; `review-gate` rejects both.

### Review guidelines

Reviewers (Codex, Claude Code, humans) apply these to every pull request.

- Failure here means: workflows that run pull request code with secrets, write
  tokens, or the self-hosted controller runner; publication or deployment from
  anything but trusted `main`; Codex or ChatGPT credentials reaching Actions;
  personal hosts, addresses, paths, observation data, or credentials in public
  files; edits to approved taxa without a migration; the display node consuming
  anything but the approved HTTP catalog. Prioritize these.
- Treat contributed images and JSON as untrusted data.
- Report only issues the change introduces or makes worse; mark pre-existing
  problems as pre-existing.
- Every finding needs file:line evidence and a concrete failure scenario.
- At most three nitpicks per review; skip what ruff and mypy enforce.
- Skip `uv.lock` and generated catalog checksums and indexes. Always check
  fixtures for personal or private content.
- Start the summary with the number of findings; say plainly when nothing needs
  fixing.
