from __future__ import annotations

import hashlib
import json
import random
import unittest
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inky_bird_frame.config import DisplayNodeConfig, RotationMode
from inky_bird_frame.display_node import _read_state, parse_catalog_entries, run_display_cycle
from inky_bird_frame.errors import CatalogError


def catalog_payload(images: dict[int, bytes]) -> dict[str, object]:
    species: list[dict[str, object]] = []
    for taxon_id, image in images.items():
        species.append(
            {
                "taxon_id": taxon_id,
                "common_name": f"Bird {taxon_id}",
                "scientific_name": f"Species {taxon_id}",
                "slug": f"bird-{taxon_id}",
                "portrait_path": f"species/{taxon_id}/portrait.png",
                "portrait_sha256": "b" * 64,
                "display_path": f"{taxon_id}.png",
                "display_sha256": hashlib.sha256(image).hexdigest(),
                "approved_at": "2026-07-09T00:00:00+00:00",
            }
        )
    return {"schema_version": 1, "species": species}


def asset_response(images: dict[int, bytes]) -> Callable[[str, float], bytes]:
    def get_asset(url: str, timeout: float) -> bytes:
        del timeout
        taxon_id = int(url.rsplit("/", maxsplit=1)[-1].removesuffix(".png"))
        return images[taxon_id]

    return get_asset


