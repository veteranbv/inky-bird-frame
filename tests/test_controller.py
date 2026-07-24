from __future__ import annotations

import json
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from inky_bird_frame.birds import (
    BirdSpecies,
    BirdWeatherSpecies,
    DateRange,
    EbirdResolution,
    EbirdSpecies,
    ObservationWindow,
)
from inky_bird_frame.catalog import CatalogEntry, candidate_directory, write_candidate_manifest
from inky_bird_frame.codex_runner import CodexRunner
from inky_bird_frame.config import DiscoveryProvider, load_config
from inky_bird_frame.controller import (
    DiscoveryResult,
    DiscoverySnapshot,
    ProviderStatus,
    discover_species,
    enqueue_seed_species,
    exclusive_refresh_lock,
    generate_candidate,
    load_or_create_profile,
    load_or_fetch_references,
    run_controller_cycle,
    run_generation_cycle,
    run_refresh_cycle,
)
from inky_bird_frame.errors import (
    CatalogError,
    DataSourceError,
    GenerationError,
    InsufficientReferencesError,
    QualityReviewError,
    SpeciesStateError,
)
from inky_bird_frame.geo import DiscoveryLocation
from inky_bird_frame.models import QualityReview, SpeciesProfileData
from inky_bird_frame.prompts import PROMPT_VERSION
from inky_bird_frame.retry import RetryStore


def discovery_result(location: DiscoveryLocation, species: list[BirdSpecies]) -> DiscoveryResult:
    return DiscoveryResult(
        location=location,
        species=species,
        providers=[ProviderStatus("inaturalist", "ok", len(species))],
        unresolved=[],
    )


PROFILE = SpeciesProfileData(
    taxon_id=9083,
    common_name="Northern Cardinal",
    scientific_name="Cardinalis cardinalis",
    family="Cardinalidae",
    measurements={"length": "8.3 in", "wingspan": "10 in", "weight": "1.5 oz"},
    field_marks=["crest", "red plumage", "black mask", "orange bill"],
    habitat="Woodland edges",
    behavior="Forages near cover",
    palette=["red", "black", "orange"],
    sources=[
        {"title": "Source one", "url": "https://example.test/one"},
        {"title": "Source two", "url": "https://example.test/two"},
    ],
)

CONFIG = """
[discovery]
zip_code = "12345"
radius_km = 8
species_limit = 12
window = "last-week"

[controller]
workspace_dir = "."
catalog_dir = "catalog"
state_dir = "state"
codex_path = "/usr/bin/false"
bind_host = "127.0.0.1"
port = 8793
references_per_species = 4
generations_per_cycle = 1
max_generation_attempts = 3

[display_node]
controller_url = "http://controller.test:8793"
state_dir = "display"
"""


