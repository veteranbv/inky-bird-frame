# Set up and sign in to Inky

On a prepared new installation, open Inky on your home network and create a
username and password. No email, setup code, online account or password-manager
subscription is required. The first account owns this installation. Finish
setup before sharing access. Existing installations create that first account
locally, so a visitor cannot claim your retained birds during an upgrade.

This development candidate includes the [browser journal](journal.md), source
setup, history imports, collections, plate work and [scoped presentation](presentation.md).
It is not a published 1.0 release. Qualification and exact-head review remain
required before a production upgrade.

## Ordinary home-network setup

1. Follow [new native setup](settings.md#start-with-approved-art), or the [Docker guide](docker.md#2-create-private-configuration), including its one-time new-installation preparation.
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
owner_access_mode = "local"
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

## What changes on an existing installation

The browser journal adds a private owner account. It does not replace your
configuration, provider credentials, detection history, plates, saved birds,
generation limits or display settings. Your existing schedules keep running.
The older catalog and dashboard routes retain their trusted-network contract;
the new owner account does not put them behind a login.

Before updating from v0.9.7, follow the [backup guide](backup.md) and keep the
matching release with that backup. Use the [native update](installation.md#update)
or [Docker update](docker.md#update) procedure for the exact reviewed candidate.
There is no published 1.0 release yet. Do not use this development branch as
an unattended production upgrade.

After updating, check controller and display health. An existing owner keeps
the same account. If no owner exists, native interactive setup asks for a
username and a password with hidden entry. You can also create it yourself:

~~~bash
inky-bird-frame access setup --config <config.toml>
~~~

For Docker, run this from a private interactive terminal:

~~~bash
docker compose exec controller inky-bird-frame access setup --config /data/config.toml
~~~

Then open `/app` and sign in. This command creates an owner only if none exists.
It cannot replace an account, including one created while you enter credentials.
It never accepts a password argument or password environment variable.

An unattended upgrade does not wait for a prompt. Catalog serving, displays
and scheduled work continue. The new owner controls stay locked until you run
local setup; the page explains how. This also applies to an automatic pull of
`:latest`. Pin a release tag or digest when you want control over upgrade timing.
An update does not run new-installation preparation automatically.

Restart the controller and confirm the same account still works. No source
reconnection or detection import is needed merely because you upgraded. Import
new records only when you deliberately want to update history.

Browser account creation requires a private authorization written during
explicit new-installation preparation. Native `config init` writes it only
when the configuration's directory contains no prior runtime data. All runtime
paths must stay inside that directory. Docker's guide prepares the empty volume
before catalog bootstrap. Any other file or directory, even an empty or unknown
one, keeps browser setup closed. Use local setup instead; never delete bird
data or access state to make an installation look new. Losing an access file,
restarting, config sync and an expired pilot setup code do not reopen setup.

An upgrade does not opt you into HTTPS or Authentik. Keep ordinary local login,
or follow the advanced steps below. An existing named HTTPS origin keeps
HTTPS-only access unless you deliberately configure a local-mode downgrade.

For a failed trial, stop the candidate and restore the matching old runtime
and private backup. Do not mix a newer state directory with an older release.
Owner access is one part of the browser journal. Check bird browsing, trips
and source setup as separate tasks, not just a successful account sign-in.

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
An incorrect confirmation password keeps your edits on screen so you can retry.
SSO confirmation replaces the current sign-in without using another device slot
or extending its original expiry. The provider round trip clears unsaved edits;
repeat your change after returning to Account and access.

Five failed password checks within 15 minutes pause further checks until the
window clears. The allowance survives restarts. Checks run one at a time to
bound password-hashing memory. At most 16 owner logins coexist. The pause applies
across clients, including the owner's password confirmation. Restrict who can
reach the service.

The controller stores an Argon2id password hash using RFC 9106's low-memory
profile. It stores browser secrets only as hashes. Private access files use
mode 0600 in a mode-0700 directory. Unsafe permissions, symlinks and corrupt
state fail closed. HTTPS cookies use Secure, HttpOnly, SameSite=Strict and the
__Host- prefix. Local HTTP uses a separate host-only cookie.
Browser cookies are not scoped by port. Another service on the same hostname
can receive the cookie. Use a dedicated hostname when other services share
the computer and are outside your trust boundary.

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
If you configured `owner_subject`, that managed identity still permits SSO.
Remove it from the managed configuration and restart if you need to cut off a
compromised identity. Local recovery clears Inky's stored link, not that setting.

For SSO-only mode, set sso_only = false in the managed configuration and restart
before using the recovered password. Recovery deliberately invalidates the
previous SSO test and identity. Relink and test before restoring SSO-only mode.

Recovery does not repair corrupt state or bypass private-file checks. Restore
a matching private backup for corruption. Backups containing web-access are
credential-sensitive. Never publish them or include them in collection exports.

Existing pilot accounts without a username can still sign in with their
original password. Use Account and access to choose a username and password.
Existing pilot sessions retain their original 30-minute/eight-hour limits.
The earlier `access bootstrap` command remains for pilot compatibility.
An existing bootstrap reservation, including an expired one, closes ordinary
browser account creation. Use local `access setup` if no owner exists, or
`access recover` to replace an existing owner;
do not delete the reservation or access state to reopen setup.
`access reset` now aliases interactive recovery. It does not generate a code or
leave account creation open. Headless reset scripts must change to a deliberate
private-terminal recovery. Neither command is needed for ordinary setup.

## Advanced: HTTPS through a reverse proxy

A reverse proxy receives browser requests and forwards them to Inky. You can
use your existing proxy; Inky does not require one for ordinary local setup.

| Your setup | Recommended path | Verification so far |
| --- | --- | --- |
| Existing Traefik homelab | Keep Traefik and its certificate setup. Use the private route below. | Traefik 3.7.14 completed real Authentik owner linking, sign-in, confirmation, denial and logout. Callback-log privacy passed a forced upstream failure. |
| New optional HTTPS setup | Caddy can issue a private certificate. Each device must trust it deliberately. | Caddy 2.11.7 completed real Authentik sign-in. Separate tests checked certificate rejection, restart and callback error-log privacy. |
| Existing Nginx | Keep Nginx with the private route and logging policy below. | Nginx 1.30.6 completed the same real sign-in flow, header checks, blocked routes and upstream-failure privacy test. |
| Nginx Proxy Manager | Do not use a location-only logging override for OIDC. | Version 2.16.0 passed sign-in, but a malformed request retained synthetic callback query values in its server error log. It does not pass this candidate's privacy gate. |

These checks used a disposable identity service, synthetic accounts and verified
TLS in Chromium. They qualify the access checkpoint, not the finished journal,
every phone's certificate trust or your installation. Repeat the checks after
changing proxy versions, routes or logging policy.

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

Route `/app`, `/app/*`, `/screen`, `/v1/access`, `/v1/owner/*`,
`/v1/screen/*`, `/v1/integrations/*` and `/v1/dashboard/*` through the private
endpoint. Dashboard art stays off until the owner explicitly enables it.
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
      rule: "Host(`inky.example.test`) && (Path(`/app`) || PathPrefix(`/app/`) || Path(`/screen`) || Path(`/v1/access`) || PathPrefix(`/v1/screen/`) || PathPrefix(`/v1/integrations/`) || PathPrefix(`/v1/dashboard/`) || (PathPrefix(`/v1/owner/`) && !Path(`/v1/owner/oidc/callback`)))"
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
Direct TLS changes the entire controller listener, including the catalog,
images, health and display telemetry. Update each display node's `controller_url`
to the HTTPS address. For a private CA, configure the display service's
`SSL_CERT_FILE` with the authenticated public root too. Existing HTTP clients
will not work on that port. Use a reverse proxy if you want owner HTTPS while
keeping the private display listener on HTTP.
Protect the key with mode 0600 and let only the service user read it.
Use a regular key file, not a symlink to a certificate manager's live directory.
Startup checks both files and their match. Invalid or unreadable files stop
startup; Inky does not start an HTTP replacement. Update the files and restart
Inky after certificate renewal. Inky does not issue or renew certificates.

For Docker, mount the files read-only through a private Compose override.
The service runs as UID 10001. Arrange file ownership so that user can read the
key without making it public. The bundled health check reads the configuration
and selects HTTPS itself. Configure normal certificate trust, resolve the
listener hostname inside the container, and include its exact origin in
owner_allowed_origins. Never use an unverified SSL context. Doctor validates
the same hostname and certificate chain.

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
    @owner path /app /app/* /screen /v1/access /v1/owner/* /v1/screen/* /v1/integrations/* /v1/dashboard/*
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
privacy. A further isolated Caddy test completed the real Authentik code
exchange, linked-owner sign-in, unlinked/denied-account rejection and logout.
These tests did not install host trust or test your phone.

## Advanced: Nginx

Keep this server private. Replace the example hostname, certificate paths and
controller address. Trust only the exact Nginx address in Inky's configuration.
Put `log_format` inside Nginx's `http` block, outside the `server` block:

~~~nginx
log_format inky_status '$status';

server {
    listen 443 ssl;
    server_name inky.example.test;
    ssl_certificate /path/to/certificate.pem;
    ssl_certificate_key /path/to/private-key.pem;

    access_log /var/log/nginx/inky-status.log inky_status;
    error_log /dev/null;

    location ~ ^/(app(/(app\.js|journal\.js|settings\.js|imports\.js|frame-tools\.js|presentation\.js|viewer\.js|app\.css|access-guide))?/?|screen|v1/access|v1/(owner|screen|integrations|dashboard)/.*)$ {
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_pass http://127.0.0.1:8793;
    }
    location / { return 404; }
}
~~~

Check `nginx -t` before reloading. Keep the status log private and under your
existing bounded rotation policy. It records response status only, not URLs,
headers or client addresses. Nginx error messages can include the original
callback query. This server therefore suppresses request error logs at the
server level, including errors before location selection. Startup configuration
checks and query-free status logging remain available. Do not enable request
debug logging for this server.

See the official [Nginx access-log](https://nginx.org/en/docs/http/ngx_http_log_module.html)
and [error-log](https://nginx.org/en/docs/ngx_core_module.html#error_log) rules.
If your installation requires detailed request error logs, choose a proxy with
verified field redaction instead of silently recording sign-in credentials.

### Nginx Proxy Manager limitation

Nginx Proxy Manager generates a server-level error log for each proxy host.
An Advanced-tab `location` override can protect normal callbacks and upstream
failures, but it cannot protect errors that occur before a location is chosen.
An isolated malformed-header test demonstrated this with synthetic values.
The [generated host template](https://github.com/NginxProxyManager/nginx-proxy-manager/blob/v2.16.0/backend/templates/proxy_host.conf)
and [custom configuration hooks](https://nginxproxymanager.com/advanced-config/#custom-nginx-configurations)
explain that boundary.

Do not assume that passing ordinary login is enough. This candidate does not
provide a qualified Nginx Proxy Manager OIDC recipe. Use Caddy, Traefik or the
server-level Nginx recipe for the owner endpoint. Do not change shared templates
or other applications' logging to work around this limitation without a separate
review.

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
For native setup, supply that variable in the setup process environment once.
The installer keeps only this named secret in a mode-0600 native-access.env
beside config.toml. It reuses that private cache on later setup runs unless you
supply a replacement value. Linux loads it only for the controller service;
macOS copies it only into the private serving LaunchAgent. Scheduled jobs do
not receive it. The secret survives service restarts and reboots. Back up the
cache and protect the serving plist as credential-sensitive files. Never
commit either file. 1Password and other secret managers are optional ways to
inject the initial value, not user requirements.

For an interactive Bash or Zsh shell, enter it without putting the value in
shell history or process arguments, then run the normal native setup:

~~~bash
printf 'OIDC client secret: '
read -r -s INKY_OIDC_CLIENT_SECRET
printf '\n'
export INKY_OIDC_CLIENT_SECRET
inky-bird-frame setup controller --config /path/to/config.toml --yes
unset INKY_OIDC_CLIENT_SECRET
~~~

Use the variable named by client_secret_env if you chose a different name.
To rotate the secret, supply its replacement and rerun setup. Keep the generated
cache private; do not source or manually edit it. Removing OIDC from the
configuration removes the service injection, not the retained private cache.
You can remove that one cache file after checking your rollback and backup needs.

Sign in locally. Open Account and access > Advanced access and select Link my
Authentik account. Authenticate at Authentik. Inky links the verified issuer
and subject to the existing owner. It never promotes another user based on
email, display name, groups or being the first person to use SSO.

Linking replaces this browser's sign-in cookie without extending its original
expiry or changing its Stay signed in choice. It also signs out earlier
single-sign-on sessions, including a replaced provider account. Other local
password sessions stay signed in. Repeated verification does not consume
another device slot.

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

Inky asks the provider for the time it last checked your identity. That check
must be less than ten minutes old. Account linking and confirmation also ask
the provider to show its sign-in step again. Inky preserves the signed check
time; a new provider token does not restart the ten-minute window. Keep both
servers' clocks synchronized. A provider that omits this check time cannot
complete sign-in.

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
