# Discovery sources

Inky Bird Frame reads public observations, detections from your own acoustic
station through BirdWeather or self-hosted BirdNET-Go, authorized postcard
detections from your own Bird Buddy account, or a combination. Every result
enters the same catalog and review process.

Merlin Bird ID is Cornell's identification app. Its nearby lists are powered by
eBird, so this project uses the documented eBird API rather than trying to
automate Merlin.

## Choose providers

| Source | Credentials | Windows | Best use |
| --- | --- | --- | --- |
| `inaturalist` | None | All | Default setup, historical seeds, and licensed references |
| `ebird` | Personal eBird key | 1, 7, or 30 days | Bird-specific recent public sightings |
| `birdweather` | BirdWeather station token | All | Species acoustically detected by one station |
| `birdnet-go` | Local BirdNET-Go base URL | All | Species acoustically detected by a self-hosted station |
| `birdbuddy` | Authorized Bird Buddy account | All after setup | Postcard species from one feeder, plus optional manual sightings |

Select any combination with a TOML array. Each configured provider runs
independently.

## Configure a discovery location

iNaturalist and eBird need a point and radius. Choose exactly one location
form; BirdWeather-, BirdNET-Go-, and Bird Buddy-only setups need none.

Direct coordinates are the provider-independent option. They work worldwide,
do not disclose a postal code to a geocoder, and cannot become ambiguous:

```toml
[discovery]
latitude = 0.0
longitude = 0.0
```

