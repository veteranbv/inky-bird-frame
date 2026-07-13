# Discovery sources

Inky Bird Frame can discover birds through public observations or an optional
authenticated acoustic station. The controller consumes species metadata from
these services and sends every result through one catalog and review pipeline.
Merlin Bird ID is Cornell's identification application; its nearby lists are
powered by eBird. This project integrates the documented eBird API, not Merlin.

## Choose a source

| Source | Credentials | Windows | Best use |
| --- | --- | --- | --- |
| `inaturalist` | None | All | Default setup, historical seeds, and licensed references |
| `ebird` | Personal eBird key | 1, 7, or 30 days | Bird-specific recent public sightings |
| `combined` | Personal eBird key | 1, 7, or 30 days | Broad recent discovery with provider fallback |
| `birdweather` | BirdWeather station token | All | Species acoustically detected by one station |
| `all` | Both credentials | 1, 7, or 30 days | Public observations plus station detections |

Request a personal key from [eBird](https://ebird.org/api/keygen). Store it in
the private configuration:

```toml
[discovery]
source = "combined"
zip_code = "12345"
radius_km = 8
species_limit = 50
window = "last-30-days"
ebird_api_key = "your-personal-api-key"
```

For manually invoked commands, you may replace `ebird_api_key` with
`ebird_api_key_env = "EBIRD_API_KEY"`. Managed controller services do not inherit the shell
environment used during installation, so LaunchAgent and systemd installations require the direct
value in the private mode-`0600` configuration file.

Keep the configuration outside the checkout with mode `0600`. The application
never writes the key to state, logs, catalog files, or command output.

## BirdWeather station setup

BirdWeather is optional. Create a BirdWeather account and station, then connect
a compatible detector such as BirdNET-Pi by following the official
[BirdNET-Pi integration guide](https://www.birdweather.com/birdnetpi). Copy the
station authentication token into the private controller configuration:

```toml
[discovery]
source = "birdweather"
zip_code = "12345"
radius_km = 8
species_limit = 50
window = "last-30-days"
birdweather_token = "your-station-token"
```

Use `source = "all"` and configure both `ebird_api_key` and
`birdweather_token` to query every provider. For manually invoked commands,
`birdweather_token_env = "BIRDWEATHER_TOKEN"` is also supported. Managed
services require the direct token in the private mode-`0600` file because they
do not inherit the installation shell environment.

The token authenticates one station. The application uses BirdWeather's
documented station-species endpoint, requests only the `avian` classification,
and uses the selected time window. It does not query nearby BirdWeather
stations or infer a station from the configured ZIP code. The ZIP remains a
required controller setting for compatibility and for location-based providers,
but it does not select or filter BirdWeather station detections.

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

## Combined and all modes

Each provider receives the configured `species_limit`; the merged result can be
larger before duplicates are removed by iNaturalist taxon ID. `combined` queries
iNaturalist and eBird. `all` adds BirdWeather. If one provider fails, successful
providers still refresh the active catalog and the controller records a
provider-specific degradation. The refresh fails only when every configured
provider fails, leaving the prior active catalog in place.

iNaturalist supplies observation counts, BirdWeather supplies station detection
counts, and eBird's nearby endpoint supplies presence rather than a comparable
aggregate count. eBird-only species receive weight one. These counts describe
different collection methods and should not be compared as equivalent evidence.
`shuffle_bag` is the recommended source-neutral rotation policy.

## Limits and data use

The eBird nearby API supports at most 30 days and 50 km. BirdWeather returns at
most 100 species per station-species request. Use an explicit
iNaturalist seed for longer periods:

```bash
inky-bird-frame seed --config /path/to/config.toml \
  --source inaturalist --window last-year --species-limit 500
```

Review the official [eBird API documentation](https://documenter.getpostman.com/view/664302/S1ENwy59/)
and [eBird data-use guidance](https://support.ebird.org/en/support/solutions/articles/48001078113)
and the official [BirdWeather V1 API](https://app.birdweather.com/api/v1)
before commercial use. BirdWeather is a hosted dependency and its availability,
retention, accepted detection format, and API behavior remain outside this
project's control. The private discovery snapshot may contain provider
diagnostics and source names, but station tokens, location details, and
observation details never enter reusable plates or the public catalog.
