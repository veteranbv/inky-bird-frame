# Notifications

Inky Bird Frame embeds the open-source Apprise Python library. Apprise translates one local
configuration model into each provider's API. You do not need an Apprise account, hosted Apprise
service, or separate notification container.

You do need the destination you choose. Pushover requires a Pushover account and application
token. Discord requires a channel webhook. A self-hosted ntfy or Gotify destination requires that
server. SMTP email requires a mailbox or relay. Notification credentials stay in the private
deployment `config.toml` or its process environment and are never written to the catalog.

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

The supported macOS installer runs `notifications dispatch` on the configured delivery retry
interval. Other service managers should schedule the same command. To requeue all dead letters
after fixing a destination, run:

```bash
inky-bird-frame notifications retry --config /path/to/config.toml
```

Status and validation output include only destination name, scheme, subscribed events, and queue
counts. They never include service URLs or credentials.

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

Routine successes do not notify. Transient failures notify only after either
`degradation_failure_threshold` consecutive failures or `degradation_window_minutes` has elapsed.
`cooldown_minutes` suppresses repeat notices while the same service remains unhealthy. Recovery
sends once, and only if a degradation notice was sent.

## Delivery reliability

Application work commits before notification delivery. A provider outage cannot roll back
discovery, generation, publication, or display state.

The controller stores a private durable outbox under its configured `state_dir`. Successful
destinations are recorded individually. If Discord succeeds and Pushover fails, the retry targets
only Pushover. Delivery retries after `delivery_retry_minutes`; after
`max_delivery_attempts`, the message moves to a visible dead-letter count for operator review.
Event IDs are retained in a bounded ledger so service restarts do not resend completed events.
Only one dispatcher performs network delivery at a time. Enqueue operations use a short separate
state lock, so a slow provider cannot block or overwrite application events.

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
into every application process. The supported macOS installer intentionally does not copy its
shell environment into LaunchAgents; use the private mode-0600 TOML file there.

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
