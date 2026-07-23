# Operations

## Controller configuration

Keep the deployment configuration outside the Git checkout. Required fields are
documented in `config.example.toml`.

Recommended starting values balance local relevance, seasonal variety, API use,
and subscription-backed generation:

| Setting | Recommended | Why |
| --- | --- | --- |
| `discovery.radius_km` | `8` | Approximately five miles; widen it in sparsely observed areas. |
| `discovery.species_limit` | `50` | Avoids truncating normal local results without creating an unbounded active set. |
| `discovery.window` | `"last-30-days"` | More reliable than a short window while remaining seasonally relevant. |
| `schedule.refresh_minutes` | `15` | Keeps observations current at a modest API request rate. |
| `schedule.generation_minutes` | `360` | Generates at most four new plates per day by default. |
| `controller.generations_per_cycle` | `1` | Bounds work and recovery impact per invocation. |
| `research.max_searches_per_day` | `5` | Covers the default generation rate plus one recovery. |
| `research.max_searches_per_species` | `2` | Allows one normal attempt and one bounded recovery. |
| `schedule.rotation_minutes` | `30` | A calm starting cadence for an e-paper display. |
| `display_node.rotation_mode` | `"shuffle_bag"` | Shows every active bird once before repeating. |

These are starting points, not service limits. If the controller generates more
often, review and raise `max_searches_per_day` to match. Cached profiles do not
consume the budget again. Use `seed` for a broad historical catalog instead of
making the active observation window permanently broad.

The controller's `workspace_dir` must be writable because the Codex image tool
copies its final image there. Keep it separate from configuration, catalog, and
state; the example uses a dedicated `workspace` directory. `catalog_dir` and
`state_dir` must persist across deployments. `codex_path` must point to a Codex
CLI whose `login status` reports a ChatGPT-authenticated session.

Schedules are configured in `[schedule]`. Conservative starting values are:

- controller HTTP service: always running;
- observation refresh: every 15 minutes;
- generation cycle: every six hours, one candidate per cycle;
- catalog publication: every five minutes when enabled; and
- display cycle: every 30 minutes.

The refresh command does not invoke Codex. `generation_minutes`,
`generations_per_cycle`, and `max_generation_attempts` jointly bound
subscription use. If generation takes longer than its interval, the service
manager does not start a second copy and the generation lock also rejects
manual overlap. Only a candidate that passes the independent Codex review is
published.

`max_species_attempts_per_cycle` is a separate queue scan cap. A transiently
failing species receives durable exponential backoff and no longer consumes the
successful-generation quota on every cycle. Insufficient licensed references
use the longer `insufficient_references_retry_minutes` delay because source
availability changes slowly. Later birds continue through the queue. Exhausted
factual or visual review remains terminal and requires `retry TAXON_ID`.

Discovery requests and validates species-rank iNaturalist results so genera,
families, and other aggregate taxa never enter generation. Species context uses
iNaturalist first and the Cornell BirdNET Taxonomy API when
iNaturalist omits its descriptive context. The fallback is accepted only when
the iNaturalist taxon ID and scientific name match exactly. Licensed reference
photos remain research-grade CC0 or CC BY iNaturalist observations from distinct
observers. The application does not substitute arbitrary web images.

A new species profile may use one tightly bounded Codex web research pass after
the structured context and image references are assembled. Research is limited
by configured domains, per-species attempts, and a daily total. A validated
profile is cached, so image retries do not repeat profile research. Independent
quality review may revisit configured source domains to verify the rendered
facts rather than trusting the profile's citations.

`rotation_mode` is configured under `[display_node]`:

- `sequential`: stable round-robin order;
- `shuffle`: existing shuffled-round behavior; removed species are pruned, while
  new species join on the next refill;
- `shuffle_bag`: a separately persisted bag that shows each active species at
  most once per refill, admits new active species immediately in randomized
  order, prunes inactive species, and avoids repeating the prior species across
  a refill when another species is active; or
- `weighted`: random selection weighted by current observation count, without
  immediate repeats when another species is active.

