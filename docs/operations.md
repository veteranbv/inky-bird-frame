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

The rate limit keeps subscription use bounded and leaves every candidate at a
human approval gate.

## Review a candidate

```bash
inky-bird-frame status --config /path/to/config.toml
open /path/to/state/pending/TAXON_ID-SLUG/portrait.png
```

Check species markings, anatomy, every rendered word and number, composition,
and absence of location data. Then choose one action:

```bash
inky-bird-frame approve --config /path/to/config.toml TAXON_ID
inky-bird-frame reject --config /path/to/config.toml TAXON_ID --reason "specific issue"
```

After approval, commit the complete catalog directory for that taxon. This
preserves the accepted bitmap and prevents future installations from spending
generation quota on it.

## Failure recovery

- Network or source failure: inspect `var/controller/runs/` and the JSON command
  result. Failed taxa stay terminal.
- Unsuitable licensed references: inspect the reference manifest and source
  pages. Retry only after deciding the source set can be improved.
- Generated image or text defect: reject with a concrete reason, then run
  `retry` when ready for a deliberate new attempt.
- Controller unavailable: the current e-paper image remains visible. Display
  state is not advanced.
- Checksum mismatch: the display refuses the asset and preserves current state.

## Ethernet to Wi-Fi transition

The display node requires only outbound HTTP access to the controller. Its own
address is used for administration and monitoring, not by the application.

When moving to Wi-Fi:

1. connect the supported Wi-Fi adapter and configure the SSID locally;
2. verify the Wi-Fi interface has a DHCP lease and reaches `/health` on the
   controller;
3. create the desired UniFi reservation for the Wi-Fi adapter's MAC address;
4. update inventory, SSH deployment host, and monitoring to the reserved Wi-Fi
   address;
5. run `display-cycle --force`; and
6. disconnect Ethernet only after the display and monitoring checks pass.

No catalog or image needs to be regenerated.