class ControllerTests(unittest.TestCase):
    def test_invalid_cached_profile_fails_as_catalog_state(self) -> None:
        species = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            cache_path = config.controller.state_dir / "profiles/9083/profile.json"
            cache_path.parent.mkdir(parents=True)
            cache_path.write_text("{}")

            with self.assertRaisesRegex(CatalogError, "Invalid cached species profile"):
                load_or_create_profile(
                    config,
                    species,
                    [],
                    [],
                    cast(CodexRunner, object()),
                    Path(temporary) / "profile.json",
                    Path(temporary) / "profile.log",
                )

    def test_validated_species_profile_is_reused_without_new_research(self) -> None:
        species = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        profile = cast(
            SpeciesProfileData,
            {
                **PROFILE,
                "sources": [
                    {"title": "Cornell", "url": "https://www.allaboutbirds.org/one"},
                    {"title": "Audubon", "url": "https://www.audubon.org/two"},
                ],
            },
        )

        class FakeRunner:
            calls = 0

            def create_profile(self, *_args: object, **_kwargs: object) -> SpeciesProfileData:
                self.calls += 1
                output_path = _args[-2]
                assert isinstance(output_path, Path)
                output_path.write_text(json.dumps(profile))
                return profile

        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            runner = FakeRunner()
            with patch("inky_bird_frame.controller.fetch_taxon_context"):
                first, _ = load_or_create_profile(
                    config,
                    species,
                    [],
                    [],
                    cast(CodexRunner, runner),
                    Path(temporary) / "first.json",
                    Path(temporary) / "first.log",
                )
            second, second_path = load_or_create_profile(
                config,
                species,
                [],
                [],
                cast(CodexRunner, runner),
                Path(temporary) / "second.json",
                Path(temporary) / "second.log",
            )

        self.assertEqual(runner.calls, 1)
        self.assertEqual(first, second)
        self.assertEqual(second_path.name, "second.json")

    def test_seed_queues_distinct_unapproved_species_without_changing_discovery(self) -> None:
        approved = BirdSpecies(1, "Approved Bird", "Avis approved", 4, "iNaturalist")
        queued = BirdSpecies(2, "Queued Bird", "Avis queued", 3, "iNaturalist")
        location = DiscoveryLocation("12345", "Exampleville", "XY", 1.0, 2.0)
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            with (
                patch(
                    "inky_bird_frame.controller.resolve_discovery_location", return_value=location
                ),
                patch(
                    "inky_bird_frame.controller.fetch_inaturalist_birds",
                    return_value=[approved, queued],
                ),
                patch("inky_bird_frame.controller.approved_taxon_ids", return_value={1}),
            ):
                result = enqueue_seed_species(
                    config,
                    window=ObservationWindow.LAST_YEAR,
                    species_limit=500,
                )

            queue = json.loads((config.controller.state_dir / "generation-queue.json").read_text())

        self.assertEqual(result["discovered_count"], 2)
        self.assertEqual(result["already_approved_count"], 1)
        self.assertEqual(result["added_count"], 1)
        added = cast(list[dict[str, object]], result["added"])
        self.assertEqual(added[0]["source"], "iNaturalist")
        self.assertEqual(queue["species"][0]["taxon_id"], 2)
        self.assertNotIn("zip_code", queue)

    def test_seed_dry_run_does_not_create_controller_state(self) -> None:
        species = BirdSpecies(2, "New Bird", "Avis nova", 1, "eBird")
        location = DiscoveryLocation("12345", "Exampleville", "XY", 1.0, 2.0)
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            with (
                patch(
                    "inky_bird_frame.controller.discover_species",
                    return_value=discovery_result(location, [species]),
                ) as discover,
                patch("inky_bird_frame.controller.approved_taxon_ids", return_value=set()),
            ):
                result = enqueue_seed_species(
                    config,
                    window=ObservationWindow.LAST_WEEK,
                    dry_run=True,
                )

            state_exists = config.controller.state_dir.exists()

        self.assertEqual(result["added_count"], 1)
        self.assertFalse(state_exists)
        self.assertFalse(discover.call_args.kwargs["persist_taxonomy_cache"])

    def test_historical_seed_uses_coordinate_override_without_mutating_config(self) -> None:
        species = BirdSpecies(2, "Trip Bird", "Avis itineris", 3, "iNaturalist")
        date_range = DateRange(date(2026, 7, 13), date(2026, 7, 16))
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            with (
                patch(
                    "inky_bird_frame.controller.fetch_inaturalist_birds",
                    return_value=[species],
                ) as fetch,
                patch("inky_bird_frame.controller.approved_taxon_ids", return_value=set()),
            ):
                result = enqueue_seed_species(
                    config,
                    date_range=date_range,
                    latitude=33.6407,
                    longitude=-84.4277,
                    sources=(DiscoveryProvider.INATURALIST,),
                    radius_km=11,
                    dry_run=True,
                )

        self.assertEqual(result["start_date"], "2026-07-13")
        self.assertEqual(result["end_date"], "2026-07-16")
        self.assertIsNone(result["window"])
        self.assertEqual(fetch.call_args.kwargs["latitude"], 33.6407)
        self.assertEqual(fetch.call_args.kwargs["longitude"], -84.4277)
        self.assertEqual(fetch.call_args.kwargs["date_range"], date_range)
        self.assertEqual(config.discovery.zip_code, "12345")

    def test_historical_seed_rejects_provider_without_exact_date_semantics(self) -> None:
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            with self.assertRaisesRegex(ValueError, "require --source inaturalist"):
                enqueue_seed_species(
                    config,
                    date_range=DateRange(date(2026, 7, 13), date(2026, 7, 16)),
                    sources=(DiscoveryProvider.EBIRD,),
                    dry_run=True,
                )

    def test_generation_prioritizes_current_species_before_seed_queue(self) -> None:
        current = BirdSpecies(1, "Current Bird", "Avis current", 4, "iNaturalist")
        queued = BirdSpecies(2, "Queued Bird", "Avis queued", 3, "iNaturalist")
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            config.controller.state_dir.mkdir(parents=True)
            (config.controller.state_dir / "discovery.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "refreshed_at": datetime.now(UTC).isoformat(),
                        "place_name": "Exampleville",
                        "state": "XY",
                        "species": [
                            {
                                "taxon_id": current.taxon_id,
                                "common_name": current.common_name,
                                "scientific_name": current.scientific_name,
                                "observation_count": current.observation_count,
                                "source": current.source,
                            }
                        ],
                    }
                )
            )
            (config.controller.state_dir / "generation-queue.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "updated_at": datetime.now(UTC).isoformat(),
                        "species": [
                            {
                                "taxon_id": queued.taxon_id,
                                "common_name": queued.common_name,
                                "scientific_name": queued.scientific_name,
                                "observation_count": queued.observation_count,
                                "source": queued.source,
                            }
                        ],
                    }
                )
            )
            attempted: list[int] = []

            def fail_generation(_config: object, species: BirdSpecies, _workspace: object) -> Path:
                attempted.append(species.taxon_id)
                raise DataSourceError("temporary")

            with (
                patch("inky_bird_frame.controller.approved_taxon_ids", return_value=set()),
                patch("inky_bird_frame.controller.generate_candidate", side_effect=fail_generation),
                patch("inky_bird_frame.controller.rebuild_catalog_index", return_value=[]),
            ):
                result = run_generation_cycle(config)

        self.assertEqual(attempted, [current.taxon_id, queued.taxon_id])
        self.assertEqual(result["eligible_count"], 2)
        self.assertEqual(result["attempted_count"], 2)
        self.assertEqual(result["queued_count"], 1)

    def test_generation_skips_deferred_species_and_attempts_later_work(self) -> None:
        deferred = BirdSpecies(1, "Deferred Bird", "Avis deferred", 4, "iNaturalist")
        later = BirdSpecies(2, "Later Bird", "Avis later", 3, "iNaturalist")
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            config.controller.state_dir.mkdir(parents=True)
            (config.controller.state_dir / "discovery.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "refreshed_at": datetime.now(UTC).isoformat(),
                        "place_name": "Exampleville",
                        "state": "XY",
                        "species": [
                            {
                                "taxon_id": item.taxon_id,
                                "common_name": item.common_name,
                                "scientific_name": item.scientific_name,
                                "observation_count": item.observation_count,
                                "source": item.source,
                            }
                            for item in (deferred, later)
                        ],
                    }
                )
            )
            RetryStore(config.controller.state_dir / "generation-retries.json").record_failure(
                deferred.taxon_id,
                DataSourceError("temporary"),
                now=datetime.now(UTC),
                initial_minutes=30,
                maximum_minutes=60,
            )
            attempted: list[int] = []

            def fail_generation(_config: object, species: BirdSpecies, _workspace: object) -> Path:
                attempted.append(species.taxon_id)
                raise DataSourceError("temporary")

            with (
                patch("inky_bird_frame.controller.approved_taxon_ids", return_value=set()),
                patch("inky_bird_frame.controller.generate_candidate", side_effect=fail_generation),
                patch("inky_bird_frame.controller.rebuild_catalog_index", return_value=[]),
            ):
                result = run_generation_cycle(config)

        self.assertEqual(attempted, [later.taxon_id])
        self.assertEqual(result["attempted_count"], 1)
        self.assertEqual(result["deferred_count"], 2)
        self.assertEqual(result["outstanding_retry_count"], 2)

    def test_due_unattempted_retry_remains_outstanding(self) -> None:
        first = BirdSpecies(1, "First Bird", "Avis first", 4, "iNaturalist")
        retrying = BirdSpecies(2, "Retrying Bird", "Avis retrying", 3, "iNaturalist")
        entry = CatalogEntry(
            taxon_id=first.taxon_id,
            common_name=first.common_name,
            scientific_name=first.scientific_name,
            slug="first-bird",
            portrait_path="species/1-first-bird/portrait.png",
            portrait_sha256="portrait",
            display_path="species/1-first-bird/display.png",
            display_sha256="display",
            approved_at=datetime.now(UTC).isoformat(),
        )
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            config.controller.state_dir.mkdir(parents=True)
            (config.controller.state_dir / "discovery.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "refreshed_at": datetime.now(UTC).isoformat(),
                        "place_name": "Exampleville",
                        "state": "XY",
                        "species": [
                            {
                                "taxon_id": species.taxon_id,
                                "common_name": species.common_name,
                                "scientific_name": species.scientific_name,
                                "observation_count": species.observation_count,
                                "source": species.source,
                            }
                            for species in (first, retrying)
                        ],
                    }
                )
            )
            retry_store = RetryStore(config.controller.state_dir / "generation-retries.json")
            retry_store.record_failure(
                retrying.taxon_id,
                DataSourceError("temporary"),
                now=datetime.now(UTC) - timedelta(minutes=31),
                initial_minutes=30,
                maximum_minutes=60,
            )
            retry_store.set_quality_guidance(first.taxon_id, ("Keep the wing angle accurate",))
            with (
                patch("inky_bird_frame.controller.approved_taxon_ids", return_value=set()),
                patch("inky_bird_frame.controller.generate_candidate"),
                patch("inky_bird_frame.controller.approve_candidate", return_value=entry),
                patch("inky_bird_frame.controller._write_active_catalog", return_value=0),
            ):
                result = run_generation_cycle(config)
            first_guidance = RetryStore(
                config.controller.state_dir / "generation-retries.json"
            ).quality_guidance(first.taxon_id)

        self.assertEqual(result["attempted_count"], 1)
        self.assertEqual(result["deferred_count"], 0)
        self.assertEqual(result["outstanding_retry_count"], 1)
        self.assertIsNone(first_guidance)

    def test_overlapping_refresh_is_rejected_before_discovery(self) -> None:
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            with (
                exclusive_refresh_lock(config.controller.state_dir),
                patch("inky_bird_frame.controller.discover_species") as discover,
                self.assertRaisesRegex(DataSourceError, "already running"),
            ):
                run_refresh_cycle(config)

        discover.assert_not_called()

    def test_generation_recovers_pending_before_requiring_discovery(self) -> None:
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            recovered: list[str] = []

            def approve(_config: object) -> list[dict[str, object]]:
                recovered.append("approved")
                return []

            with (
                patch(
                    "inky_bird_frame.controller.approve_passing_candidates",
                    side_effect=approve,
                ),
                patch(
                    "inky_bird_frame.controller._read_discovery_snapshot",
                    side_effect=DataSourceError("missing"),
                ),
                self.assertRaisesRegex(DataSourceError, "missing"),
            ):
                run_generation_cycle(config)

        self.assertEqual(recovered, ["approved"])

    def test_generation_rebuilds_active_catalog_from_latest_snapshot(self) -> None:
        initial = DiscoverySnapshot(
            datetime.now(UTC),
            "Exampleville",
            "XY",
            [BirdSpecies(1, "Alpha Bird", "Alpha avis", 1, "iNaturalist")],
        )
        latest = DiscoverySnapshot(
            datetime.now(UTC),
            "Exampleville",
            "XY",
            [BirdSpecies(2, "Beta Bird", "Beta avis", 4, "iNaturalist")],
        )
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            with (
                patch("inky_bird_frame.controller.approve_passing_candidates", return_value=[]),
                patch(
                    "inky_bird_frame.controller._read_discovery_snapshot",
                    side_effect=[initial, latest],
                ),
                patch("inky_bird_frame.controller._has_terminal_state", return_value=True),
                patch("inky_bird_frame.controller._write_active_catalog", return_value=0) as write,
            ):
                run_generation_cycle(config)

        write.assert_called_once_with(config, latest.species)

    def test_generation_rejects_a_stale_discovery_snapshot(self) -> None:
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            config.controller.state_dir.mkdir(parents=True)
            (config.controller.state_dir / "discovery.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "refreshed_at": "2000-01-01T00:00:00+00:00",
                        "place_name": "Exampleville",
                        "state": "XY",
                        "species": [],
                    }
                )
            )

            with self.assertRaisesRegex(DataSourceError, "stale"):
                run_generation_cycle(config)

    def test_refresh_writes_only_observed_approved_species_to_private_active_catalog(
        self,
    ) -> None:
        observed = BirdSpecies(
            12942,
            "Eastern Bluebird",
            "Sialia sialis",
            9,
            "BirdWeather",
            latest_detection_at="2026-07-13T08:10:00-04:00",
        )
        unapproved = BirdSpecies(
            7513, "Carolina Wren", "Thryothorus ludovicianus", 4, "iNaturalist"
        )
        location = DiscoveryLocation("12345", "Exampleville", "XY", 1.0, 2.0)
        approved = CatalogEntry(
            12942,
            "Eastern Bluebird",
            "Sialia sialis",
            "eastern-bluebird",
            "species/12942/portrait.png",
            "a" * 64,
            "species/12942/display.png",
            "b" * 64,
            "2026-07-09T00:00:00+00:00",
        )
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            with (
                patch(
                    "inky_bird_frame.controller.discover_species",
                    return_value=discovery_result(location, [observed, unapproved]),
                ),
                patch(
                    "inky_bird_frame.controller.rebuild_catalog_index",
                    return_value=[approved],
                ),
            ):
                result = run_refresh_cycle(config)
            active = json.loads((config.controller.state_dir / "active-catalog.json").read_text())
            snapshot = json.loads((config.controller.state_dir / "discovery.json").read_text())

        self.assertEqual(result["active_approved_count"], 1)
        self.assertEqual(active["species"][0]["taxon_id"], 12942)
        self.assertEqual(active["species"][0]["observation_count"], 9)
        self.assertEqual(active["species"][0]["latest_detection_at"], "2026-07-13T08:10:00-04:00")
        self.assertNotIn("zip_code", active)
        self.assertEqual(len(snapshot["species"]), 2)
        self.assertEqual(snapshot["species"][0]["source"], "BirdWeather")
        self.assertEqual(snapshot["species"][0]["latest_detection_at"], "2026-07-13T08:10:00-04:00")

    def test_refresh_preserves_approved_catalog_order(self) -> None:
        first = BirdSpecies(1, "Alpha Bird", "Alpha avis", 2, "iNaturalist")
        second = BirdSpecies(2, "Beta Bird", "Beta avis", 9, "iNaturalist")
        location = DiscoveryLocation("12345", "Exampleville", "XY", 1.0, 2.0)
        approved = [
            CatalogEntry(
                species.taxon_id,
                species.common_name,
                species.scientific_name,
                species.common_name.lower().replace(" ", "-"),
                f"species/{species.taxon_id}/portrait.png",
                "a" * 64,
                f"species/{species.taxon_id}/display.png",
                "b" * 64,
                "2026-07-09T00:00:00+00:00",
            )
            for species in (first, second)
        ]
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            with (
                patch(
                    "inky_bird_frame.controller.discover_species",
                    return_value=discovery_result(location, [second, first]),
                ),
                patch(
                    "inky_bird_frame.controller.rebuild_catalog_index",
                    return_value=approved,
                ),
            ):
                run_refresh_cycle(config)
            active = json.loads((config.controller.state_dir / "active-catalog.json").read_text())

        self.assertEqual([item["taxon_id"] for item in active["species"]], [1, 2])
        self.assertEqual([item["observation_count"] for item in active["species"]], [2, 9])

    def test_transient_source_failure_remains_eligible(self) -> None:
        species = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        location = DiscoveryLocation("12345", "Exampleville", "XY", 1.0, 2.0)
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            with (
                patch(
                    "inky_bird_frame.controller.discover_species",
                    return_value=discovery_result(location, [species]),
                ),
                patch("inky_bird_frame.controller.generate_candidate") as generate,
            ):
                generate.side_effect = DataSourceError("iNaturalist timed out")
                result = run_controller_cycle(config)
            terminal_failures = list((config.controller.state_dir / "failed").glob("9083-*"))

        failures = result["failures"]
        self.assertEqual(terminal_failures, [])
        self.assertIsInstance(failures, list)
        if isinstance(failures, list):
            self.assertFalse(failures[0]["terminal"])

    def test_cycle_publishes_a_previously_reviewed_pending_candidate(self) -> None:
        species = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        location = DiscoveryLocation("12345", "Exampleville", "XY", 1.0, 2.0)
        review = QualityReview(
            True,
            5,
            4,
            5,
            5,
            True,
            (),
            (
                {"title": "Cornell", "url": "https://example.test/cornell"},
                {"title": "ADW", "url": "https://example.test/adw"},
            ),
        )
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            candidate = candidate_directory(config.controller.state_dir, species)
            candidate.mkdir(parents=True)
            (candidate / "portrait.png").write_bytes(b"portrait")
            (candidate / "display.png").write_bytes(b"display")
            write_candidate_manifest(
                candidate,
                species,
                PROFILE,
                [],
                review,
                generator="test",
                prompt_version=PROMPT_VERSION,
                attempt=2,
                max_attempts=3,
            )
            with patch(
                "inky_bird_frame.controller.discover_species",
                return_value=discovery_result(location, [species]),
            ):
                result = run_controller_cycle(config)

            published = (config.controller.catalog_dir / "species/9083-northern-cardinal").is_dir()
            active = json.loads((config.controller.state_dir / "active-catalog.json").read_text())

        self.assertTrue(published)
        self.assertEqual(result["approved_count"], 1)
        self.assertEqual(result["active_approved_count"], 1)
        self.assertEqual([item["taxon_id"] for item in active["species"]], [9083])
        published_pending = result["published_pending"]
        self.assertIsInstance(published_pending, list)
        if isinstance(published_pending, list):
            self.assertEqual(len(published_pending), 1)

    def test_cycle_does_not_auto_publish_a_legacy_pending_candidate(self) -> None:
        species = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        location = DiscoveryLocation("12345", "Exampleville", "XY", 1.0, 2.0)
        review = QualityReview(True, 5, 4, 5, 5, True, ())
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            candidate = candidate_directory(config.controller.state_dir, species)
            candidate.mkdir(parents=True)
            (candidate / "portrait.png").write_bytes(b"portrait")
            (candidate / "display.png").write_bytes(b"display")
            write_candidate_manifest(
                candidate,
                species,
                PROFILE,
                [],
                review,
                generator="legacy",
                prompt_version="field-journal-v1",
            )
            with patch(
                "inky_bird_frame.controller.discover_species",
                return_value=discovery_result(location, []),
            ):
                result = run_controller_cycle(config)

            published = (config.controller.catalog_dir / "species/9083-northern-cardinal").is_dir()

        self.assertFalse(published)
        self.assertEqual(result["approved_count"], 0)
        self.assertEqual(result["published_pending"], [])

    def test_insufficient_references_are_deferred_without_blocking_queue(self) -> None:
        species = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        location = DiscoveryLocation("12345", "Exampleville", "XY", 1.0, 2.0)
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            config.controller.state_dir.mkdir(parents=True)
            RetryStore(
                config.controller.state_dir / "generation-retries.json"
            ).set_quality_guidance(species.taxon_id, ("Keep the bill proportion accurate",))
            with (
                patch(
                    "inky_bird_frame.controller.discover_species",
                    return_value=discovery_result(location, [species]),
                ),
                patch("inky_bird_frame.controller.generate_candidate") as generate,
            ):
                generate.side_effect = InsufficientReferencesError("only 1 of 4 references")
                result = run_controller_cycle(config)
            terminal_failures = list((config.controller.state_dir / "failed").glob("9083-*"))
            guidance = RetryStore(
                config.controller.state_dir / "generation-retries.json"
            ).quality_guidance(species.taxon_id)

        self.assertEqual(terminal_failures, [])
        failures = result["failures"]
        self.assertIsInstance(failures, list)
        if isinstance(failures, list):
            self.assertFalse(failures[0]["terminal"])
            self.assertIn("retry_at", failures[0])
        self.assertEqual(result["deferred_count"], 1)
        self.assertIsNotNone(guidance)
        self.assertEqual(
            generate.call_args.kwargs["initial_correction_findings"],
            ("Keep the bill proportion accurate",),
        )

    def test_failed_review_is_corrected_and_passing_attempt_is_staged(self) -> None:
        species = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        failed_review = QualityReview(False, 3, 4, 5, 5, True, ("Crest is too short",))
        passed_review = QualityReview(
            True,
            5,
            4,
            5,
            5,
            True,
            (),
            (
                {"title": "Cornell", "url": "https://www.allaboutbirds.org/example"},
                {"title": "Audubon", "url": "https://www.audubon.org/example"},
            ),
        )

        class FakeRunner:
            corrections: list[tuple[str, ...]] = []
            generated_paths: list[Path] = []
            review_paths: list[Path] = []
            reviews = iter((failed_review, passed_review))

            def __init__(self, _executable: Path, workspace: Path) -> None:
                self.workspace = workspace.resolve()

            def create_profile(self, *_args: object, **_kwargs: object) -> SpeciesProfileData:
                output_path = _args[-2]
                assert isinstance(output_path, Path)
                assert not output_path.resolve().is_relative_to(self.workspace)
                output_path.write_text(json.dumps(PROFILE))
                return PROFILE

            def generate_plate(self, *_args: object) -> Path:
                output_path = _args[-3]
                correction = _args[-1]
                assert isinstance(output_path, Path)
                assert isinstance(correction, tuple)
                self.generated_paths.append(output_path)
                assert output_path.resolve().is_relative_to(self.workspace)
                self.corrections.append(correction)
                output_path.write_bytes(b"generated")
                return output_path

            def review_plate(self, *_args: object, **_kwargs: object) -> QualityReview:
                portrait_path = _args[3]
                assert isinstance(portrait_path, Path)
                self.review_paths.append(portrait_path)
                assert not portrait_path.resolve().is_relative_to(self.workspace)
                return next(self.reviews)

        def prepare(_source: Path, portrait: Path, display: Path) -> None:
            portrait.write_bytes(b"portrait")
            display.write_bytes(b"display")

        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(
                CONFIG.replace('workspace_dir = "."', 'workspace_dir = "workspace"')
            )
            config = load_config(config_path)
            config.controller.workspace_dir.mkdir()
            with (
                patch("inky_bird_frame.controller.load_or_fetch_references", return_value=[]),
                patch("inky_bird_frame.controller.fetch_taxon_context"),
                patch("inky_bird_frame.controller.CodexRunner", FakeRunner),
                patch("inky_bird_frame.controller.prepare_generated_plate", side_effect=prepare),
            ):
                candidate = generate_candidate(
                    config,
                    species,
                    config.controller.workspace_dir,
                    initial_correction_findings=("Preserve the previous scale correction",),
                )
            manifest = json.loads((candidate / "manifest.json").read_text())
            private_histories = list(
                (config.controller.state_dir / "runs").glob("*/attempt-history.json")
            )

        self.assertEqual(
            FakeRunner.corrections,
            [("Preserve the previous scale correction",), ("Crest is too short",)],
        )
        self.assertEqual(len(FakeRunner.generated_paths), 2)
        self.assertEqual(len(FakeRunner.review_paths), 2)
        self.assertEqual(manifest["generation"]["attempt"], 2)
        self.assertEqual(manifest["status"], "pending")
        self.assertFalse((candidate / "attempt-history.json").exists())
        self.assertEqual(len(private_histories), 1)

    def test_runtime_generation_failure_remains_eligible(self) -> None:
        species = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        location = DiscoveryLocation("12345", "Exampleville", "XY", 1.0, 2.0)
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            config.controller.state_dir.mkdir(parents=True)
            RetryStore(
                config.controller.state_dir / "generation-retries.json"
            ).set_quality_guidance(species.taxon_id, ("Correct the ruler scale",))
            with (
                patch(
                    "inky_bird_frame.controller.discover_species",
                    return_value=discovery_result(location, [species]),
                ),
                patch("inky_bird_frame.controller.generate_candidate") as generate,
            ):
                generate.side_effect = GenerationError("profile failed")
                result = run_controller_cycle(config)

            failures = list((config.controller.state_dir / "failed").glob("9083-*"))
            guidance = RetryStore(
                config.controller.state_dir / "generation-retries.json"
            ).quality_guidance(species.taxon_id)

        self.assertEqual(failures, [])
        self.assertEqual(result["eligible_count"], 1)
        failure_results = result["failures"]
        self.assertIsInstance(failure_results, list)
        first_failure = failure_results[0] if isinstance(failure_results, list) else None
        self.assertIsInstance(first_failure, dict)
        if isinstance(first_failure, dict):
            self.assertEqual(first_failure["error"], "profile failed")
            self.assertFalse(first_failure["terminal"])
        self.assertIsNotNone(guidance)
        self.assertEqual(
            generate.call_args.kwargs["initial_correction_findings"],
            ("Correct the ruler scale",),
        )

    def test_catalog_wide_error_still_aborts_the_cycle(self) -> None:
        species = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        location = DiscoveryLocation("12345", "Exampleville", "XY", 1.0, 2.0)
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            with (
                patch(
                    "inky_bird_frame.controller.discover_species",
                    return_value=discovery_result(location, [species]),
                ),
                patch(
                    "inky_bird_frame.controller.generate_candidate",
                    side_effect=CatalogError("Asset checksum mismatch: catalog corrupt"),
                ),
                self.assertRaisesRegex(CatalogError, "catalog corrupt"),
            ):
                run_controller_cycle(config)

            self.assertEqual(list((config.controller.state_dir / "failed").glob("*")), [])

    def test_catalog_failure_is_terminal_for_species_without_aborting_cycle(self) -> None:
        species = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        location = DiscoveryLocation("12345", "Exampleville", "XY", 1.0, 2.0)
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            with (
                patch(
                    "inky_bird_frame.controller.discover_species",
                    return_value=discovery_result(location, [species]),
                ),
                patch("inky_bird_frame.controller.generate_candidate") as generate,
            ):
                generate.side_effect = SpeciesStateError("cached references are invalid")
                result = run_controller_cycle(config)

            failures = list((config.controller.state_dir / "failed").glob("9083-*"))

        self.assertEqual(len(failures), 1)
        failure_results = result["failures"]
        self.assertIsInstance(failure_results, list)
        if isinstance(failure_results, list):
            self.assertEqual(failure_results[0]["error"], "cached references are invalid")
            self.assertTrue(failure_results[0]["terminal"])

    def test_malformed_reference_entry_raises_species_state_error(self) -> None:
        species = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            manifest = config.controller.state_dir / "references" / "9083" / "references.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({"references": ["not-an-object"]}))

            with self.assertRaisesRegex(SpeciesStateError, "Invalid reference manifest"):
                load_or_fetch_references(config, species)

    def test_non_utf8_reference_manifest_raises_species_state_error(self) -> None:
        species = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            manifest = config.controller.state_dir / "references" / "9083" / "references.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_bytes(b'{"references": [\xff\xfe')

            with self.assertRaisesRegex(SpeciesStateError, "Invalid reference manifest"):
                load_or_fetch_references(config, species)

    def test_corrupt_profile_cache_quarantines_species_and_cycle_continues(self) -> None:
        self._assert_corrupt_profile_cache_is_quarantined(b"{}")

    def test_non_utf8_profile_cache_quarantines_species_and_cycle_continues(self) -> None:
        self._assert_corrupt_profile_cache_is_quarantined(b'{"taxon_id": 9083, "\xff\xfe')

    def _assert_corrupt_profile_cache_is_quarantined(self, corrupt_payload: bytes) -> None:
        corrupt = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        healthy = BirdSpecies(7513, "Carolina Wren", "Thryothorus ludovicianus", 3, "test")
        location = DiscoveryLocation("12345", "Exampleville", "XY", 1.0, 2.0)
        review = QualityReview(
            True,
            5,
            4,
            5,
            5,
            True,
            (),
            (
                {"title": "Cornell", "url": "https://www.allaboutbirds.org/example"},
                {"title": "Audubon", "url": "https://www.audubon.org/example"},
            ),
        )
        wren_profile = {
            "taxon_id": 7513,
            "common_name": "Carolina Wren",
            "scientific_name": "Thryothorus ludovicianus",
            "family": "Troglodytidae",
            "measurements": {"length": "5.5 in", "wingspan": "11 in", "weight": "0.7 oz"},
            "field_marks": ["white eyebrow", "rufous back", "barred wings", "upright tail"],
            "habitat": "Brushy woodland",
            "behavior": "Forages low in cover",
            "palette": ["rufous", "cream", "umber"],
            "sources": [
                {"title": "Cornell", "url": "https://www.allaboutbirds.org/one"},
                {"title": "Audubon", "url": "https://www.audubon.org/two"},
            ],
        }

        class FakeRunner:
            def __init__(self, _executable: Path, _workspace: Path) -> None:
                pass

            def generate_plate(self, *_args: object) -> Path:
                output_path = _args[-3]
                assert isinstance(output_path, Path)
                output_path.write_bytes(b"generated")
                return output_path

            def review_plate(self, *_args: object, **_kwargs: object) -> QualityReview:
                return review

        def prepare(_source: Path, portrait: Path, display: Path) -> None:
            portrait.write_bytes(b"portrait")
            display.write_bytes(b"display")

        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            profiles = config.controller.state_dir / "profiles"
            (profiles / "9083").mkdir(parents=True)
            (profiles / "9083" / "profile.json").write_bytes(corrupt_payload)
            (profiles / "7513").mkdir(parents=True)
            (profiles / "7513" / "profile.json").write_text(json.dumps(wren_profile))
            with (
                patch(
                    "inky_bird_frame.controller.discover_species",
                    return_value=discovery_result(location, [corrupt, healthy]),
                ),
                patch("inky_bird_frame.controller.load_or_fetch_references", return_value=[]),
                patch("inky_bird_frame.controller.CodexRunner", FakeRunner),
                patch("inky_bird_frame.controller.prepare_generated_plate", side_effect=prepare),
            ):
                result = run_controller_cycle(config)

            terminal_failures = list((config.controller.state_dir / "failed").glob("9083-*"))
            published = (config.controller.catalog_dir / "species/7513-carolina-wren").is_dir()

        self.assertEqual(len(terminal_failures), 1)
        self.assertTrue(published)
        failure_results = result["failures"]
        self.assertIsInstance(failure_results, list)
        if isinstance(failure_results, list):
            self.assertEqual(failure_results[0]["taxon_id"], 9083)
            self.assertTrue(failure_results[0]["terminal"])
        generated = cast(list[dict[str, object]], result["generated"])
        self.assertEqual([item["taxon_id"] for item in generated], [7513])

    def test_cycle_recovers_legacy_orphan_approved_pending_candidate(self) -> None:
        species = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        location = DiscoveryLocation("12345", "Exampleville", "XY", 1.0, 2.0)
        review = QualityReview(
            True,
            5,
            4,
            5,
            5,
            True,
            (),
            (
                {"title": "Cornell", "url": "https://example.test/cornell"},
                {"title": "ADW", "url": "https://example.test/adw"},
            ),
        )
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            candidate = candidate_directory(config.controller.state_dir, species)
            candidate.mkdir(parents=True)
            (candidate / "portrait.png").write_bytes(b"portrait")
            (candidate / "display.png").write_bytes(b"display")
            manifest_path = write_candidate_manifest(
                candidate,
                species,
                PROFILE,
                [],
                review,
                generator="test",
                prompt_version=PROMPT_VERSION,
                attempt=2,
                max_attempts=3,
            )
            manifest = json.loads(manifest_path.read_text())
            manifest["status"] = "approved"
            manifest["approved_at"] = "2026-07-12T08:00:00+00:00"
            manifest_path.write_text(json.dumps(manifest))
            with patch(
                "inky_bird_frame.controller.discover_species",
                return_value=discovery_result(location, [species]),
            ):
                result = run_controller_cycle(config)

            published = (config.controller.catalog_dir / "species/9083-northern-cardinal").is_dir()
            orphan_remains = candidate.exists()

        self.assertTrue(published)
        self.assertFalse(orphan_remains)
        published_pending = result["published_pending"]
        self.assertIsInstance(published_pending, list)
        if isinstance(published_pending, list):
            self.assertEqual(len(published_pending), 1)

    def test_exhausted_quality_review_becomes_terminal(self) -> None:
        species = BirdSpecies(9083, "Northern Cardinal", "Cardinalis cardinalis", 2, "test")
        location = DiscoveryLocation("12345", "Exampleville", "XY", 1.0, 2.0)
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(CONFIG)
            config = load_config(config_path)
            config.controller.state_dir.mkdir(parents=True)
            RetryStore(
                config.controller.state_dir / "generation-retries.json"
            ).set_quality_guidance(species.taxon_id, ("Correct the ruler scale",))
            with (
                patch(
                    "inky_bird_frame.controller.discover_species",
                    return_value=discovery_result(location, [species]),
                ),
                patch("inky_bird_frame.controller.generate_candidate") as generate,
            ):
                generate.side_effect = QualityReviewError("review attempts exhausted")
                result = run_controller_cycle(config)

            failures = list((config.controller.state_dir / "failed").glob("9083-*"))
            guidance = RetryStore(
                config.controller.state_dir / "generation-retries.json"
            ).quality_guidance(species.taxon_id)

        self.assertEqual(len(failures), 1)
        failure_results = result["failures"]
        self.assertIsInstance(failure_results, list)
        if isinstance(failure_results, list):
            self.assertTrue(failure_results[0]["terminal"])
        self.assertIsNone(guidance)
        self.assertEqual(
            generate.call_args.kwargs["initial_correction_findings"],
            ("Correct the ruler scale",),
        )