`prioritize_latest_detection = true` is the default. When the active catalog
contains BirdWeather timestamps, the newest detection later than the display
node's durable watermark is shown once before the configured rotation resumes.
The priority display counts as shown when that bird is already next in sequence
or present in a shuffle pool, which prevents an immediate duplicate without
reordering the other birds. A failed panel update does not consume the
detection, and a first run shows only the current newest detection rather than
replaying the historical window. Set the option to `false` to use
`rotation_mode` for every update.

Approved plates use the project's canonical 1200x1600 portrait and 1600x1200
display assets. This geometry remains the catalog contract. PIM774 consumes the
display asset unchanged; a PIM773 display node contains the full asset on its
800x480 canvas with paper-colored margins and no crop or stretch. Controller
and display state paths remain TOML configuration. Installer bootstrap paths,
including the TOML path itself, remain environment variables because the
installer must find the configuration before it can load it.

## Seed a broader catalog

The active display window and the generation backlog are separate. Use `seed`
to enqueue distinct taxa from a broader period without changing which birds are
currently active on the display:

```bash
inky-bird-frame seed --config /path/to/config.toml \
  --source inaturalist --window last-year --species-limit 500 --dry-run
inky-bird-frame seed --config /path/to/config.toml \
  --source inaturalist --window last-year --species-limit 500
```

To seed a historical trip or event, preview an inclusive iNaturalist date range
around a command-scoped coordinate before applying the same command:

```bash
inky-bird-frame seed --config /path/to/config.toml \
  --source inaturalist --latitude 40.7128 --longitude -74.0060 \
  --radius-km 11 --start-date 2026-04-01 --end-date 2026-04-03 \
  --species-limit 500 --dry-run
```

The configured source is used unless `--source` is provided. eBird cannot query
beyond 30 days or guarantee arbitrary coordinate-radius historical windows, so
exact date ranges require iNaturalist. `--latitude` and `--longitude` must be
provided together and do not change the configured location. The configured
radius is used unless `--radius-km` is provided. Repeating a seed
is idempotent: approved, terminal, and already queued taxa are not added again.
Current observations remain ahead of seed-only taxa during generation.

## Run a combined cycle

`refresh` and `generate` are the scheduled one-shot commands.
`controller-cycle` runs one observation refresh followed by one generation
cycle in a single invocation and reports the combined JSON result:

```bash
inky-bird-frame controller-cycle --config /path/to/config.toml
```

Use it for a manual end-to-end pass. Scheduled installations keep the separate
`refresh` and `generate` commands so each job retains its own interval and
failure notifications; `controller-cycle` does not send the per-command
notifications those commands emit.

## Copy species between catalogs

`catalog sync` copies every species missing from one catalog into another and
rebuilds the destination index:

```bash
inky-bird-frame catalog sync --source-catalog /path/to/source \
  --catalog /path/to/destination
```