class DisplayNodeTests(unittest.TestCase):
    def test_downloads_verifies_displays_then_skips_unchanged_single_plate(self) -> None:
        image = b"display-image"
        digest = hashlib.sha256(image).hexdigest()
        payload = {
            "schema_version": 1,
            "species": [
                {
                    "taxon_id": 12942,
                    "common_name": "Eastern Bluebird",
                    "scientific_name": "Sialia sialis",
                    "slug": "eastern-bluebird",
                    "portrait_path": "species/12942-eastern-bluebird/portrait.png",
                    "portrait_sha256": "b" * 64,
                    "display_path": "species/12942-eastern-bluebird/display.png",
                    "display_sha256": digest,
                    "approved_at": "2026-07-09T00:00:00+00:00",
                }
            ],
        }
        with TemporaryDirectory() as temporary:
            config = DisplayNodeConfig("http://controller.test", Path(temporary))
            with (
                patch("inky_bird_frame.display_node.get_json", return_value=payload),
                patch("inky_bird_frame.display_node.get_bytes", return_value=image) as get_bytes,
                patch("inky_bird_frame.display_node.show_on_inky", return_value=(1600, 1200)),
            ):
                first = run_display_cycle(config)
                second = run_display_cycle(config)

        self.assertEqual(first["display_update"], "sent")
        self.assertEqual(second["display_update"], "unchanged")
        get_bytes.assert_called_once()

    def test_unchanged_plate_advances_to_a_new_catalog_entry(self) -> None:
        first_image = b"first"
        second_image = b"second"
        first_digest = hashlib.sha256(first_image).hexdigest()
        second_digest = hashlib.sha256(second_image).hexdigest()
        entry = {
            "scientific_name": "Example bird",
            "portrait_path": "portrait.png",
            "portrait_sha256": "b" * 64,
            "approved_at": "2026-07-09T00:00:00+00:00",
        }
        payload = {
            "schema_version": 1,
            "species": [
                {
                    **entry,
                    "taxon_id": 1,
                    "common_name": "First bird",
                    "slug": "first-bird",
                    "display_path": "first.png",
                    "display_sha256": first_digest,
                },
                {
                    **entry,
                    "taxon_id": 2,
                    "common_name": "Second bird",
                    "slug": "second-bird",
                    "display_path": "second.png",
                    "display_sha256": second_digest,
                },
            ],
        }
        with TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "state.json").write_text(
                json.dumps({"next_index": 0, "last_sha256": first_digest})
            )
            config = DisplayNodeConfig("http://controller.test", state_dir)
            with (
                patch("inky_bird_frame.display_node.get_json", return_value=payload),
                patch(
                    "inky_bird_frame.display_node.get_bytes", return_value=second_image
                ) as get_bytes,
                patch("inky_bird_frame.display_node.show_on_inky", return_value=(1600, 1200)),
            ):
                unchanged = run_display_cycle(config)
                updated = run_display_cycle(config)
                state = json.loads((state_dir / "state.json").read_text())

        self.assertEqual(unchanged["display_update"], "unchanged")
        self.assertEqual(updated["taxon_id"], 2)
        self.assertEqual(state["schema_version"], 3)
        get_bytes.assert_called_once()

    def test_checksum_mismatch_fails_without_display_or_state_advance(self) -> None:
        payload = catalog_payload({1: b"one"})
        with TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            config = DisplayNodeConfig("http://controller.test", Path(temporary))
            with (
                patch("inky_bird_frame.display_node.get_json", return_value=payload),
                patch("inky_bird_frame.display_node.get_bytes", return_value=b"tampered"),
                patch("inky_bird_frame.display_node.show_on_inky") as show_on_inky,
                self.assertRaisesRegex(CatalogError, "checksum mismatch"),
            ):
                run_display_cycle(config)

            self.assertFalse(state_path.exists())
        show_on_inky.assert_not_called()

    def test_successful_cycle_reports_display_success(self) -> None:
        payload = catalog_payload({1: b"one"})
        requested: list[str] = []

        def fake_get_json(url: str, timeout: float) -> object:
            requested.append(url)
            return payload if url.endswith("/v1/catalog") else {"ok": True}

        with TemporaryDirectory() as temporary:
            config = DisplayNodeConfig("http://controller.test", Path(temporary))
            with (
                patch("inky_bird_frame.display_node.get_json", side_effect=fake_get_json),
                patch("inky_bird_frame.display_node.get_bytes", return_value=b"one"),
                patch("inky_bird_frame.display_node.show_on_inky", return_value=(1600, 1200)),
            ):
                run_display_cycle(config)

        self.assertEqual(requested[-1], "http://controller.test/v1/display-success")

    def test_failed_cycle_does_not_report_display_success(self) -> None:
        payload = catalog_payload({1: b"one"})
        requested: list[str] = []

        def fake_get_json(url: str, timeout: float) -> object:
            requested.append(url)
            return payload

        with TemporaryDirectory() as temporary:
            config = DisplayNodeConfig("http://controller.test", Path(temporary))
            with (
                patch("inky_bird_frame.display_node.get_json", side_effect=fake_get_json),
                patch("inky_bird_frame.display_node.get_bytes", return_value=b"tampered"),
                patch("inky_bird_frame.display_node.show_on_inky"),
                self.assertRaisesRegex(CatalogError, "checksum mismatch"),
            ):
                run_display_cycle(config)

        self.assertEqual([url for url in requested if url.endswith("/v1/display-success")], [])

    def test_successful_cycle_evicts_stale_cache_for_same_taxon_only(self) -> None:
        images = {1: b"one"}
        payload = catalog_payload(images)
        digest = hashlib.sha256(images[1]).hexdigest()
        with TemporaryDirectory() as temporary:
            cache_dir = Path(temporary) / "cache"
            cache_dir.mkdir(parents=True)
            stale = cache_dir / "1-oldhash00000.png"
            stale.write_bytes(b"stale")
            other_taxon = cache_dir / "13-otherhash00.png"
            other_taxon.write_bytes(b"other")
            config = DisplayNodeConfig("http://controller.test", Path(temporary))
            with (
                patch("inky_bird_frame.display_node.get_json", return_value=payload),
                patch("inky_bird_frame.display_node.get_bytes", side_effect=asset_response(images)),
                patch("inky_bird_frame.display_node.show_on_inky", return_value=(1600, 1200)),
            ):
                result = run_display_cycle(config)

            self.assertEqual(result["display_update"], "sent")
            self.assertFalse(stale.exists())
            self.assertTrue(other_taxon.exists())
            self.assertTrue((cache_dir / f"1-{digest[:12]}.png").exists())

    def test_unicode_display_path_is_percent_encoded_in_asset_url(self) -> None:
        image = b"unicode-plate"
        payload = {
            "schema_version": 1,
            "species": [
                {
                    "taxon_id": 4711,
                    "common_name": "Piopío",
                    "scientific_name": "Turnagra capensis",
                    "slug": "piopío",
                    "portrait_path": "species/4711-piopío/portrait.png",
                    "portrait_sha256": "b" * 64,
                    "display_path": "species/4711-piopío/display.png",
                    "display_sha256": hashlib.sha256(image).hexdigest(),
                    "approved_at": "2026-07-09T00:00:00+00:00",
                }
            ],
        }
        with TemporaryDirectory() as temporary:
            config = DisplayNodeConfig("http://controller.test", Path(temporary))
            with (
                patch("inky_bird_frame.display_node.get_json", return_value=payload),
                patch("inky_bird_frame.display_node.get_bytes", return_value=image) as get_bytes,
                patch("inky_bird_frame.display_node.show_on_inky", return_value=(1600, 1200)),
            ):
                result = run_display_cycle(config)

        self.assertEqual(result["display_update"], "sent")
        self.assertEqual(
            get_bytes.call_args.args[0],
            "http://controller.test/v1/assets/species/4711-piop%C3%ADo/display.png",
        )

    def test_shuffle_bag_persists_across_cycles_without_replacement(self) -> None:
        images = {1: b"one", 2: b"two", 3: b"three"}
        payload = catalog_payload(images)
        with TemporaryDirectory() as temporary:
            config = DisplayNodeConfig(
                "http://controller.test",
                Path(temporary),
                RotationMode.SHUFFLE_BAG,
            )
            with (
                patch("inky_bird_frame.display_node.get_json", return_value=payload),
                patch("inky_bird_frame.display_node.get_bytes", side_effect=asset_response(images)),
                patch("inky_bird_frame.display_node.show_on_inky", return_value=(1600, 1200)),
            ):
                first = run_display_cycle(config, rng=random.Random(7))
                second = run_display_cycle(config, rng=random.Random(99))
                state = json.loads((Path(temporary) / "state.json").read_text())

        self.assertNotEqual(first["taxon_id"], second["taxon_id"])
        self.assertEqual(state["schema_version"], 3)
        self.assertEqual(len(state["shuffle_bag_seen"]), 2)
        self.assertEqual(len(state["shuffle_bag_remaining"]), 1)

    def test_shuffle_bag_state_is_independent_from_shuffle_on_mode_switch(self) -> None:
        images = {1: b"one", 2: b"two", 3: b"three"}
        payload = catalog_payload(images)
        with TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "next_index": 0,
                        "last_sha256": "",
                        "last_taxon_id": 1,
                        "shuffle_remaining": [2],
                        "shuffle_bag_remaining": [3],
                        "shuffle_bag_seen": [1, 2],
                    }
                )
            )
            shuffle_bag_config = DisplayNodeConfig(
                "http://controller.test", Path(temporary), RotationMode.SHUFFLE_BAG
            )
            shuffle_config = DisplayNodeConfig(
                "http://controller.test", Path(temporary), RotationMode.SHUFFLE
            )
            with (
                patch("inky_bird_frame.display_node.get_json", return_value=payload),
                patch("inky_bird_frame.display_node.get_bytes", side_effect=asset_response(images)),
                patch("inky_bird_frame.display_node.show_on_inky", return_value=(1600, 1200)),
            ):
                bag_result = run_display_cycle(shuffle_bag_config, rng=random.Random(1))
                after_bag = json.loads(state_path.read_text())
                shuffle_result = run_display_cycle(shuffle_config, rng=random.Random(1))

        self.assertEqual(bag_result["taxon_id"], 3)
        self.assertEqual(after_bag["shuffle_remaining"], [2])
        self.assertEqual(shuffle_result["taxon_id"], 2)

    def test_display_failure_does_not_advance_shuffle_bag_state(self) -> None:
        images = {1: b"one", 2: b"two"}
        payload = catalog_payload(images)
        original_state = {
            "schema_version": 2,
            "next_index": 0,
            "last_sha256": "",
            "last_taxon_id": 1,
            "shuffle_remaining": [],
            "shuffle_bag_remaining": [2],
            "shuffle_bag_seen": [1],
        }
        with TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            state_path.write_text(json.dumps(original_state))
            config = DisplayNodeConfig(
                "http://controller.test", Path(temporary), RotationMode.SHUFFLE_BAG
            )
            with (
                patch("inky_bird_frame.display_node.get_json", return_value=payload),
                patch("inky_bird_frame.display_node.get_bytes", side_effect=asset_response(images)),
                patch(
                    "inky_bird_frame.display_node.show_on_inky", side_effect=OSError("panel failed")
                ),
                self.assertRaisesRegex(OSError, "panel failed"),
            ):
                run_display_cycle(config)
            state_after_failure = json.loads(state_path.read_text())

        self.assertEqual(state_after_failure, original_state)

    def test_rejects_malformed_state_and_accepts_legacy_state(self) -> None:
        with TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            state_path.write_text(json.dumps({"next_index": 1, "shuffle_remaining": [2]}))
            legacy = _read_state(state_path)

            state_path.write_text(json.dumps({"schema_version": 4}))
            with self.assertRaises(CatalogError):
                _read_state(state_path)
            state_path.write_text(json.dumps({"schema_version": 3}))
            with self.assertRaises(CatalogError):
                _read_state(state_path)
            state_path.write_text(json.dumps({"shuffle_bag_remaining": [1, 1]}))
            with self.assertRaises(CatalogError):
                _read_state(state_path)
            state_path.write_text(json.dumps({"shuffle_bag_remaining": [0]}))
            with self.assertRaises(CatalogError):
                _read_state(state_path)
            state_path.write_bytes(b"\xff")
            with self.assertRaises(CatalogError):
                _read_state(state_path)

        self.assertEqual(legacy.next_index, 1)
        self.assertEqual(legacy.shuffle_remaining, (2,))

    def test_latest_detection_preempts_rotation_once_then_newer_detection_preempts(self) -> None:
        images = {1: b"one", 2: b"two", 3: b"three"}
        payload = catalog_payload(images)
        species = payload["species"]
        assert isinstance(species, list)
        species[1]["latest_detection_at"] = "2026-07-13T08:00:00-04:00"
        species[2]["latest_detection_at"] = "2026-07-13T08:05:00-04:00"
        with TemporaryDirectory() as temporary:
            config = DisplayNodeConfig("http://controller.test", Path(temporary))
            with (
                patch("inky_bird_frame.display_node.get_json", return_value=payload),
                patch("inky_bird_frame.display_node.get_bytes", side_effect=asset_response(images)),
                patch("inky_bird_frame.display_node.show_on_inky", return_value=(1600, 1200)),
            ):
                first = run_display_cycle(config)
                second = run_display_cycle(config)
                species[1]["latest_detection_at"] = "2026-07-13T08:10:00-04:00"
                third = run_display_cycle(config)
                state = json.loads((Path(temporary) / "state.json").read_text())

        self.assertEqual((first["taxon_id"], first["selection_reason"]), (3, "latest_detection"))
        self.assertEqual((second["taxon_id"], second["selection_reason"]), (1, "rotation"))
        self.assertEqual((third["taxon_id"], third["selection_reason"]), (2, "latest_detection"))
        self.assertEqual(state["last_prioritized_detection_at"], "2026-07-13T08:10:00-04:00")

    def test_latest_detection_does_not_repeat_when_it_is_next_in_sequence(self) -> None:
        images = {1: b"one", 2: b"two"}
        payload = catalog_payload(images)
        species = payload["species"]
        assert isinstance(species, list)
        species[0]["latest_detection_at"] = "2026-07-13T08:10:00-04:00"
        with TemporaryDirectory() as temporary:
            config = DisplayNodeConfig("http://controller.test", Path(temporary))
            with (
                patch("inky_bird_frame.display_node.get_json", return_value=payload),
                patch("inky_bird_frame.display_node.get_bytes", side_effect=asset_response(images)),
                patch("inky_bird_frame.display_node.show_on_inky", return_value=(1600, 1200)),
            ):
                first = run_display_cycle(config)
                second = run_display_cycle(config)

        self.assertEqual((first["taxon_id"], first["selection_reason"]), (1, "latest_detection"))
        self.assertEqual((second["taxon_id"], second["selection_reason"]), (2, "rotation"))

    def test_equal_latest_detection_timestamps_are_each_prioritized(self) -> None:
        images = {1: b"one", 2: b"two", 3: b"three"}
        payload = catalog_payload(images)
        species = payload["species"]
        assert isinstance(species, list)
        species[0]["latest_detection_at"] = "2026-07-13T08:10:00-04:00"
        species[1]["latest_detection_at"] = "2026-07-13T12:10:00+00:00"
        with TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            config = DisplayNodeConfig("http://controller.test", Path(temporary))
            with (
                patch("inky_bird_frame.display_node.get_json", return_value=payload),
                patch("inky_bird_frame.display_node.get_bytes", side_effect=asset_response(images)),
                patch("inky_bird_frame.display_node.show_on_inky", return_value=(1600, 1200)),
            ):
                first = run_display_cycle(config)
                second = run_display_cycle(config)
                third = run_display_cycle(config)
                state = json.loads(state_path.read_text())

        self.assertEqual((first["taxon_id"], first["selection_reason"]), (1, "latest_detection"))
        self.assertEqual((second["taxon_id"], second["selection_reason"]), (2, "latest_detection"))
        self.assertEqual(third["selection_reason"], "rotation")
        self.assertEqual(state["prioritized_detection_taxa"], [1, 2])

    def test_latest_detection_seeds_empty_shuffle_without_selected_taxon(self) -> None:
        images = {1: b"one", 2: b"two", 3: b"three"}
        payload = catalog_payload(images)
        species = payload["species"]
        assert isinstance(species, list)
        species[1]["latest_detection_at"] = "2026-07-13T08:10:00-04:00"
        with TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            config = DisplayNodeConfig(
                "http://controller.test", Path(temporary), RotationMode.SHUFFLE
            )
            with (
                patch("inky_bird_frame.display_node.get_json", return_value=payload),
                patch("inky_bird_frame.display_node.get_bytes", side_effect=asset_response(images)),
                patch("inky_bird_frame.display_node.show_on_inky", return_value=(1600, 1200)),
            ):
                first = run_display_cycle(config, rng=random.Random(3))
                state = json.loads(state_path.read_text())
                second = run_display_cycle(config, rng=random.Random(3))
                third = run_display_cycle(config, rng=random.Random(3))

        self.assertEqual((first["taxon_id"], first["selection_reason"]), (2, "latest_detection"))
        self.assertCountEqual(state["shuffle_remaining"], [1, 3])
        self.assertNotIn(2, {second["taxon_id"], third["taxon_id"]})

    def test_latest_detection_failure_does_not_consume_watermark(self) -> None:
        images = {1: b"one", 2: b"two"}
        payload = catalog_payload(images)
        species = payload["species"]
        assert isinstance(species, list)
        species[1]["latest_detection_at"] = "2026-07-13T08:10:00-04:00"
        with TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            config = DisplayNodeConfig("http://controller.test", Path(temporary))
            with (
                patch("inky_bird_frame.display_node.get_json", return_value=payload),
                patch("inky_bird_frame.display_node.get_bytes", side_effect=asset_response(images)),
                patch(
                    "inky_bird_frame.display_node.show_on_inky", side_effect=OSError("panel failed")
                ),
                self.assertRaisesRegex(OSError, "panel failed"),
            ):
                run_display_cycle(config)

            self.assertFalse(state_path.exists())

    def test_latest_detection_counts_as_shown_in_shuffle_bag(self) -> None:
        images = {1: b"one", 2: b"two", 3: b"three"}
        payload = catalog_payload(images)
        species = payload["species"]
        assert isinstance(species, list)
        species[2]["latest_detection_at"] = "2026-07-13T08:10:00-04:00"
        initial_state = {
            "schema_version": 2,
            "next_index": 0,
            "last_sha256": hashlib.sha256(images[1]).hexdigest(),
            "last_taxon_id": 1,
            "shuffle_remaining": [],
            "shuffle_bag_remaining": [2, 3],
            "shuffle_bag_seen": [1],
        }
        with TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            state_path.write_text(json.dumps(initial_state))
            config = DisplayNodeConfig(
                "http://controller.test", Path(temporary), RotationMode.SHUFFLE_BAG
            )
            with (
                patch("inky_bird_frame.display_node.get_json", return_value=payload),
                patch("inky_bird_frame.display_node.get_bytes", side_effect=asset_response(images)),
                patch("inky_bird_frame.display_node.show_on_inky", return_value=(1600, 1200)),
            ):
                result = run_display_cycle(config, rng=random.Random(1))
                state = json.loads(state_path.read_text())

        self.assertEqual((result["taxon_id"], result["selection_reason"]), (3, "latest_detection"))
        self.assertEqual(state["shuffle_bag_remaining"], [2])
        self.assertEqual(state["shuffle_bag_seen"], [1, 3])

    def test_latest_detection_priority_can_be_disabled(self) -> None:
        images = {1: b"one", 2: b"two"}
        payload = catalog_payload(images)
        species = payload["species"]
        assert isinstance(species, list)
        species[1]["latest_detection_at"] = "2026-07-13T08:10:00-04:00"
        with TemporaryDirectory() as temporary:
            config = DisplayNodeConfig(
                "http://controller.test",
                Path(temporary),
                prioritize_latest_detection=False,
            )
            with (
                patch("inky_bird_frame.display_node.get_json", return_value=payload),
                patch("inky_bird_frame.display_node.get_bytes", side_effect=asset_response(images)),
                patch("inky_bird_frame.display_node.show_on_inky", return_value=(1600, 1200)),
            ):
                result = run_display_cycle(config)

        self.assertEqual((result["taxon_id"], result["selection_reason"]), (1, "rotation"))

    def test_rejects_invalid_or_timezone_free_detection_timestamps(self) -> None:
        payload = catalog_payload({1: b"one"})
        species = payload["species"]
        assert isinstance(species, list)
        species[0]["latest_detection_at"] = "not-a-timestamp"
        with self.assertRaisesRegex(CatalogError, "latest detection timestamp"):
            parse_catalog_entries(payload)

        species[0]["latest_detection_at"] = "2026-07-13T08:10:00"
        with self.assertRaisesRegex(CatalogError, "latest detection timestamp"):
            parse_catalog_entries(payload)

    def test_rejects_duplicate_or_empty_catalogs(self) -> None:
        payload = catalog_payload({1: b"one"})
        cast_species = payload["species"]
        assert isinstance(cast_species, list)
        duplicate = dict(cast_species[0])
        cast_species.append(duplicate)

        with self.assertRaises(CatalogError):
            parse_catalog_entries(payload)
        invalid_taxon = catalog_payload({1: b"one"})
        invalid_species = invalid_taxon["species"]
        assert isinstance(invalid_species, list)
        invalid_species[0]["taxon_id"] = True
        with self.assertRaises(CatalogError):
            parse_catalog_entries(invalid_taxon)
        with self.assertRaises(CatalogError):
            parse_catalog_entries({"schema_version": 1, "species": []})

    def test_rejects_overlapping_display_cycles_before_fetching_catalog(self) -> None:
        with TemporaryDirectory() as temporary:
            config = DisplayNodeConfig("http://controller.test", Path(temporary))
            with (
                patch("inky_bird_frame.display_node.fcntl.flock", side_effect=BlockingIOError),
                patch("inky_bird_frame.display_node.get_json") as get_json,
                self.assertRaisesRegex(CatalogError, "already running"),
            ):
                run_display_cycle(config)

        get_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
