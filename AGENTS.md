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
- Container publication runs only from trusted `main`, a repository release,
  or an owner-started workflow. Pull requests never publish packages.
- Never expose or copy a ChatGPT/Codex login into GitHub Actions.
