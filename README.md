# Inky Bird Frame

Inky Bird Frame turns nearby public bird observations into reusable scientific
field-journal plates and rotates approved plates on a Pimoroni Inky Impression
Spectra 13.3 display.

The system has two roles:

- The **controller** discovers species, downloads licensed reference photos,
  researches species facts, generates a candidate through Codex, runs visual
  quality review, and serves the approved catalog.
- The **display node** pulls approved assets, verifies their checksums, and
  rotates them on the Inky panel.

Discovery location is private controller configuration. Approved images and
manifests contain no ZIP code, coordinates, observation dates, or local place
names, so the catalog can be reused and published.

## Pipeline

1. Resolve a configured US ZIP code with Zippopotam.us.
2. Query iNaturalist for bird species within the configured radius and rolling
   observation window.
3. Skip taxa already approved, pending, rejected, or failed.
4. Download multiple research-grade iNaturalist reference photos from distinct
   observers. Only CC0 and CC BY photos are accepted.
5. Use the ChatGPT-authenticated Codex CLI to research a cited species profile.
6. Attach every reference photo and the species-specific profile to the
   versioned image prompt in
   [`src/inky_bird_frame/prompts.py`](src/inky_bird_frame/prompts.py).
7. Run a separate Codex review that independently verifies facts with current
   authoritative sources and checks the plate against every reference photo.
8. Regenerate failed reviews with the concrete findings as corrective input,
   up to the configured attempt limit.
9. Automatically publish a passing plate to the immutable catalog and serve it
   to displays.

An approved taxon is never regenerated implicitly. Work that exhausts its
bounded attempts is terminal until an operator runs `retry`.

## Requirements

Controller:

- macOS or Linux with Python 3.11 or newer
- A Codex CLI session authenticated with a ChatGPT subscription
- Network access to Codex, iNaturalist, Zippopotam.us, and cited research sites

Display node:

- Raspberry Pi with a Pimoroni Inky Impression Spectra 13.3
- Python 3.11 or newer
- Network access to the controller HTTP service

The display reports a `1600x1200` landscape canvas. Plates are authored at
`1200x1600` and rotated left for portrait mounting.

## Install

```bash
uv sync --extra dev --locked
cp config.example.toml config.toml
```

Set the private discovery ZIP, radius, rolling window, local paths, and
controller URL in `config.toml`. Do not commit that file.

On the Pi, install the hardware extra into the Pimoroni environment:

```bash
python -m pip install -e '.[inky]'
```

## Operate

```bash
# Confirm local discovery without generating anything.
uv run inky-bird-frame discover --config config.toml

# Generate and AI-review at most generations_per_cycle missing species.
uv run inky-bird-frame controller-cycle --config config.toml

# Inspect approved, pending, and failed work.
uv run inky-bird-frame status --config config.toml

# Recovery controls for an interrupted cycle or an operator override.
uv run inky-bird-frame approve --config config.toml TAXON_ID
uv run inky-bird-frame reject --config config.toml TAXON_ID --reason "..."
uv run inky-bird-frame retry --config config.toml TAXON_ID

# Controller and display-node service entry points.
uv run inky-bird-frame serve --config config.toml
uv run inky-bird-frame display-cycle --config config.toml
```

Observation windows are `last-day`, `last-week`, `last-30-days`, and
`all-time`. Distance is configured as `radius_km`; 8 km is approximately 5
miles.

## Catalog

Approved plates live under `catalog/species/<taxon-id>-<slug>/` with:

- `portrait.png`: location-neutral `1200x1600` source plate
- `display.png`: hardware-ready `1600x1200` image
- `manifest.json`: facts, research and review sources, reference provenance,
  quality scores, generation metadata, and SHA-256 checksums

Downloaded reference photos, run logs, pending work, rejected work, and display
state live under `var/` and are ignored by Git.

## Development

```bash
uv sync --extra dev --locked
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

See [`docs/architecture.md`](docs/architecture.md),
[`docs/operations.md`](docs/operations.md), and
[`CONTRIBUTING.md`](CONTRIBUTING.md) for design, deployment, and contribution
details.
