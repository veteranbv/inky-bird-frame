# Operations

## Controller configuration

Keep the deployment configuration outside the Git checkout. Required fields are
documented in `config.example.toml`.

The controller's `workspace_dir` must be writable because the Codex image tool
copies its final image there. `catalog_dir` and `state_dir` must persist across
deployments. `codex_path` must point to a Codex CLI whose `login status` reports
a ChatGPT-authenticated session.

Recommended schedule:

- controller HTTP service: always running;
- generation cycle: every six hours, one candidate per cycle;
- display cycle: every 30 minutes.

The cycle limit and `max_generation_attempts` keep subscription use bounded.
Only a candidate that passes the independent AI review is published.

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

Commit the complete catalog directory for every published taxon. This preserves
the accepted bitmap for other installations and prevents them from spending
generation quota on it.

## Failure recovery

- Network or source failure: inspect `var/controller/runs/` and the JSON command
  result. The taxon remains eligible for the next scheduled cycle.
- Unsuitable licensed references: inspect the reference manifest and source
  pages. Retry only after deciding the source set can be improved.
- Generated image or text defect: the controller feeds review findings into a
  new attempt automatically. After all configured attempts fail, inspect the
  retained artifacts and use `retry` for a deliberate new cycle.
- Controller unavailable: the current e-paper image remains visible. Display
  state is not advanced.
- Checksum mismatch: the display refuses the asset and preserves current state.
