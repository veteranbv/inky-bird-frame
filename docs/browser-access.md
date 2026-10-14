# Set up and sign in to Inky

Open Inky on your home network and create a username and password. No email,
setup code, online account or password-manager subscription is required.
The first account owns this installation. Inky closes account creation once
that account exists. Finish setup before sharing access.

This worktree currently provides account setup and access controls, not the
complete bird journal. The remaining interface is tracked in [issue #310](https://github.com/veteranbv/inky-bird-frame/issues/310).
Do not treat this access checkpoint as a release-ready full application.

## Ordinary home-network setup

1. Install the controller extra, or use the controller container.
2. Keep the controller on your trusted home network.
3. Open the controller's local address followed by /app.
4. Choose a username and an Inky password of at least 15 characters.
5. Save the password and complete account creation.

A username uses 3–64 ASCII letters, numbers, dots, hyphens or underscores.
Letter case does not matter. A password can contain Unicode and unrelated
words. Your bird-service passwords and API keys are separate.

For a new installation, local mode is the default:

~~~toml
[controller]
bind_host = "0.0.0.0"
port = 8793
owner_access_mode = "local"
~~~

Open the actual local IP shown by your installation, not a guessed hostname.
For example, http://192.168.1.50:8793/app works only if that is your controller's
address. Direct IP requests must match the address and port of the accepted
server socket. Local peers must use loopback, RFC 1918 IPv4 or IPv6 unique-local
addresses. Reserved ranges and link-local addresses are not treated as trusted.
A private client address does not prove that a router cannot expose the server.

A friendly hostname is optional. Add its exact origin explicitly:

~~~toml
owner_allowed_origins = ["http://inky.home:8793"]
~~~

An origin is the scheme, host and port, without a path. Arbitrary hostnames are
rejected even when they resolve to the controller. This protects against a
website that changes its DNS to point at your local server.

**HTTP does not encrypt passwords, browser-session credentials, provider
credentials or private history in transit.** Use a unique Inky password.
Only use HTTP on a trusted home network. Do not open a router port, publish
a public tunnel or expose the legacy catalog routes. Inky does none of these
automatically. HTTPS is available when you need encryption.

## Sessions and private files

Normal sign-in uses a browser-session cookie. It expires after 12 hours without
an owner action, or 24 hours after sign-in. Closing the browser usually removes
the cookie; browser session-restore settings can preserve it.

**Stay signed in on this device** is off by default. It permits seven days
without an owner action, with a hard limit of 30 days. Use it only on a private
device. Background polling never extends either policy. These limits provide
a working-day login and bounded return visits, not a permanent owner token.

Account and access lists each login without collecting browser fingerprints.
You can revoke an individual login. Password/account changes revoke every
other login. Sensitive access changes require authentication within the last
ten minutes. Older password sessions use a password confirmation. Older SSO
sessions require another provider sign-in.

Five failed password checks within 15 minutes pause further checks until the
window clears. The allowance survives restarts. Checks run one at a time to
bound password-hashing memory. At most 16 owner logins coexist.

The controller stores an Argon2id password hash using RFC 9106's low-memory
profile. It stores browser secrets only as hashes. Private access files use
mode 0600 in a mode-0700 directory. Unsafe permissions, symlinks and corrupt
state fail closed. HTTPS cookies use Secure, HttpOnly, SameSite=Strict and the
__Host- prefix. Local HTTP uses a separate host-only cookie.

## Recover a forgotten password

Use a private interactive terminal on the computer running Inky:

~~~bash
inky-bird-frame access recover --config <config.toml>
~~~

In a source checkout, prefix the command with uv run. For Docker:

~~~bash
docker compose exec controller inky-bird-frame access recover \
  --config /data/config.toml
~~~

Recovery first asks you to confirm. The optional --yes flag skips only that
confirmation, not the private-terminal requirement. It then asks for a username
and a new password in a hidden prompt. It never
accepts a password argument. Recovery replaces credentials, revokes all logins
and unlinks single sign-on. It never leaves network account creation open.
Your bird-service accounts, history, plates, collections and work remain.

For SSO-only mode, set sso_only = false in the managed configuration and restart
before using the recovered password. Recovery deliberately invalidates the
previous SSO test and identity. Relink and test before restoring SSO-only mode.

Recovery does not repair corrupt state or bypass private-file checks. Restore
a matching private backup for corruption. Backups containing web-access are
credential-sensitive. Never publish them or include them in collection exports.

Existing pilot accounts without a username can still sign in with their
original password. Use Account and access to choose a username and password.
Existing pilot sessions retain their original 30-minute/eight-hour limits.
The earlier access bootstrap/reset commands remain available for pilot
compatibility; they are not the ordinary setup or recovery path.

## Advanced: HTTPS through a reverse proxy

A reverse proxy receives browser requests and forwards them to Inky. You can
use your existing proxy; Inky does not require one for ordinary local setup.

| Your setup | Recommended path | Verification so far |
| --- | --- | --- |
| Existing Traefik homelab | Keep Traefik and its certificate setup. Use the private route below. | Live private reference route, headers, blocked paths and callback-log privacy checked. Real owner SSO remains a separate gate. |
| New optional HTTPS setup | Caddy can issue a private certificate. You must choose how each device trusts it. | Isolated Caddy 2.11.7 HTTPS, restart, blocked paths, spoof rejection and error-log privacy checked. No device trust was installed. |
| Existing Nginx, Nginx Proxy Manager or another HTTP proxy | Keep your proxy if it can meet the route, header and logging rules below. | Same Inky transport contract; these products have not had a separate live qualification in this worktree. |

These are proxy setups, not separate Inky providers or login systems. Your
proxy supplies HTTPS. Inky still owns account setup, sign-in and optional OIDC.
Do not add proxy forward-auth when using Inky's native Authentik sign-in.

~~~toml
[controller]
owner_access_mode = "https-only"
owner_allowed_origins = ["https://inky.example.test"]
trusted_proxy_addresses = ["192.0.2.10"]
~~~

Replace the example host and proxy address. Trust only each proxy's exact IP.
Wildcards and networks are rejected. The proxy must preserve Host, set
X-Forwarded-Proto from the real connection and replace client-supplied
forwarding headers. Inky ignores headers from all other peers.

Route /app, /app/*, /v1/access and /v1/owner/* through the private endpoint.
Keep catalog, assets, health, embed and telemetry within their separate
trusted-network boundary. Owner sign-in does not authenticate those legacy
routes. Keep the proxy-to-controller hop private too.

HTTPS-only mode rejects insecure owner access, including direct loopback or
backend access. It never falls back to HTTP after a TLS or provider error.
Existing configurations with owner_allowed_origins and no access mode retain
HTTPS-only mode on upgrade. Choosing local mode is a deliberate change.

### Traefik file-provider route

Use your existing private HTTPS entry point and certificate configuration.
This dynamic example assumes the entry point is named `websecure`. Change the
example addresses to your controller and intended private client networks:

~~~yaml
http:
  routers:
    inky-owner:
      entryPoints: [websecure]
      rule: "Host(`inky.example.test`) && (Path(`/app`) || PathPrefix(`/app/`) || Path(`/v1/access`) || (PathPrefix(`/v1/owner/`) && !Path(`/v1/owner/oidc/callback`)))"
      middlewares: [inky-private]
      service: inky-owner
      tls: {}
    inky-owner-callback:
      entryPoints: [websecure]
      rule: "Host(`inky.example.test`) && Method(`GET`) && Path(`/v1/owner/oidc/callback`)"
      middlewares: [inky-private]
      service: inky-owner
      tls: {}
      observability:
        accessLogs: false
        tracing: false
  services:
    inky-owner:
      loadBalancer:
        passHostHeader: true
        servers:
          - url: http://192.0.2.20:8793
  middlewares:
    inky-private:
      ipAllowList:
        sourceRange: [192.168.1.0/24]
~~~

Set `trusted_proxy_addresses` to the exact address Inky sees for Traefik.
Do not enable `forwardedHeaders.insecure` on the entry point. If Traefik itself
sits behind another proxy, configure only that proxy's trusted addresses.
See [Traefik's forwarding-header settings](https://doc.traefik.io/traefik/reference/install-configuration/entrypoints/#forwarded-headers).

The callback's separate router prevents its sign-in query from entering access
logs or traces. Check your installed Traefik version supports
[per-router logging controls](https://doc.traefik.io/traefik/reference/routing-configuration/http/routing/observability/).
Keep routine error logs. Verify a synthetic callback marker is absent from logs
before using a real identity. Never publish backend ports, expose the entire
controller through a host-only rule, or bypass a certificate warning.

## Advanced: supply a certificate

TLS is the encryption used by HTTPS. To let Inky serve HTTPS directly:

~~~toml
[controller]
owner_access_mode = "https-only"
owner_allowed_origins = ["https://inky.example.test:8793"]
tls_certificate = "private/fullchain.pem"
tls_private_key = "private/key.pem"
~~~

Supply a certificate for that hostname and its matching private key.
Protect the key with mode 0600 and let only the service user read it.
Startup checks both files and their match. Invalid or unreadable files stop
startup; Inky does not start an HTTP replacement. Update the files and restart
Inky after certificate renewal. Inky does not issue or renew certificates.

For Docker, mount the files read-only through a private Compose override.
The service runs as UID 10001. Arrange file ownership so that user can read the
key without making it public. Override the default HTTP health check with an
HTTPS check that validates the hostname and chain. Never use an unverified
SSL context. Doctor uses a configured HTTPS origin on the controller port
when available, and validates normal certificate trust.

A private certificate authority requires deliberate trust on each client and
on the controller used by doctor. Transfer its root through an authenticated
channel and verify the fingerprint. Do not install trust automatically, share
the signing key or teach users to ignore browser warnings.

## Advanced: optional Caddy private certificates

[Caddy's local HTTPS guide](https://caddyserver.com/docs/automatic-https#local-https)
explains its private certificate authority. Use this only if you choose to
manage certificate trust. Ordinary home-network setup does not require it.

A Caddy instance on the controller can use this Caddyfile:

~~~caddyfile
{
    skip_install_trust
    log default {
        format filter {
            wrap json
            fields {
                request>uri delete
                request>headers delete
                resp_headers delete
            }
        }
    }
}

https://inky.home {
    tls internal
    @owner path /app /app/* /v1/access /v1/owner/*
    handle @owner {
        reverse_proxy 127.0.0.1:8793
    }
    handle {
        respond "Not found" 404
    }
}
~~~

Configure Inky's exact HTTPS origin and trust 127.0.0.1 as the proxy. Keep the
proxy listener restricted to your private network. For a Docker Caddy container,
use the controller service name as the upstream and trust the container's
explicit private address instead. Do not trust an entire container network.

The global log filter also protects gateway-error logs, which can otherwise
include a callback query. It removes request URLs and headers but retains error
messages and status. The example does not enable access logs. If you add another
logger, apply the same filter there. Merge this block into an existing global
block; Caddy permits only one. See [Caddy's log filters](https://caddyserver.com/docs/caddyfile/directives/log#filter).
`skip_install_trust` prevents automatic trust installation on the proxy host.

Caddy stores its CA in its persistent data directory. Preserve that directory,
protect its signing key and back it up privately. Install only the root
certificate, not the private key, on clients after checking its fingerprint.
Phone trust may need a separate full-trust setting. Removing the root from
each device reverses client trust; stop the proxy and remove its DNS/routing
entry separately. Do not delete your only CA backup before recovering clients.

An isolated test checked HTTPS, exact proxy trust, blocked legacy paths,
certificate rejection, persistent CA across restart and callback error-log
privacy. It used synthetic data and request-scoped certificate trust. It did
not change OS/browser trust, test your phone or complete a real SSO exchange.

## Advanced: Authentik and other OpenID Connect providers

OpenID Connect (OIDC) lets your identity service sign you in. It is optional.
Authentik is the reference provider. Inky never asks for your Authentik password
and does not need a second Inky password after successful SSO sign-in.

Create a confidential OAuth2/OpenID provider and application. Allow only the
intended owner at the identity service. Use Authorization Code, PKCE S256,
an asymmetric RS256 or ES256 signing key and the openid scope. Do not request
offline_access. Set authorization_code explicitly when using the Authentik API.
Use a strict, exact HTTPS callback; never leave the allowed callback empty.

~~~toml
[controller.oidc]
issuer = "https://auth.example.test/application/o/inky/"
client_id = "your-client-id"
client_secret_env = "INKY_OIDC_CLIENT_SECRET"
callback_url = "https://inky.example.test/v1/owner/oidc/callback"
label = "Authentik"
sso_only = false
~~~

The callback's origin must appear in owner_allowed_origins. Preserve the exact
issuer, including its trailing slash. Supply the client secret to the controller
process through the named environment variable. For Docker, put it in a private
mode-0600 access.env file, or set INKY_ACCESS_ENV to your private file. The serving
container receives that file separately from scheduler/provider credentials.
Native services need that variable in their service environment. 1Password
and other secret managers are optional ways to inject it, not user requirements.

Sign in locally. Open Account and access > Advanced access and select Link my
Authentik account. Authenticate at Authentik. Inky links the verified issuer
and subject to the existing owner. It never promotes another user based on
email, display name, groups or being the first person to use SSO.

A managed headless installation can set owner_subject to the owner's exact
verified subject. This is private configuration, not an email address or group.
It does not create an owner or remove the need for the local recovery password.

Test sign-out and provider sign-in before setting sso_only = true. Inky refuses
that mode until a successful verified exchange is recorded for the exact
provider/owner configuration. Enabling it revokes local-password sessions.
Operator recovery remains available.

The library verifies signature, issuer, audience, nonce and token lifetime.
The controller bounds responses/timeouts, requires HTTPS endpoints and fetches
fresh signing keys for each exchange. Pending requests expire in ten minutes,
work once and are tied to that browser. The redirect cookie permits only the
provider's return journey; normal owner cookies remain SameSite=Strict.
Provider tokens stay in memory and are not retained as login refresh tokens.
Proxy logs sit outside Inky's controls. Do not log passwords, request bodies,
Authorization, Cookie or Set-Cookie headers. The `/v1/owner/oidc/callback` query
contains a short-lived sign-in code. Drop its query parameters from proxy logs,
or use a separate callback route with access logging and tracing off. Keep
ordinary status and error logs without these values. Do not enable OAuth debug
payload logging on a live installation.

A provider outage disables the SSO button with an explanation. Local login
works unless you deliberately chose SSO-only mode. Signing out of Inky revokes
only that Inky login. It does not sign out other identity-service applications.
Disabling an identity-service account does not instantly revoke an existing
Inky login; use Inky's device revocation or local recovery when needed.

Changing the client, issuer, callback or managed owner identity invalidates
old SSO sessions. Relinking requires recent owner authentication, verifies the
replacement and revokes old SSO sessions. Unlinking also invalidates pending
requests, including an exchange already in progress.

## Advanced: private remote access

Connect your phone or computer through your own VPN before opening the private
Inky address. Do not assume a private hostname works away from home. Confirm
your VPN routes and DNS. HTTPS remains recommended across networks you do not
control. Inky never adds a VPN, opens a router port or publishes a tunnel.

An HTTPS dashboard may block an HTTP Inky image or iframe. Some browser
features also need a secure connection. Use the existing private HTTPS image
route or optional HTTPS setup. These browser rules do not prevent ordinary
local account setup.

## Verify before trusting an installation

Complete setup, reload, sign out and sign back in. Confirm the fields work on
your real phone and with your password manager. Verify the displayed address
and transport. Do not reset a real owner to repeat a setup test.

For HTTPS, verify the browser trusts the certificate without bypassing warnings.
For SSO, verify the linked account succeeds, an unlinked provider user is denied,
provider errors are understandable and local recovery still works. Test process
restart and signing-key rotation in an isolated installation.

Automated tests use synthetic credentials and signed tokens. Those tests do
not prove your phone's certificate trust, real Authentik policy or a physical
display update. Live reference-provider and multi-browser checks remain
release gates, not claims made by unit-test counts.
