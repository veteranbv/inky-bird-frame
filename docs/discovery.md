# Discovery sources

Inky Bird Frame can discover nearby birds through iNaturalist, eBird, or both.
Merlin Bird ID is Cornell's identification application; its nearby lists are
powered by eBird. This project integrates the documented eBird API, not Merlin.

## Choose a source

| Source | Credentials | Windows | Best use |
| --- | --- | --- | --- |
| `inaturalist` | None | All | Default setup, historical seeds, and licensed references |
| `ebird` | Personal eBird key | 1, 7, or 30 days | Bird-specific recent public sightings |
| `combined` | Personal eBird key | 1, 7, or 30 days | Broad recent discovery with provider fallback |

Request a personal key from [eBird](https://ebird.org/api/keygen). Store it in
the private configuration or inject it through an environment variable:

```toml
[discovery]
source = "combined"
zip_code = "12345"
radius_km = 8
species_limit = 50
window = "last-30-days"
ebird_api_key_env = "EBIRD_API_KEY"
```

Keep the configuration outside the checkout with mode `0600`. The application
never writes the key to state, logs, catalog files, or command output. Native
services do not necessarily inherit an interactive shell environment; a direct
value in the protected private TOML is the simplest option unless the service
manager explicitly injects `EBIRD_API_KEY`.

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

## Combined mode

Each provider receives the configured `species_limit`; the merged result can be
up to twice that size before duplicates are removed by iNaturalist taxon ID. If
one provider fails, the successful provider still refreshes the active catalog
and the controller records a provider-specific degradation. The refresh fails
only when every configured provider fails, leaving the prior active catalog in
place.

iNaturalist supplies observation-frequency counts. eBird's nearby endpoint
supplies recent presence rather than a comparable aggregate count, so eBird-only
species receive weight one. `shuffle_bag` is the recommended source-neutral
rotation policy.

## Limits and data use

The eBird nearby API supports at most 30 days and 50 km. Use an explicit
iNaturalist seed for longer periods:

```bash
inky-bird-frame seed --config /path/to/config.toml \
  --source inaturalist --window last-year --species-limit 500
```

Review the official [eBird API documentation](https://documenter.getpostman.com/view/664302/S1ENwy59/)
and [eBird data-use guidance](https://support.ebird.org/en/support/solutions/articles/48001078113)
before commercial use. The private discovery snapshot may contain provider
diagnostics, but location and observation details never enter reusable plates
or the public catalog.