The command is add-only. It validates both catalogs, refuses a destination
taxon that conflicts with its immutable source version, and reports published
and already-present taxa. Pass `--state-dir` with the controller state
directory when the destination is a live controller catalog, so the copy holds
the same lock as generation. The Docker bootstrap service uses this command to
copy the image's bundled catalog into persistent storage; see the
[Docker controller guide](docker.md#what-runs).

## Catalog publication

Clone this project repository into a controller-only checkout. Install GitHub
CLI and authenticate it as the repository owner. The owner must have pull
request bypass permission on the base-branch ruleset. GitHub CLI stores its
credential outside application configuration; do not use a deploy key because a
deploy key cannot exercise the owner's pull request bypass.

```toml
[public_catalog]
enabled = true
checkout_dir = "/path/to/inky-bird-frame-source"
repository = "owner/inky-bird-frame"
gh_path = "/path/to/gh"
remote = "origin"
base_branch = "main"
commit_name = "Inky Bird Frame Catalog"
commit_email = "inky-bird-frame@users.noreply.github.com"

[schedule]
catalog_publish_minutes = 5
```

Validate the complete local and remote catalog without committing or pushing:

```bash
inky-bird-frame catalog-publish --config /path/to/config.toml --dry-run
```

Run an immediate publication cycle:

```bash
inky-bird-frame catalog-publish --config /path/to/config.toml
```

The publisher copies only new approved taxa. Existing repository taxa must be
byte-for-byte identical to their local approved versions. It never publishes
the private discovery snapshot, downloaded reference bitmaps, run logs, failed
attempts, or display state. The macOS installer creates the publication
LaunchAgent only when `[public_catalog].enabled` is true.

## Maintainer deployment on macOS

The optional owner-only deployment workflow uses the included macOS controller
installer. The self-hosted Actions runner dispatches that installer as a
one-shot job in the controller user's GUI launchd domain. This is the same
security context as the catalog publisher, so the existing keychain-backed
GitHub CLI credential remains available without copying it into an Actions
secret or environment variable.

The trusted runner reads display connection details from
`~/Library/Application Support/Inky Bird Frame/deployment.env`:

```bash
INKY_BIRD_DISPLAY_HOST=display-node-address
INKY_BIRD_DISPLAY_USER=display-user
INKY_BIRD_DISPLAY_SSH_KEY="$HOME/.ssh/inky-bird-frame-display"
INKY_BIRD_DISPLAY_APP_DIR=/home/display-user/Services/inky-bird-frame
INKY_BIRD_DISPLAY_CONFIG_PATH=/home/display-user/.config/inky-bird-frame/config.toml
INKY_BIRD_DISPLAY_VENV=/home/display-user/.virtualenvs/inky-bird-frame
```

All six values are deployment-specific and required. Keep this file on the
controller. It is not part of the repository.

## Inspect or override a candidate

```bash
inky-bird-frame status --config /path/to/config.toml
```

Normal operation does not require a human approval. The commands below are
recovery and operator-override controls for a candidate left pending by an
interrupted cycle:

```bash
inky-bird-frame approve --config /path/to/config.toml TAXON_ID
inky-bird-frame reject --config /path/to/config.toml TAXON_ID --reason "specific issue"
```

Enable catalog publication to preserve accepted plates for other installations.
Each generated image receives an auditable catalog-only PR, while application
code continues through the full review and CI policy.

## Display a personal image

`prepare-image` fits any image onto the supported plate geometry without
Codex:

```bash
inky-bird-frame prepare-image /path/to/image.jpg --output-dir output
```

It centers the source on a paper-colored `1200x1600` portrait canvas and
writes `<name>-portrait.png` plus a rotated `1600x1200` `<name>-display.png`
into `--output-dir` (default `output`). Add `--display` to also send the
prepared display asset to a locally attached Inky panel, or copy the file to
the display node and use `display-image`. Prepared images are local output
only; they never enter the approved catalog or rotation.

## Failure recovery

- Network or source failure: inspect the refresh or generation log and JSON
  result. Generation refuses a discovery snapshot older than twice the
  configured refresh interval.
- Unsuitable licensed references: inspect the reference manifest and source
  pages. The taxon is deferred automatically and later queue items continue.
- Generated image or text defect: the controller feeds review findings into a
  new attempt automatically. After all configured attempts fail, inspect the
  retained artifacts and use `retry` for a deliberate new cycle.
- Controller unavailable: the current e-paper image remains visible. Display
  state is not advanced.
- Checksum mismatch: the display refuses the asset and preserves current state.
- Catalog publication failure: inspect `catalog-publish.log` for the structured
  command error; `catalog-publish.error.log` is reserved for process-level
  diagnostics. Run `gh auth status --hostname github.com` as the
  controller service account, then fix authentication, remote divergence, or the
  reported validation problem. Confirm recovery with `catalog-publish --dry-run`
  before rerunning `catalog-publish`. Local approval and display rotation continue
  while public publication is unavailable.

## Runtime state retention

`runs/`, `failed/`, and `rejected/` under the controller `state_dir` grow
without automatic pruning so failed and rejected work stays available for
inspection. Check them first when investigating a generation failure. Entries
are safe to delete once they have been reviewed.

## Log retention

On systemd hosts the services log JSON to journald and rely on the
distribution's default journal rotation. Set `SystemMaxUse=` in
`/etc/systemd/journald.conf` and restart `systemd-journald` to enforce a hard
size cap. macOS LaunchAgents write to log files under the managed support
directory instead.

See [`notifications.md`](notifications.md) for provider setup, event filtering,
durable delivery, noise controls, testing, and redacted status commands.