class DiscoveryProviderTests(unittest.TestCase):
    def test_birdweather_refresh_clears_previous_location_metadata(self) -> None:
        species = BirdSpecies(12942, "Eastern Bluebird", "Sialia sialis", 7, "BirdWeather")
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(
                CONFIG.replace(
                    "[discovery]\n",
                    '[discovery]\nsource = "birdweather"\nbirdweather_token = "station-secret"\n',
                )
            )
            config = load_config(config_path)
            config.controller.state_dir.mkdir(parents=True)
            (config.controller.state_dir / "discovery.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "refreshed_at": "2026-07-12T08:00:00+00:00",
                        "place_name": "Old Location",
                        "state": "XY",
                        "providers": [],
                        "species": [],
                    }
                )
            )
            discovery = DiscoveryResult(
                location=None,
                species=[species],
                providers=[ProviderStatus("birdweather", "ok", 1)],
                unresolved=[],
            )
            with (
                patch("inky_bird_frame.controller.discover_species", return_value=discovery),
                patch("inky_bird_frame.controller._write_active_catalog", return_value=0),
            ):
                result = run_refresh_cycle(config)

            snapshot = json.loads((config.controller.state_dir / "discovery.json").read_text())

        self.assertEqual(result["place_name"], "")
        self.assertEqual(result["state"], "")
        self.assertEqual(snapshot["place_name"], "")
        self.assertEqual(snapshot["state"], "")

    def test_all_mode_station_fallback_clears_previous_location_metadata(self) -> None:
        species = BirdSpecies(12942, "Eastern Bluebird", "Sialia sialis", 7, "BirdWeather")
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(
                CONFIG.replace(
                    "[discovery]\n",
                    '[discovery]\nsource = "all"\n'
                    'ebird_api_key = "ebird-secret"\n'
                    'birdweather_token = "station-secret"\n',
                )
            )
            config = load_config(config_path)
            config.controller.state_dir.mkdir(parents=True)
            (config.controller.state_dir / "discovery.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "refreshed_at": "2026-07-12T08:00:00+00:00",
                        "place_name": "Old Location",
                        "state": "XY",
                        "providers": [],
                        "species": [],
                    }
                )
            )
            discovery = DiscoveryResult(
                location=None,
                species=[species],
                providers=[
                    ProviderStatus("inaturalist", "error", 0),
                    ProviderStatus("ebird", "error", 0),
                    ProviderStatus("birdweather", "ok", 1),
                ],
                unresolved=[],
            )
            with (
                patch("inky_bird_frame.controller.discover_species", return_value=discovery),
                patch("inky_bird_frame.controller._write_active_catalog", return_value=0),
            ):
                result = run_refresh_cycle(config)

            snapshot = json.loads((config.controller.state_dir / "discovery.json").read_text())

        self.assertEqual(result["place_name"], "")
        self.assertEqual(result["state"], "")
        self.assertEqual(snapshot["place_name"], "")
        self.assertEqual(snapshot["state"], "")

    def test_birdweather_source_uses_station_detections(self) -> None:
        detection = BirdWeatherSpecies(
            42,
            "Eastern Bluebird",
            "Sialia sialis",
            7,
            "2026-07-12T08:15:00-04:00",
        )
        resolved = BirdSpecies(12942, "Eastern Bluebird", "Sialia sialis", 7, "BirdWeather")
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(
                CONFIG.replace(
                    "[discovery]\n",
                    '[discovery]\nsource = "birdweather"\nbirdweather_token = "station-secret"\n',
                )
            )
            config = load_config(config_path)
            with (
                patch("inky_bird_frame.controller.resolve_discovery_location") as lookup,
                patch(
                    "inky_bird_frame.controller.fetch_birdweather_species",
                    return_value=[detection],
                ) as fetch,
                patch(
                    "inky_bird_frame.controller.resolve_birdweather_species",
                    return_value=([resolved], []),
                ),
            ):
                result = discover_species(config)

        self.assertEqual(result.species, [resolved])
        self.assertIsNone(result.location)
        self.assertEqual(result.providers, [ProviderStatus("birdweather", "ok", 1)])
        self.assertEqual(fetch.call_args.kwargs["token"], "station-secret")
        lookup.assert_not_called()

    def test_all_mode_uses_birdweather_when_zip_lookup_fails(self) -> None:
        detection = BirdWeatherSpecies(
            42,
            "Eastern Bluebird",
            "Sialia sialis",
            7,
            "2026-07-12T08:15:00-04:00",
        )
        resolved = BirdSpecies(12942, "Eastern Bluebird", "Sialia sialis", 7, "BirdWeather")
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(
                CONFIG.replace(
                    "[discovery]\n",
                    '[discovery]\nsource = "all"\n'
                    'ebird_api_key = "ebird-secret"\n'
                    'birdweather_token = "station-secret"\n',
                )
            )
            config = load_config(config_path)
            with (
                patch(
                    "inky_bird_frame.controller.resolve_discovery_location",
                    side_effect=DataSourceError("ZIP service unavailable"),
                ),
                patch("inky_bird_frame.controller.fetch_inaturalist_birds") as inaturalist,
                patch("inky_bird_frame.controller.fetch_ebird_observations") as ebird,
                patch(
                    "inky_bird_frame.controller.fetch_birdweather_species",
                    return_value=[detection],
                ),
                patch(
                    "inky_bird_frame.controller.resolve_birdweather_species",
                    return_value=([resolved], []),
                ),
            ):
                result = discover_species(config)

        self.assertEqual(result.species, [resolved])
        self.assertIsNone(result.location)
        self.assertEqual(
            [(provider.name, provider.status) for provider in result.providers],
            [("inaturalist", "error"), ("ebird", "error"), ("birdweather", "ok")],
        )
        inaturalist.assert_not_called()
        ebird.assert_not_called()

    def test_all_mode_continues_when_birdweather_fails(self) -> None:
        inaturalist = BirdSpecies(12942, "Eastern Bluebird", "Sialia sialis", 9, "iNaturalist")
        ebird = BirdSpecies(12942, "Eastern Bluebird", "Sialia sialis", 1, "eBird")
        observation = EbirdSpecies("easblu", "Eastern Bluebird", "Sialia sialis", "2026-07-12")
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(
                CONFIG.replace(
                    "[discovery]\n",
                    '[discovery]\nsource = "all"\n'
                    'ebird_api_key = "ebird-secret"\n'
                    'birdweather_token = "station-secret"\n',
                )
            )
            config = load_config(config_path)
            with (
                patch(
                    "inky_bird_frame.controller.resolve_discovery_location",
                    return_value=DiscoveryLocation("12345", "Exampleville", "XY", 1.0, 2.0),
                ),
                patch(
                    "inky_bird_frame.controller.fetch_inaturalist_birds",
                    return_value=[inaturalist],
                ),
                patch(
                    "inky_bird_frame.controller.fetch_ebird_observations",
                    return_value=[observation],
                ),
                patch(
                    "inky_bird_frame.controller.resolve_ebird_species",
                    return_value=EbirdResolution([ebird], []),
                ),
                patch(
                    "inky_bird_frame.controller.fetch_birdweather_species",
                    side_effect=DataSourceError("BirdWeather unavailable"),
                ),
            ):
                result = discover_species(config)

        self.assertEqual(len(result.species), 1)
        self.assertEqual(result.species[0].sources, ("iNaturalist", "eBird"))
        self.assertEqual(result.providers[2].status, "error")

    def test_ebird_override_resolves_environment_key_at_execution_time(self) -> None:
        observation = EbirdSpecies("easblu", "Eastern Bluebird", "Sialia sialis", "2026-07-12")
        resolved = BirdSpecies(12942, "Eastern Bluebird", "Sialia sialis", 1, "eBird")
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(
                CONFIG.replace(
                    "[discovery]\n", '[discovery]\nebird_api_key_env = "TEST_EBIRD_KEY"\n'
                )
            )
            with patch.dict("os.environ", {}, clear=True):
                config = load_config(config_path)
            with (
                patch.dict("os.environ", {"TEST_EBIRD_KEY": "secret"}),
                patch(
                    "inky_bird_frame.controller.resolve_discovery_location",
                    return_value=DiscoveryLocation("12345", "Exampleville", "XY", 1.0, 2.0),
                ),
                patch(
                    "inky_bird_frame.controller.fetch_ebird_observations",
                    return_value=[observation],
                ) as fetch,
                patch(
                    "inky_bird_frame.controller.resolve_ebird_species",
                    return_value=EbirdResolution([resolved], []),
                ),
            ):
                result = discover_species(config, sources=(DiscoveryProvider.EBIRD,))

        self.assertEqual(result.species, [resolved])
        self.assertEqual(fetch.call_args.kwargs["api_key"], "secret")

    def test_combined_override_rejects_long_window_before_querying(self) -> None:
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(
                CONFIG.replace(
                    "[discovery]\n",
                    '[discovery]\nsource = "combined"\nebird_api_key = "secret"\n',
                )
            )
            config = load_config(config_path)
            with (
                patch("inky_bird_frame.controller.resolve_discovery_location") as lookup,
                self.assertRaisesRegex(ValueError, "up to 30 days"),
            ):
                discover_species(config, window=ObservationWindow.LAST_YEAR)

        lookup.assert_not_called()

    def test_combined_mode_deduplicates_by_inaturalist_taxon(self) -> None:
        inaturalist = BirdSpecies(12942, "Eastern Bluebird", "Sialia sialis", 9, "iNaturalist")
        ebird = BirdSpecies(12942, "Eastern Bluebird", "Sialia sialis", 1, "eBird")
        observation = EbirdSpecies("easblu", "Eastern Bluebird", "Sialia sialis", "2026-07-12")
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(
                CONFIG.replace(
                    "[discovery]\n",
                    '[discovery]\nsource = "combined"\nebird_api_key = "secret"\n',
                )
            )
            config = load_config(config_path)
            with (
                patch(
                    "inky_bird_frame.controller.resolve_discovery_location",
                    return_value=DiscoveryLocation("12345", "Exampleville", "XY", 1.0, 2.0),
                ),
                patch(
                    "inky_bird_frame.controller.fetch_inaturalist_birds",
                    return_value=[inaturalist],
                ),
                patch(
                    "inky_bird_frame.controller.fetch_ebird_observations",
                    return_value=[observation],
                ),
                patch(
                    "inky_bird_frame.controller.resolve_ebird_species",
                    return_value=EbirdResolution([ebird], []),
                ),
            ):
                result = discover_species(config)

        self.assertEqual(len(result.species), 1)
        self.assertEqual(result.species[0].observation_count, 9)
        self.assertEqual(result.species[0].sources, ("iNaturalist", "eBird"))
        self.assertTrue(all(provider.status == "ok" for provider in result.providers))

    def test_combined_mode_continues_when_ebird_fails(self) -> None:
        inaturalist = BirdSpecies(12942, "Eastern Bluebird", "Sialia sialis", 9, "iNaturalist")
        with TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.toml"
            config_path.write_text(
                CONFIG.replace(
                    "[discovery]\n",
                    '[discovery]\nsource = "combined"\nebird_api_key = "secret"\n',
                )
            )
            config = load_config(config_path)
            with (
                patch(
                    "inky_bird_frame.controller.resolve_discovery_location",
                    return_value=DiscoveryLocation("12345", "Exampleville", "XY", 1.0, 2.0),
                ),
                patch(
                    "inky_bird_frame.controller.fetch_inaturalist_birds",
                    return_value=[inaturalist],
                ),
                patch(
                    "inky_bird_frame.controller.fetch_ebird_observations",
                    side_effect=DataSourceError("eBird unavailable"),
                ),
            ):
                result = discover_species(config)

        self.assertEqual(result.species, [inaturalist])
        self.assertEqual(result.providers[1].status, "error")


if __name__ == "__main__":
    unittest.main()
