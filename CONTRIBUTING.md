# Contributing

Contributions are welcome through pull requests.

## Development

Use Python 3.11 or newer and `uv`:

```bash
uv sync --extra dev --locked
uv run ruff format .
uv run ruff check .
uv run mypy
uv run pytest
```

Tests must not require network access, Codex authentication, or Inky hardware.
Mock those boundaries and keep fixtures location-neutral.

## Generated plates

Do not submit unreviewed generated images. A catalog contribution must include
the portrait image, display image, and manifest with factual sources, reference
provenance, automated quality scores, and matching SHA-256 checksums. Do not
include discovery location or downloaded reference image files.

The controller's independent, sourced AI review decides whether a generated
plate is accurate enough to publish. Maintainers can still reject or retry a
plate as an operational override. Plate generation does not happen in CI.

## Review and release

Pull requests must pass formatting, lint, strict typing, tests, and the Codex
review gate. After each push, the repository owner requests review with a
comment containing both `@codex review` and `head: <full current SHA>`. All
review conversations must be resolved before merge.

Production deployment is not available to contributors. It is an explicit
owner-only workflow dispatched from `main` on a trusted self-hosted runner.
