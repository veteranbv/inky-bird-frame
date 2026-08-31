# Discovery sources

Inky Bird Frame reads public observations, your private exported eBird history,
detections from your own acoustic station through BirdWeather or self-hosted
BirdNET-Go, private CSV history from BirdNET Analyzer, authorized postcard
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
| `ebird-archive` | Explicit personal-data import | All | Species from your complete personal eBird history |
| `birdweather` | BirdWeather station token | All | Species acoustically detected by one station |
| `birdnet-go` | Local BirdNET-Go base URL | All | Species acoustically detected by a self-hosted station |
| `birdnet-analyzer` | Explicit CSV import | All; dated rows for finite windows | Offline acoustic-analysis history |
| `birdbuddy` | Authorized Bird Buddy account | All after setup | Postcard species from one feeder, plus optional manual sightings |

Select any combination with a TOML array. Each configured provider runs
independently.

Use the same setup loop for every source:

1. Meet the provider's prerequisites and add its explicit name to
   `discovery.sources`.
2. Run `inky-bird-frame config validate --config /path/to/config.toml`.
3. Run `discover` to inspect provider results without changing the active
   catalog, then run `refresh` to save a successful snapshot.
4. Inspect the named entry in the `providers` array returned by `discover` and
   `refresh`. Use `status` separately for catalog and generation state.

To disable a provider, remove its name from `discovery.sources`, validate the
configuration, and refresh. Imported history and authentication state are
retained unless a provider section below documents an explicit removal step.

## Configure a discovery location

iNaturalist and eBird need a point and radius. Choose exactly one location
form; eBird Archive-, BirdWeather-, BirdNET-Go-, BirdNET Analyzer-, and Bird
Buddy-only setups need none.

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

Verify a Geoapify-backed location with `config validate` and `discover`. An
authorization error points to the API key. An exact-match error usually means
the country code or postal format is wrong; a multiple-match error is
deliberately not guessed. Use direct coordinates when a postal code cannot be
resolved uniquely. To stop using Geoapify, replace the postal-code fields with
direct coordinates, then remove the key.

## Nearby iNaturalist and eBird setup

iNaturalist needs no credentials. The nearby eBird provider requires a personal
API key and supports only the recent windows shown in the provider table.

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

Verify both sources with `discover`, then inspect the `inaturalist` and `ebird`
entries in `providers`. Location, network, or upstream API failures stay
provider-specific when another configured source succeeds. An eBird
authentication failure usually means the key is missing or invalid; an
`unresolved_count` means returned taxonomy could not be matched exactly to an
active iNaturalist bird species. Remove either provider from `sources` to
disable it; no provider account or remote data is changed.

## eBird personal archive

