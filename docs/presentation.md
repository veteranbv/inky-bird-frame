# Screens, wall views and dashboards

Inky has three distinct ways to show approved art. They use the same catalog,
but they do not share management authority. This guide describes the 1.0
candidate; wait for its reviewed release before changing a working deployment.

| Destination | Access | What it can do |
| --- | --- | --- |
| Owner Frame | Your normal Inky login | Play a chosen view; open its bird details |
| Paired browser screen | Separate read-only cookie | Show approved plates from one permitted view |
| Dashboard art | Explicit trusted-network opt-in | Show selected approved art without owner cookies |
| Status sensors | Separate header credential | Read aggregate readiness, version and counts |
| Physical wall | Existing trusted-network display role | Pull the compatible or selected-wall catalog; report panel success |

Nothing here exposes Inky to the internet or changes router rules. Keep its
legacy catalog, assets and display-health routes private. Owner sign-in is not
protection for those older routes.

## Owner Frame

Open Home or a collection and choose **Play**. Previous, Next, Pause, Shuffle,
Details and Exit work without changing the wall. Frame fits the complete
portrait. Zoom is deliberate and allows panning; it does not alter the artwork.
Arrow keys and touch swipes change plates. Space toggles playback when focus is
not on an interactive control. Opening Details pauses playback.

Fullscreen and **Keep screen awake** appear only when the browser supports
them. Wake lock normally requires HTTPS. A browser can release it when hidden,
when battery policy changes, or when the page closes. Normal sleep settings
apply when the request fails. Controls reappear on interaction and remain
visible while focused. Background polling does not renew an owner session.

After owner expiry, Frame clears private labels and freezes the last already
visible approved image. Sign in to continue. Explicit owner logout clears the
presentation. For a tablet that should keep running, pair it instead.

## Pair a read-only screen

1. Finish owner setup first.
2. Open the controller's address followed by `/screen` on the intended screen.
3. Choose **Pair this screen**. It shows an eight-character recognition code.
4. On your signed-in owner device, open **Manage → Displays**. Reload to see a
   new request. Match its code on the actual screen.
5. Give it a name, choose one view, choose its expiry and confirm the match.

The code identifies a request. It cannot approve one or sign anyone in as the
owner. Requests expire after ten minutes. Inky keeps at most eight pending
requests and 32 current screen/status credentials in total. New unrelated
pairing requests are rate-limited to one per 15 seconds. Existing requests can
resume without consuming another slot.

The screen receives a random 256-bit secret in an HttpOnly, same-site cookie.
HTTPS uses a Secure host-only cookie. No credential goes in a link, QR code,
browser storage or routine logs. Local HTTP has the same trusted-network and
interception limits as local owner access; use HTTPS when you need encryption.

Access defaults to 90 days; choose 1–365 days. Polling does not extend expiry.
Displays shows the permitted view, expiry and last connection at hour precision.
**Revoke screen** takes effect on its next request, normally within 15 seconds
while visible. A revoked online screen clears its artwork. Deleted or newly
empty views stay empty. A fresh view that withdraws a plate clears that plate
even if its replacement fails to load. Network failure keeps only the last
image whose permission has not been authoritatively withdrawn.

An offline or suspended screen cannot receive revocation until it reconnects.
Inky cannot erase already photographed, downloaded or offline pixels. **Forget
this screen** removes its browser cookie. Also revoke the grant in Displays if
the device was lost or its credential might have been copied.

## Choose the physical wall view

Existing display configuration keeps using `/v1/catalog`, with its current and
Saved birds meaning. Changing a named collection does not change that route.

To opt into owner-selected wall views, the operator changes the display's
configuration once:

```toml
[display_node]
controller_url = "http://inky-controller:8793/wall"
```

Use your actual reachable private address. Retain HTTPS and certificate trust
when required. Validate the file, rerun display setup and restart its service
through the normal [installation procedure](installation.md). The namespace
serves `/wall/v1/catalog`, its selected assets and the existing narrow physical
telemetry contract. No display firmware or new controller is required.

Then choose and confirm the view in **Manage → Displays → Physical wall panel**.
The display applies it at its next scheduled refresh. It still owns shuffle,
rotation interval and panel rendering. A fixed trip does not admit unrelated
home detections. No browser action claims an immediate panel update. Home and
Displays show only the last confirmed physical success.

