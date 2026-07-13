# Notifications

Inky Bird Frame includes the open-source Apprise Python library. It translates
one local configuration into each provider's API. You do not need an Apprise
account, hosted Apprise service, or separate notification container.

You do need an account or server for the destination you choose. Pushover needs
a Pushover account and application token. Discord needs a channel webhook. A
self-hosted ntfy or Gotify destination needs that server. Email needs a mailbox
or SMTP relay. Credentials stay in private configuration and never enter the
bird catalog.

## Basic setup

1. Copy one destination example from `config.example.toml` into your private `config.toml`.
2. Replace the placeholders with the credentials supplied by that service.
3. Set `enabled = true` under `[notifications]`.
4. Validate without sending:

   ```bash
   inky-bird-frame config validate --config /path/to/config.toml
   ```

5. Send one test to every configured destination:

   ```bash
   inky-bird-frame notifications test --config /path/to/config.toml
   ```

6. Inspect redacted delivery state:

   ```bash
   inky-bird-frame notifications status --config /path/to/config.toml
   ```

The native installers run `notifications dispatch` on the configured retry
interval. Other service managers should schedule the same command. After
fixing a destination, requeue its dead letters with:

```bash
inky-bird-frame notifications retry --config /path/to/config.toml
```

Status and validation output includes the destination name, scheme, subscribed
events, and queue counts. It never includes service URLs or credentials.

## Events and noise controls

Each destination has its own `events` list:

| Event | Sent when |
| --- | --- |
| `discovery` | A refresh finds species not present in the previous snapshot; names are aggregated |
| `generation_approved` | A new plate passes factual and visual review |
| `terminal_error` | A taxon exhausts automated quality review and needs an explicit retry |
| `degraded` | A transient operation crosses the configured count or duration threshold |
| `recovered` | A previously reported transient degradation succeeds again |
| `publication_error` | Public catalog publication crosses the degradation threshold |
| `publication_recovered` | Public catalog publication succeeds after a reported degradation |

Routine successes do not notify. Transient failures notify only after
`degradation_failure_threshold` consecutive failures or
`degradation_window_minutes` has elapsed. `cooldown_minutes` suppresses repeat
notices while the same service remains unhealthy. Recovery sends once, and
only after a degradation notice was sent.

## Delivery reliability

Application work commits before notification delivery. A provider outage cannot
roll back discovery, generation, publication, or display state.

The controller stores a durable private outbox under `state_dir`. It records
each destination separately. If Discord succeeds and Pushover fails, only
Pushover is retried. After `max_delivery_attempts`, the message moves to a
visible dead-letter count for inspection. A bounded event ledger prevents
completed messages from being resent after a restart. A slow provider cannot
block or overwrite application events.

## Provider setup

### Pushover

Create a Pushover application, then use its user key and application token:

```toml
[[notifications.destinations]]
name = "pushover"
url = "pover://USER_KEY@APP_TOKEN"
events = ["discovery", "generation_approved", "terminal_error", "degraded", "recovered"]
```

Pushover is a hosted service. Apprise talks directly to it; there is no intermediary.

### Discord

In the desired Discord channel, open **Edit Channel > Integrations > Webhooks**, create a webhook,
and copy its URL. Put the webhook ID and token into the Apprise form:

```toml
[[notifications.destinations]]
name = "discord"
url = "discord://WEBHOOK_ID/WEBHOOK_TOKEN"
events = ["generation_approved", "terminal_error"]
```

That is the complete user flow. No bot, OAuth application, or Apprise account is required.

### ntfy

For the hosted ntfy service, choose a hard-to-guess private topic:

```toml
url = "ntfy://PRIVATE_TOPIC"
```

For a self-hosted TLS endpoint:

```toml
url = "ntfys://ntfy.example.com/PRIVATE_TOPIC"
```

Public topic names are bearer secrets: anyone who knows one may be able to publish or subscribe
unless access control is enabled.

### Gotify

Create an application in the self-hosted Gotify UI and use its application token:

```toml
url = "gotifys://gotify.example.com/APP_TOKEN"
```

### Slack

Use the three components from a Slack incoming-webhook token:

```toml
url = "slack://TOKEN_A/TOKEN_B/TOKEN_C"
```

### Email

SMTP URLs often contain punctuation that must be percent-encoded:

```toml
[[notifications.destinations]]
name = "email"
url = "mailtos://USER:PASSWORD@mail.example.com/recipient@example.com"
events = ["terminal_error", "degraded", "recovered"]
```

Use `url_env` instead of `url` only when your service manager injects that environment variable
into every application process. The supported macOS installer rejects `url_env` because launchd
does not inherit the installer's shell environment; use the private mode-0600 TOML file there.

### Home Assistant

Create a long-lived access token in the Home Assistant profile, then configure:

```toml
url = "hassios://homeassistant.example.com/LONG_LIVED_ACCESS_TOKEN"
```

### Generic JSON webhook

For an HTTPS endpoint that accepts Apprise's JSON payload:

```toml
url = "jsons://webhook.example.com/path"
```

Use the dedicated Discord, Slack, and Home Assistant schemes when available. Their authentication
and payload behavior are provider-aware; the generic webhook is for endpoints with no dedicated
Apprise plugin.

## Secret handling

- Never add a real service URL to `config.example.toml`, documentation, issues, logs, or catalog
  files.
- Keep the deployed `config.toml` outside the Git checkout.
- Use `url_env` when a service URL is injected by a secret manager or service manager.
- URL-encode usernames, passwords, tokens, paths, and recipients when they contain URL-reserved
  characters.
- Rotate a token immediately if its full URL appears in terminal output or version control.