eBird Archive is optional and separate from the recent-nearby `ebird` API
provider. Sign in to eBird's official
[Download My Data](https://ebird.org/downloadMyData) page and request your
complete personal export. Cornell sends a ZIP containing `MyEBirdData.csv`;
Inky also accepts that CSV directly if you extract or rename it.

The CSV must contain the complete official header and may be at most 512 MiB
after decompression. This bound protects the controller from damaged or
hostile archives while accommodating very large personal histories.

Preview the complete export before replacing local history:

```bash
inky-bird-frame ebird archive import \
  --config /path/to/config.toml \
  --archive /path/to/ebird.zip \
  --dry-run
```

Then import it and opt in to the provider:

```bash
inky-bird-frame ebird archive import \
  --config /path/to/config.toml \
  --archive /path/to/ebird.zip
```

```toml
[discovery]
sources = ["ebird-archive"]
window = "all-time"
```

In a mixed-provider installation, keep the normal discovery window for live
sources and seed the complete personal history separately. Preview first, then
repeat without `--dry-run`:

```bash
inky-bird-frame seed --config /path/to/config.toml \
  --source ebird-archive --window all-time --species-limit 100 --dry-run
```

That adds the resolved archive species to the private collection and queues
only missing plates; it does not change the configured window for eBird,
BirdWeather, or other live providers. An explicit personal travel interval is
also supported:

```bash
inky-bird-frame seed --config /path/to/config.toml \
  --source ebird-archive --start-date 2025-04-29 --end-date 2025-05-02 \
  --species-limit 100 --dry-run
```

Choose a species limit appropriate to the size of the export. The command
reports the applied limit and discovered count so a truncated selection is
visible before it writes collection or queue state.

Each import is a complete snapshot. Reimporting the same export is idempotent;
a newer complete export atomically adds or updates checklists. Inky refuses an
export that omits a previously imported checklist because a filtered download
could otherwise erase history. After confirming that the file is complete and
the reduction reflects intentional eBird edits or deletions, rerun with
`--allow-history-reduction`.

Private state retains only a one-way checklist fingerprint, the checklist date,
and common and scientific species names. It does not retain eBird account data,
raw submission or location IDs, coordinates, place names, observer counts,
individual bird counts, comments, effort, or media references. One species on
one checklist counts as one personal observation regardless of the reported
number of individuals. `ebird archive status` reports only aggregate counts and
the imported date range.

Every scientific name must still resolve exactly to one active iNaturalist bird
species. Hybrids, `sp.` aggregates, domestic groups, and ambiguous or unmatched
labels remain private unresolved diagnostics and cannot trigger generation.
First-time taxonomy resolution is paced to iNaturalist's official guidance of
about one API request per second; large archives can therefore take a few
minutes to seed. Completed matches are cached atomically, so a transient error
or rate limit preserves progress for the next run.
Because the export supplies calendar dates rather than event instants, archive
observations do not claim latest-detection rotation priority. Re-export and
reimport periodically to include newly published eBird checklists; no eBird
password, browser cookie, or unsupported live API is stored.

Verify an import with:

```bash
inky-bird-frame ebird archive status --config /path/to/config.toml
inky-bird-frame discover --config /path/to/config.toml
```

Malformed headers, an oversized uncompressed CSV, or an incomplete replacement
fail before history is committed. Remove `ebird-archive` from `sources` to
disable discovery without deleting the private import.

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

Use `sources = ["inaturalist", "ebird", "birdweather"]` to combine this station
with nearby iNaturalist and eBird observations. For manually invoked commands,
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

Verify the provider with `discover` and inspect the `birdweather` result,
including `species_count` and `unresolved_count`. Authentication errors point to
the station token; an empty healthy result can mean that the configured time
window contains no accepted station species. Correct detection policy and
false positives at the detector. Remove `birdweather` from `sources` to disable
it; Inky does not change the station.

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
proxy. The endpoint must be reachable without HTTP authentication; keep it on a
trusted network or VPN. Do not embed credentials, query parameters, or
fragments. A BirdNET-Go-only configuration needs no discovery location.

### macOS Local Network access

On macOS 15 and later, Local Network privacy applies to LaunchAgents. A direct
BirdNET-Go address on the controller's attached subnet can therefore work from
Terminal but fail during the scheduled refresh with `[Errno 65] No route to
host`. Apple documents the distinction between command-line tools and
LaunchAgents in [TN3179: Understanding local network privacy][apple-tn3179].

If you already run a reverse proxy on a routed network path, prefer its HTTPS
base URL:

```toml
[discovery]
sources = ["birdnet-go"]
birdnet_go_url = "https://birdnet.example.net"
```

The proxy must expose the same unauthenticated BirdNET-Go API. After changing
the URL, test the installed service instead of relying on a manual refresh:

```bash
refresh_label="gui/$(id -u)/com.inky-bird-frame.refresh"
refresh_pid="$(launchctl kickstart -kp "$refresh_label")"
while kill -0 "$refresh_pid" 2>/dev/null; do sleep 1; done
tail -n 200 "$HOME/Library/Application Support/Inky Bird Frame/logs/refresh.log"
```

The newest result should show the `birdnet-go` provider with `status` set to
`ok`. If both the direct address and the proxy fail from LaunchAgent context,
follow Apple's current Local Network privacy guidance rather than adding a
broad network exception without reviewing its system-wide effect.

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
running. BirdNET-Go runs only when you include `birdnet-go` in the explicit
`sources` array.

Verify with `discover` and inspect the `birdnet-go` provider. Connection errors
usually mean the base URL is not reachable from the controller process or
container; response-contract errors can indicate an incompatible BirdNET-Go
version. Correct false positives in BirdNET-Go. Remove `birdnet-go` from
`sources` to disable it; Inky has no detector-side state to remove.

[apple-tn3179]: https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy

## BirdNET Analyzer CSV import

[BirdNET Analyzer](https://github.com/birdnet-team/BirdNET-Analyzer) is the
offline desktop and command-line analyzer from the BirdNET team. Its official
CSV output contains segment offsets, species names, confidence, and a
source-file field, but no absolute recording date. Inky never guesses one from
a filename or filesystem timestamp.

For a directory of recordings, ask Analyzer to create one importable table in
addition to its per-file results:

```bash
python3 -m birdnet_analyzer.analyze /path/to/recordings \
  --output /path/to/analyzer-output \
  --rtype csv \
  --combine_results \
  --min_conf 0.7
```

This example uses a minimum confidence of `0.70`. Choose a threshold that fits
your detector policy, then review `BirdNET_CombinedTable.csv` before import.
Inky accepts one CSV per import and does not apply a second confidence filter.
The required headers are exactly `Start (s)`, `End (s)`, `Scientific name`,
`Common name`, `Confidence`, and `File`.

Import an Analyzer CSV into controller-private history, then opt in to the
provider:

```bash
inky-bird-frame birdnet-analyzer import \
  --config /path/to/config.toml \
  --csv /path/to/analyzer-output/BirdNET_CombinedTable.csv
```

```toml
[discovery]
sources = ["birdnet-analyzer"]
window = "all-time"
```

An undated import is eligible only for `all-time`. When every row in a CSV came
from recordings made on one known calendar date, supply it explicitly:

```bash
inky-bird-frame birdnet-analyzer import \
  --config /path/to/config.toml \
  --csv /path/to/results.csv \
  --observed-on 2026-08-09
```

The date applies to every row. Split multi-day results into one CSV per date;
do not assign an approximate date merely to make detections enter a shorter
window. Analyzer provides no absolute time, so dated imports do not invent a
latest-detection timestamp or take latest-detection rotation priority.

Imports are atomic and idempotent. Inky identifies a recording segment from
the CSV's `File`, `Start (s)`, and `End (s)` values, allowing a later Analyzer
classification for that same segment to replace the earlier one. Keep the
Analyzer `File` values stable and unique across your corpus. Reusing one segment
identity with two different explicit dates fails closed rather than silently
mixing history. Use `--dry-run` to validate and preview counts without replacing
history.

The private mode-`0600` state retains only a one-way segment fingerprint,
species names, and an optional explicit date. Source paths, segment offsets,
confidence values, and audio are not retained. Every species still requires an
exact active iNaturalist bird match; unresolved or non-bird labels cannot enter
generation. `all-time` means all unique segments imported into this
installation, not the complete history of the recording device.

Inky validates that each CSV confidence is a finite value from zero through one
but does not impose a second, arbitrary threshold. Set the desired minimum
confidence when running BirdNET Analyzer and review its output before import;
Inky uses the rows in that export as the operator-approved detection set.

Use `--dry-run` first, then verify the committed import with `discover` and
inspect the `birdnet-analyzer` provider details. Missing or renamed headers fail
closed; Analyzer versions or export modes that produce another schema are not
silently inferred. Remove `birdnet-analyzer` from `sources` to disable it
without deleting imported history.

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

Bird Buddy rotates its refresh token once before each authenticated sync, so
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

Inspect the saved authentication state and selected feeder with `birdbuddy
status`, then run `discover` to verify live access and inspect the `birdbuddy`
provider. The status command is local-only; `discover` performs the authenticated
refresh and rotates the saved token. Multiple accessible feeders require a new
login with `--feeder-id`. An invalid or revoked session requires an explicit
login; schema or pagination errors may mean the private API changed.
Inky redacts credentials, tokens, email addresses, postcard identifiers, and
raw server payloads from errors. Feeder names and IDs are intentionally shown
when needed to choose or verify a feeder. Remove `birdbuddy` from `sources` to
disable synchronization while retaining auth and history, or run `birdbuddy
logout --yes` to remove local authentication while preserving history.

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
and `"birdweather"` remain accepted. Do not set both `source` and `sources`.
Version 0.5.0 removed `source = "all"` because its frozen three-provider meaning
became misleading as new providers were added. Replace it without changing
behavior:

```toml
[discovery]
sources = ["inaturalist", "ebird", "birdweather"]
```

Configuration loading now fails with this migration when `source = "all"` is
present. New configurations should always use the explicit array.

iNaturalist supplies observation counts. eBird Archive counts checklists that
contain each species. BirdWeather and BirdNET-Go supply station detection
counts, BirdNET Analyzer counts imported detection records, and Bird Buddy
supplies distinct postcard counts. eBird's nearby endpoint supplies presence
rather than a comparable aggregate count, so eBird-only species receive weight
one. When several providers resolve to the same species, Inky keeps the largest
provider count instead of adding unlike measurements together. These counts
describe different collection methods and should not be compared as equivalent
evidence. `shuffle_bag` is the recommended source-neutral rotation policy.

BirdWeather, BirdNET-Go, and Bird Buddy also supply each species' latest
detection timestamp. By default, a display node shows the newest detection once
before returning to its configured rotation. This is display priority, not a
confidence claim or a live event stream: the timestamp advances only when the
controller refreshes a provider. Configure
`display_node.prioritize_latest_detection = false` to disable it. The `discover`
command includes `latest_detection_at` on species entries when a configured
provider supplies that timestamp and omits the field otherwise.

## Limits and data use

The eBird nearby API supports at most 30 days and 50 km. An eBird personal
archive is bounded to 512 MiB of uncompressed CSV data per import. BirdWeather
returns at most 100 species per station-species request. Bird Buddy can return
no more history than remains in its current feed on first setup, then uses
private local history. Use an explicit iNaturalist seed for longer pre-existing
periods:

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

Coordinate-radius historical ranges require iNaturalist because the other
configured providers cannot guarantee the same place-and-time query semantics.
An imported eBird Archive supports personal-checklist date ranges without a
location filter. Run without `--dry-run` only after reviewing the structured
result. The seed stores every distinct taxon as private collection membership
and queues only missing, non-terminal plates. It stores no raw observation
records or command-scoped coordinates. Approved seeded taxa become active
immediately; unapproved taxa become active after a plate passes review and
approval.

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
