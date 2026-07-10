# Contributing

Contributions are welcome. Useful changes include bug fixes, focused features,
hardware support, documentation, tests, and new bird plates for the reusable
catalog.

## Before starting

- Search existing issues and pull requests.
- Open a feature request before a change that affects architecture, catalog
  policy, external APIs, configuration compatibility, or a major dependency.
- Use the bird request template to claim a species before investing in a new
  plate. This helps avoid duplicate generation work.
- Report vulnerabilities privately as described in [`SECURITY.md`](SECURITY.md).

## Development setup

Use Python 3.11 or newer and [`uv`](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/veteranbv/inky-bird-frame.git
cd inky-bird-frame
uv sync --extra dev --locked
```

Run the application from the managed environment:

```bash
uv run inky-bird-frame --help
```

Private controller configuration is not required for unit tests or catalog
validation. Never commit a real `config.toml`, notification destination,
observation snapshot, downloaded reference image, generated run directory, or
authentication material.

## Engineering expectations

- Keep changes small, coherent, and directly related to the stated problem.
- Prefer existing modules and patterns over new layers or dependencies.
- Do not submit stubs, fake implementations, hidden fallbacks, or unexplained
  hardcoded values.
- Preserve the controller/display-node boundary. The display node consumes the
  approved HTTP catalog and does not perform discovery or generation.
- Use typed models and structured parsers for external data. Avoid untyped
  dictionaries at module boundaries.
- Keep CLI success and failure output in the existing JSON envelope.
- Treat network services, Codex, GitHub, notifications, and display hardware as
  explicit boundaries with bounded failures and useful errors.
- Maintain compatibility for documented configuration unless the change
  includes a clear migration.
- Add focused tests for behavior changes and regression tests for bug fixes.

Tests must not require network access, Codex authentication, notification
credentials, or Inky hardware. Mock those boundaries and keep fixtures
location-neutral.

## Code changes

1. Create a focused branch in your fork.
2. Implement the smallest complete change.
3. Update documentation and `config.example.toml` when public behavior or
   configuration changes.
4. Run the project checks.
5. Open a pull request and complete the applicable template sections.

Required checks:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run inky-bird-frame catalog validate --catalog catalog
```

Run `uv run ruff format .` before the checks when Python files need formatting.
CI also validates GitHub Actions and shell deployment scripts.

## Catalog contributions

Catalog entries are immutable, location-neutral artifacts. A contribution adds
one new taxon; it must not edit or replace an approved taxon.

### Generate and prepare a plate

Generate and approve the plate using your own controller and AI account. Then,
from a checkout of your fork, copy exactly one approved taxon into the public
catalog:

```bash
uv run inky-bird-frame catalog prepare <taxon-id> \
  --source-catalog <approved-catalog> \
  --catalog catalog

uv run inky-bird-frame catalog validate --catalog catalog
```

`catalog prepare` validates the complete source catalog, copies only the
requested taxon, rebuilds `catalog/index.json`, and validates the result. It
fails if the source taxon is missing or conflicts with an existing entry. Do
not hand-edit generated JSON or checksums.

### Required plate contents

Each `catalog/species/<taxon-id>-<slug>/` directory contains only the files
allowed by the catalog validator:

- `portrait.png`: metadata-free `1200x1600` PNG;
- `display.png`: metadata-free `1600x1200` PNG;
- `manifest.json`: identity, facts, sources, provenance, generation metadata,
  review, and matching SHA-256 checksums;
- `profile.json`: factual species profile matching the manifest, when produced
  by the current pipeline; and
- `quality-review.json`: sourced review matching the manifest, when produced by
  the current pipeline.

The contribution must not contain discovery locations, observation counts,
local paths, private service addresses, credentials, run logs, or downloaded
reference images. Source URLs and reference licensing or provenance remain in
the metadata, but source bitmaps are not redistributed.

You must have the right to submit the generated images and metadata under the
repository's MIT license. Do not submit copyrighted field-guide artwork or
reference photographs as catalog assets.

### Catalog review

Public CI checks the file allowlist, schema, checksums, image dimensions,
embedded image metadata, private fields, provenance requirements, quality
review thresholds, index consistency, and add-only behavior against the pull
request base.

A submitted `quality-review.json` is evidence from the contributor's pipeline,
not a substitute for maintainer-side verification. Maintainers may run an
independent, sourced review of the committed artifacts before acceptance. A
trusted reviewer must treat pull request files as data and must never execute a
contributor branch on the private controller.

## Pull request review

Pull requests should explain the user-visible behavior, validation performed,
security or compatibility implications, and anything that could not be tested.
Review findings should be resolved on the same pull request so the complete
change remains visible to reviewers.

All pull requests must pass formatting, lint, strict typing, tests, catalog
validation, and the repository review gate. The repository owner controls the
exact-head Codex review request and production deployment. External pull
requests never receive those credentials.

## Documentation and agent guidance

Public documentation and examples must be portable. Use placeholders for
locations, hosts, URLs, usernames, and secrets. `AGENTS.md` contains public
project-wide guidance for coding agents; update it only when a durable project
rule changes, and keep detailed procedures in the relevant documentation.
