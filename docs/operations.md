# Operations

## Controller configuration

Keep the deployment configuration outside the Git checkout. Required fields are
documented in `config.example.toml`.

The controller's `workspace_dir` must be writable because the Codex image tool
copies its final image there. `catalog_dir` and `state_dir` must persist across
deployments. `codex_path` must point to a Codex CLI whose `login status` reports
a ChatGPT-authenticated session.

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
manual overlap. Only a candidate that passes the independent AI review is
published.

`rotation_mode` is configured under `[display_node]`:

- `sequential`: stable round-robin order;
- `shuffle`: every active species once per shuffled round; or
- `weighted`: random selection weighted by current observation count, without
  immediate repeats when another species is active.

Approved plates use the project's canonical 1200x1600 portrait and 1600x1200
display assets. This geometry is a catalog contract so committed plates remain
portable across installations using the supported panel. Controller and display
state paths remain TOML configuration. Installer bootstrap paths, including the
TOML path itself, remain environment variables because the installer must find
the configuration before it can load it.

## Seed a broader catalog

The active display window and the generation backlog are separate. Use `seed`
to enqueue distinct taxa from a broader period without changing which birds are
currently active on the display:

```bash
inky-bird-frame seed --config /path/to/config.toml \
  --window last-year --species-limit 500 --dry-run
inky-bird-frame seed --config /path/to/config.toml \
  --window last-year --species-limit 500
```

The configured radius is used unless `--radius-km` is provided. Repeating a seed
is idempotent: approved, terminal, and already queued taxa are not added again.
Current observations remain ahead of seed-only taxa during generation.

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
installer. Its trusted runner reads display connection details from
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

## Failure recovery

- Network or source failure: inspect the refresh or generation log and JSON
  result. Generation refuses a discovery snapshot older than twice the
  configured refresh interval.
- Unsuitable licensed references: inspect the reference manifest and source
  pages. Retry only after deciding the source set can be improved.
- Generated image or text defect: the controller feeds review findings into a
  new attempt automatically. After all configured attempts fail, inspect the
  retained artifacts and use `retry` for a deliberate new cycle.
- Controller unavailable: the current e-paper image remains visible. Display
  state is not advanced.
- Checksum mismatch: the display refuses the asset and preserves current state.
- Catalog publication failure: inspect `catalog-publish.error.log`. Fix repository
  authentication, remote divergence, or the reported validation problem, then
  rerun `catalog-publish`. Local approval and display rotation continue while
  public publication is unavailable.