Undo this opt-in by restoring the previous controller URL and rerunning display
setup. Named collections, source history and approved plates stay intact.

## Home Assistant and Homepage

Open **Manage → Home Assistant and Homepage**. Art is off initially. Choose a
view and read its exposure notice before enabling it. The static art page rotates
within that selected view once a minute. It contains no scripts, owner cookie,
private collection name, dates, source names or detection details. Its selected
species are still information. Anyone who can reach that permitted network
address can learn them.

Set the parent dashboard's exact origin through the installer:

```toml
[controller]
embed_allowed_origins = ["https://dashboard.example.test"]
```

Restart after changing this field. Framing restrictions tell a browser which
pages may embed the art. They are not authentication or a firewall. Keep the
endpoint on your trusted network or VPN. Do not widen an internet proxy's
owner-path allowlist to include legacy routes or dashboard art.

For a [Home Assistant webpage card](https://www.home-assistant.io/dashboards/iframe/):

```yaml
type: iframe
url: https://inky.example.test/v1/dashboard/art
aspect_ratio: 133%
```

For [Homepage's iframe widget](https://gethomepage.dev/widgets/services/iframe/):

```yaml
widget:
  type: iframe
  name: Inky artwork
  src: https://inky.example.test/v1/dashboard/art
  classes: h-96
  referrerPolicy: no-referrer
  allowScrolling: no
```

Use a trusted certificate and replace the example address. An HTTPS dashboard
cannot embed HTTP Inky. The app warns when its configured HTTPS parent origins
conflict with the current HTTP address. A remote Home Assistant app still needs
network access to Inky; a private VPN or reachable private HTTPS route supplies
that. Server-side sensors can work while a remote phone cannot reach an iframe.
Never turn off certificate verification to hide this distinction.

### Separate status sensors

Create a named status credential on Integrations. Choose its view and expiry.
Copy it once into the dashboard's secret store. Inky stores only its hash.
Reloading loses that displayed copy; revoke and create another if needed.

`GET /v1/integrations/status` requires `Authorization: Bearer <credential>`.
Tokens in query strings are refused. The response has the usual `ok`, `data`
and `schema_version` envelope. Its data is limited to version, service readiness,
approved count, permitted-view ready count, coarse discovery state and panel
health. No identity, private timestamp, collection label, host, queue detail,
history or source credential is included. A status credential cannot read images
or use owner routes.

For Home Assistant's [REST integration](https://www.home-assistant.io/integrations/rest/):

```yaml
rest:
  - resource: https://inky.example.test/v1/integrations/status
    headers:
      Authorization: !secret inky_status_authorization
    scan_interval: 60
    sensor:
      - name: Inky ready plates
        value_template: "{{ value_json.data.view_ready_count }}"
```

In `secrets.yaml`, set `inky_status_authorization` to the complete `Bearer ...`
header value. Keep the file private and leave TLS verification enabled.

For Homepage's [Custom API widget](https://gethomepage.dev/widgets/services/customapi/):

```yaml
widget:
  type: customapi
  url: https://inky.example.test/v1/integrations/status
  refreshInterval: 60000
  headers:
    Authorization: "Bearer {{HOMEPAGE_VAR_INKY_STATUS}}"
  mappings:
    - field: data.view_ready_count
      label: Ready
      format: number
    - field: data.approved_count
      label: Plates
      format: number
    - field: data.version
      label: Version
      format: text
```

Inject `HOMEPAGE_VAR_INKY_STATUS` through Homepage's supported secret environment
mechanism. Never publish the resolved value. Revoke a credential here to stop
its future reads. The compatible older `/health` and featured-plate recipes
remain available on the trusted network; they do not gain authentication.

For notifications, use the existing [Home Assistant destination](notifications.md#home-assistant).
Delivery is independent of art and status sensors. Creating an integration does
not silently create a webhook, enable notifications or trigger a panel refresh.

## Private state and recovery

`state_dir/presentation-access.json` is a bounded, versioned private document,
written atomically under the catalog-state lock with mode `0600`. Inky rejects
symlinks, insecure permissions, malformed state and files larger than 256 KiB.
Owner changes use a revision guard and recent authentication. Polling records
coarse last connection but never renews grants or an owner session.

Back it up with the stopped controller. Restore can restore earlier access;
revoke lost credentials afterward. Do not delete private state merely to hide
an error. Turning dashboard art off stops its route without deleting collections,
provider history or existing plates.
