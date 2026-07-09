# Project Guidance

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

## Validation

Run before submitting a change:

```bash
uv sync --extra dev --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

## Security

- Public pull requests run only on GitHub-hosted runners.
- Deployment is manual, owner-gated, and runs only from `main` on the trusted
  controller runner.
- Never expose or copy a ChatGPT/Codex login into GitHub Actions.