For postal-code convenience, use [Geoapify's Postcode API](https://apidocs.geoapify.com/docs/postcode/).
It supports ISO 3166-1 alpha-2 country codes and international postal formats.
Create a Geoapify key and keep it in the private configuration:

```toml
[discovery]
postal_code = "YOUR_POSTAL_CODE"
country_code = "XX"
geoapify_api_key = "YOUR_PRIVATE_GEOAPIFY_API_KEY"
```

For commands you run yourself, `geoapify_api_key_env = "GEOAPIFY_API_KEY"` is
also supported. Managed LaunchAgent and systemd services do not inherit your
installation shell, so use the direct key in their mode-`0600` configuration.
Geoapify receives the configured country and postal code. The application
requires an exact country/postal-code match and rejects multiple coordinate
results rather than guessing. Geoapify permits stored results and requires
attribution; discovery output includes both Geoapify and source attribution.

Existing `zip_code = "12345"` configurations remain supported unchanged through
Zippopotam's US endpoint and need no key. This compatibility path is US-only;
new worldwide configurations should use coordinates or Geoapify. No discovery
location is written to a public catalog or rendered on a plate.

Request a personal key from [eBird](https://ebird.org/api/keygen). Store it in
the private configuration:

```toml
[discovery]
sources = ["inaturalist", "ebird"]
zip_code = "12345"
radius_km = 8
species_limit = 50
window = "last-30-days"
ebird_api_key = "your-personal-api-key"
```

For commands you run yourself, you may replace `ebird_api_key` with
`ebird_api_key_env = "EBIRD_API_KEY"`. Native LaunchAgent and systemd services
do not inherit your installation shell, so those installations need the key in
the private mode-`0600` configuration file.

Keep the configuration outside the checkout with mode `0600`. The application
never writes the key to state, logs, catalog files, or command output.

## BirdWeather station setup

BirdWeather is optional. Create a BirdWeather account and station, then connect
a compatible detector such as BirdNET-Pi by following the official
[BirdNET-Pi integration guide](https://www.birdweather.com/birdnetpi). Copy the
station authentication token into the private controller configuration:

```toml
[discovery]
sources = ["birdweather"]
radius_km = 8
species_limit = 50
window = "last-30-days"
birdweather_token = "your-station-token"
```

Use `sources = ["inaturalist", "ebird", "birdweather"]` and configure both
credentials to query every current provider. For manually invoked commands,
`birdweather_token_env = "BIRDWEATHER_TOKEN"` is also supported. Managed
services require the direct token in the private mode-`0600` file because they
do not inherit the installation shell environment.

The token authenticates one station. The application uses BirdWeather's
documented station-species endpoint, requests only the `avian` classification,
and uses the selected time window. It does not query nearby BirdWeather
stations or infer a station from a discovery location. A BirdWeather-only
configuration does not require coordinates or a postal code.

### Supported boundary

Inky Bird Frame supports:

- reading species names, detection counts, and latest-detection timestamps from
  the authenticated station;
- exact scientific-name matching to the canonical iNaturalist taxon;
- provider-specific health reporting, retries, and notifications;
- approved catalog reuse, generation, review, rotation, and publication; and
- every configured observation window, up to BirdWeather's 100-species API cap.

Inky Bird Frame does not:

- install, configure, update, or monitor BirdNET-Pi or other acoustic detectors;
- configure microphones, recording schedules, storage, retention, or uploads;
- download, proxy, play, retain, or independently review soundscape audio;
- confirm that a machine classification represents a bird physically present;
- correct detector confidence, placement, background-noise, or taxonomy errors;
  or
- submit detections to eBird or iNaturalist.

An acoustic detection is a model classification, not a human-confirmed sighting.
False positives, overlapping calls, recordings, rebroadcast audio, distant
sounds, and detector configuration can affect the result. Inky Bird Frame uses
the station's accepted BirdWeather species summary as supplied. Tune and review
the detector in its own software before relying on those species for display.

## Self-hosted BirdNET-Go setup

BirdNET-Go is optional. Point Inky at a BirdNET-Go server reachable from the
controller:

```toml
[discovery]
sources = ["birdnet-go"]
species_limit = 50
window = "last-30-days"
birdnet_go_url = "http://birdnet-go.local:8080"
```

The URL may use HTTP on a trusted local network or HTTPS through a reverse
proxy. Do not embed credentials, query parameters, or fragments. A BirdNET-Go-
only configuration needs no discovery location.

Inky uses BirdNET-Go's documented read-only
`/api/v2/analytics/species/summary` endpoint. It supplies the selected date
window and species limit, then reads only names, counts, and the last-heard
timestamp. It does not download recordings, access media, change settings, or
control the detector. BirdNET-Go applies the station's configured detection
policy and excludes detections marked false-positive before producing the
summary; Inky does not layer on an unexplained second confidence threshold.

Every scientific name must still resolve exactly to one active iNaturalist bird
species. Custom classifier labels, non-birds, and ambiguous or unmatched names
remain private unresolved diagnostics and cannot trigger plate generation. A
BirdNET-Go failure degrades only this provider; other configured sources keep
running. The legacy singular `source = "all"` retains its existing meaning and
does not silently enable access to a local BirdNET-Go server.

## Bird Buddy setup

Bird Buddy is optional and uses the private, undocumented GraphQL API behind
the Bird Buddy app. It is not an official developer integration and may change
without notice. Bird Buddy's current
[app EULA](https://mybirdbuddy.com/app-eula/) restricts automated and non-app
access. Obtain Bird Buddy's permission for your account before enabling this
provider. Inky Bird Frame cannot grant or verify that permission; you are
responsible for ensuring it remains valid.

Create a dedicated email-and-password guest account and invite it to the feeder
you want Inky to follow. Bird Buddy documents the
[guest invitation flow](https://support.mybirdbuddy.com/hc/en-us/articles/4404431992337-Adding-guest-Feeder-Members-to-your-Birdbuddy).
This is also the supported path when the feeder owner signs in with Apple,
Google, or Facebook. Keep the guest account for this integration rather than
using it interactively, so postcard state stays predictable.

Add the provider without putting credentials in TOML:

```toml
[discovery]
sources = ["inaturalist", "birdbuddy"]
birdbuddy_include_manual_sightings = false
zip_code = "12345"
radius_km = 8
species_limit = 50
window = "last-30-days"
```

Then authenticate once. The interactive command shows the authorization
attestation before asking for credentials:

```bash
inky-bird-frame birdbuddy login --config /path/to/config.toml
inky-bird-frame birdbuddy status --config /path/to/config.toml
```

For noninteractive secret-manager use, inject `INKY_BIRDBUDDY_EMAIL` and
`INKY_BIRDBUDDY_PASSWORD` for that command only and add
`--confirm-authorized-access`. Never put the password on the command line or in
`config.toml`. If the account can see more than one feeder, login reports the
available names and IDs without saving authentication; rerun with the intended
`--feeder-id`.

Login stores only the attestation time, selected feeder, and rotating refresh
token in mode-`0600` controller state. Access tokens remain in memory. The
password and email are never retained. `birdbuddy logout --config
/path/to/config.toml --yes` removes local authentication while preserving
detection history; it does not claim to revoke the remote token.

Bird Buddy rotates its refresh token during every authenticated request, so
even `discover` and seed previews must atomically update authentication state.
Preview commands do not commit detection history or taxonomy cache changes.

The provider reads high-confidence species metadata and media identifiers from
new postcards. It also reads the species, capture time, origin, feeder ID, and
media identifier from confirmed history so a postcard is not missed when
someone confirms it before Inky's next poll. The identifiers link a confirmed
correction back to its cached postcard; they never enter the public catalog.
Inky never requests media URLs or downloads photos, video, or audio.
It does not convert, reanalyze, collect, edit, discard, or share a postcard or
control a feeder. The independent
[pybirdbuddy project](https://github.com/jhansche/pybirdbuddy) demonstrates the
private API's current password and guest-account behavior but is not an
authoritative Bird Buddy API contract and is not a runtime dependency here.

Bird Buddy removes a postcard from the new-postcard feed after it is confirmed.
The controller therefore combines exact postcard history with confirmed
metadata. Exact postcards are deduplicated and counted once per species.
When confirmed history covers every media item on a cached postcard, its
species replace the earlier preview classification. An incomplete match leaves
the cached postcard unchanged rather than guessing.
Confirmed records fill gaps in species presence and supply the newest capture
time, but they do not increase an existing postcard count: one postcard can
contain several media records, and treating each as a visit would inflate the
result. Counts that rely only on confirmed history are conservative lower
bounds.

Manually added app sightings use Bird Buddy's account-level `CUSTOM_ID` origin.
They are not tied to the selected feeder and may have been recorded somewhere
else, so Inky excludes them by default. Include them only when that matches how
you use the account:

```toml
[discovery]
sources = ["birdbuddy"]
birdbuddy_include_manual_sightings = true
```

The choice is reversible. Manual evidence remains in the private history so it
does not need to be fetched again, but it stops affecting discovery as soon as
the option is disabled. Livestream `WATCHING` records and postcard history from
other feeders are always excluded.

Exact postcard events remain in private history for 366 days, with older
all-time totals retained separately. A compact correction ledger keeps media
linkage for an archived postcard only while Bird Buddy still exposes every
linked media record; it can adjust the totals without retaining the postcard
ID. Confirmed history keeps the newest evidence per species and provenance.
The first sync can import only metadata Bird Buddy still exposes. `all-time`
combines exact history accumulated since the integration began with
conservative presence evidence from the current confirmed collection; it is
not a claim about Bird Buddy's total visit count. Repeated polls are idempotent,
and changed or removed classifications replace prior species while Bird Buddy
still exposes the underlying record.

History written before media linkage cannot prove which confirmed record
belongs to a cached postcard. The first successful sync after this upgrade
drops those unlinked, short-lived postcard rows instead of risking a stale
classification. Current confirmed metadata can still restore conservative
species presence.

## How eBird enrichment works

eBird returns recent public sightings and an eBird species code. The controller
searches iNaturalist for the exact active species-rank scientific name and uses
the resulting iNaturalist taxon ID as the canonical catalog identity. Ambiguous,
inactive, hybrid, subspecies, domestic, and unmatched records are deferred. A
seven-day negative cache prevents one mismatch from generating requests every
15 minutes.

iNaturalist remains the source for taxonomy and research-grade CC0/CC-BY
reference photographs. eBird and Macaulay Library media are not copied into the
generation pipeline.

## Multiple providers

Each provider receives the configured `species_limit`, so the merged result may
be larger before duplicate species are removed. If one provider fails, the
others still refresh the active catalog and the controller records which source
failed. A refresh fails only when every configured provider fails. In that
case, the prior active catalog stays in place.

The legacy singular values `source = "inaturalist"`, `"ebird"`, `"combined"`,
`"birdweather"`, and `"all"` remain accepted. Do not set both `source` and
`sources`. The meaning of legacy `all` is frozen to the three providers present
when it was introduced, so an application upgrade cannot silently contact a
future provider. New configurations should use the explicit array.

iNaturalist supplies observation counts, BirdWeather supplies station detection
counts, Bird Buddy supplies distinct postcard counts, and eBird's nearby
endpoint supplies presence rather than a comparable aggregate count. eBird-only
species receive weight one. These counts describe different collection methods
and should not be compared as equivalent evidence. `shuffle_bag` is the
recommended source-neutral rotation policy.

BirdWeather and Bird Buddy also supply each species' latest detection timestamp.
By default, a display node shows the newest detection once before returning to
its configured rotation. This is display priority, not a confidence claim or a
live event stream: the timestamp advances only when the controller refreshes a
provider. Configure `display_node.prioritize_latest_detection = false` to
disable it. The `discover` command includes `latest_detection_at` on species
entries when a configured provider supplies that timestamp and omits the field
otherwise.

## Limits and data use

The eBird nearby API supports at most 30 days and 50 km. BirdWeather returns at
most 100 species per station-species request. Bird Buddy can return no more
history than remains in its current feed on first setup, then uses private local
history. Use an explicit iNaturalist seed for longer pre-existing periods:

```bash
inky-bird-frame seed --config /path/to/config.toml \
  --source inaturalist --window last-year --species-limit 500
```

For an exact historical place-and-time window, provide both coordinates and
both inclusive ISO dates. This command-scoped location does not alter the
configured discovery location:

```bash
inky-bird-frame seed --config /path/to/config.toml \
  --source inaturalist --latitude 40.7128 --longitude -74.0060 \
  --radius-km 11 --start-date 2026-04-01 --end-date 2026-04-03 \
  --species-limit 500 --dry-run
```

Exact date ranges require iNaturalist because the other configured providers
cannot guarantee the same coordinate-radius historical query semantics. Run
without `--dry-run` only after reviewing the structured result. The seed stores
every distinct taxon as private collection membership and queues only missing,
non-terminal plates. It stores no raw observation records or command-scoped
coordinates. Approved seeded taxa become active immediately; unapproved taxa
become active after a plate passes review and approval.

Collection membership is independent from catalog synchronization. A plate
added to the public catalog later is not added to the private collection merely
because it was downloaded. Use `collection import-approved` for an explicit
point-in-time migration or `collection add TAXON_ID` for one taxon. Current
observations remain an independent source of active eligibility.

Repeat `--source` for a multi-provider seed, for example `--source inaturalist
--source ebird`. CLI overrides accept concrete providers rather than legacy
group aliases.

Review the official [eBird API documentation](https://documenter.getpostman.com/view/664302/S1ENwy59/)
and [eBird data-use guidance](https://support.ebird.org/en/support/solutions/articles/48001078113)
and the official [BirdWeather V1 API](https://app.birdweather.com/api/v1)
before commercial use. BirdWeather is a hosted dependency and its availability,
retention, accepted detection format, and API behavior remain outside this
project's control. Bird Buddy is likewise a hosted private dependency whose
schema, availability, and access policy remain outside this project's control.
The private discovery snapshot may contain provider diagnostics and source
names, but tokens, feeder/postcard identifiers, location details, and
observation details never enter reusable plates or the public catalog.
