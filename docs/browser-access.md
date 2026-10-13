# Owner access

Owner access uses the same controller service as the catalog, with a separate
security boundary. The display and existing Homepage routes keep their current
read-only contract. Do not expose those routes to the internet.

This worktree first proves setup, sign-in, sign-out, and local recovery. The
complete field-journal interface is tracked in issue #310. This checkpoint
does not claim the remaining user journeys are complete.

## Choose a safe connection

For access on the controller itself, bind to genuine loopback:

```toml
[controller]
bind_host = "127.0.0.1"
port = 8793
```

Open `http://127.0.0.1:8793/app` on that computer. The request must come from
loopback, use a loopback hostname, and match the configured port. An arbitrary
hostname that resolves to loopback is not enough. This binding also prevents
remote display nodes from reaching the catalog. Do not apply it to an existing
wall deployment without a separate network plan.

For a headless controller bound to loopback, forward the port with SSH:

```bash
ssh -N -L 8793:127.0.0.1:8793 <user>@<controller>
```

Open the loopback URL on the client computer. Keep the forwarding process
running. A phone needs an SSH client with local forwarding, or private HTTPS.

For phones and tablets, use an existing trusted private HTTPS reverse proxy:

```toml
[controller]
owner_allowed_origins = ["https://inky.example.test"]
trusted_proxy_addresses = ["192.0.2.10"]
```

Replace the example values. The proxy must preserve the original `Host`, set
`X-Forwarded-Proto` from its actual connection, and replace client-supplied
forwarding headers with verified values. Inky ignores forwarding headers from
every other address. Wildcards and address ranges are not accepted.

Route `/app`, `/app/*`, `/v1/access`, and `/v1/owner/*` through the private
endpoint. Keep catalog, asset, health, embed, and telemetry routes within their
existing trusted network boundary. Forwarding every path does not make those
legacy routes authenticated. Keep the proxy-to-controller hop private too.

Use a VPN when away. A LAN address does not become reachable everywhere because
the phone uses a Home Assistant companion app. Never ignore a certificate
warning. A private certificate authority needs deliberate trust installation
on each client. Transfer its root through an authenticated channel, verify
its fingerprint, protect the signing key, and document trust removal. That
optional recipe remains a release qualification gate.

## Establish the owner

Install the controller extra. Keep configuration and state private. Then run:

```bash
inky-bird-frame access bootstrap --config <config.toml>
```

The JSON result gives a private file path, not the secret. Read that file on
the trusted controller. It uses mode `0600`. The secret expires after ten
minutes and can establish the owner only once. Successful setup removes the
file. A new bootstrap command replaces an unused secret.

Open `/app` through the safe connection. Enter the secret and choose a
passphrase of at least 15 characters. Password managers can fill the sign-in
form. Never put either secret in a URL, command-line argument, routine log,
screenshot, or issue.

The controller stores an Argon2id passphrase hash using RFC 9106's low-memory
profile. Session secrets are stored only as hashes. Access files use mode
`0600` in a mode-`0700` directory. Insecure permissions and symlinks fail closed.

## Sessions and recovery

An owner session expires after 30 minutes without an owner action, or eight
hours after sign-in. Polling does not extend it. Expiry and sign-out revoke
the session. HTTPS cookies use `Secure`, `HttpOnly`, `SameSite=Strict`, and
the `__Host-` prefix. Local HTTP uses a separate host-only cookie.

After five failed sign-ins within 15 minutes, sign-in pauses until that window
clears. The allowance survives restarts. Password checks run one at a time to
bound memory use. At most 16 owner sessions coexist; sign out on an old device
before opening more.

Check setup without showing credentials:

```bash
inky-bird-frame access status --config <config.toml>
```

If you lose the passphrase, regain local operator access and run:

```bash
inky-bird-frame access reset --config <config.toml> --yes
```

Reset removes owner access, revokes owner sessions, and writes a new setup
secret. It preserves source authentication, history, collections, artwork,
and generation state. It does not repair corrupt state or weaken permissions.
Restore the matching private backup if state is corrupt.

Backups containing `web-access/` are credential-sensitive. Keep them private.
Do not publish this state or include it in collection exports.

## Verification boundary

Tests cover loopback and HTTPS-origin rules, proxy handling, forged origins,
request-size limits, private files, atomic writes, concurrent login limits,
session expiry, and catalog compatibility. They do not prove certificate trust
on a real phone or tablet. That check must pass before the remaining interface
depends on remote secure access.

Security basis: [OWASP password storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
and [session management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html).
