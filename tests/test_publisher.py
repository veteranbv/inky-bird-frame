from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import call, patch

from PIL import Image, PngImagePlugin

from inky_bird_frame.catalog import rebuild_catalog_index, sha256_file
from inky_bird_frame.config import PublicCatalogConfig, load_config
from inky_bird_frame.errors import CatalogPublishError
from inky_bird_frame.http import write_json_atomic
from inky_bird_frame.publisher import (
    _catalog_transaction_prefix,
    _check_json_privacy,
    _remote_repository,
    _validate_checkout,
    replace_public_catalog_taxon,
    run_catalog_publish,
    sync_public_catalog,
    validate_catalog_additions,
    validate_public_catalog,
)


def _run_git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _create_species(catalog: Path, taxon_id: int, common_name: str) -> Path:
    slug = common_name.casefold().replace(" ", "-")
    directory = catalog / "species" / f"{taxon_id}-{slug}"
    directory.mkdir(parents=True)
    Image.new("RGB", (1200, 1600), "white").save(directory / "portrait.png")
    Image.new("RGB", (1600, 1200), "white").save(directory / "display.png")
    profile = {
        "taxon_id": taxon_id,
        "common_name": common_name,
        "scientific_name": "Avis exemplaris",
        "family": "Exemplaridae",
        "measurements": {"length": "10 cm", "wingspan": "20 cm", "weight": "10 g"},
        "field_marks": ["example field mark"],
        "habitat": "Woodland",
        "behavior": "Forages",
        "palette": ["white"],
        "sources": [
            {"title": "Source one", "url": "https://example.test/one"},
            {"title": "Source two", "url": "https://example.test/two"},
        ],
    }
    review = {
        "passed": True,
        "species_accuracy": 5,
        "anatomy_accuracy": 5,
        "text_accuracy": 5,
        "composition_quality": 5,
        "location_free": True,
        "findings": [],
        "verification_sources": [
            {"title": "Source one", "url": "https://example.test/one"},
            {"title": "Source two", "url": "https://example.test/two"},
        ],
    }
    write_json_atomic(directory / "profile.json", profile)
    write_json_atomic(directory / "quality-review.json", review)
    write_json_atomic(
        directory / "manifest.json",
        {
            "schema_version": 1,
            "status": "approved",
            "taxon_id": taxon_id,
            "common_name": common_name,
            "scientific_name": "Avis exemplaris",
            "slug": slug,
            "approved_at": "2026-07-09T00:00:00+00:00",
            "profile": profile,
            "references": [],
            "generation": {
                "generator": "test",
                "prompt_version": "test-v1",
                "generated_at": "2026-07-09T00:00:00+00:00",
                "attempt": 1,
                "max_attempts": 3,
            },
            "quality_review": review,
            "assets": {
                "portrait": {
                    "filename": "portrait.png",
                    "sha256": sha256_file(directory / "portrait.png"),
                },
                "display": {
                    "filename": "display.png",
                    "sha256": sha256_file(directory / "display.png"),
                },
            },
        },
    )
    rebuild_catalog_index(catalog)
    return directory


def _create_portrait_replacement(
    catalog: Path,
    taxon_id: int,
    common_name: str,
    *,
    approved_at: str,
    color: str,
) -> Path:
    directory = _create_species(catalog, taxon_id, common_name)
    portrait = directory / "portrait.png"
    Image.new("RGB", (1200, 1600), color).save(portrait)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["approved_at"] = approved_at
    manifest["assets"]["portrait"]["sha256"] = sha256_file(portrait)
    write_json_atomic(manifest_path, manifest)
    rebuild_catalog_index(catalog)
    return directory


def _write_config(root: Path, checkout: Path) -> Path:
    path = root / "config.toml"
    path.write_text(
        f"""
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

[public_catalog]
enabled = true
checkout_dir = "{checkout}"
repository = "example/inky-bird-frame"
gh_path = "/usr/bin/false"
remote = "origin"
base_branch = "main"
commit_name = "Test Publisher"
commit_email = "publisher@example.test"
"""
    )
    return path


def _initialize_remote(root: Path) -> tuple[Path, Path]:
    remote = root / "remote.git"
    checkout = root / "checkout"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(checkout)], check=True, capture_output=True)
    _run_git(checkout, "switch", "-c", "main")
    (checkout / "README.md").write_text("# Test catalog\n")
    _run_git(checkout, "add", "README.md")
    _run_git(
        checkout,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-m",
        "Initialize catalog",
    )
    _run_git(checkout, "push", "-u", "origin", "main")
    return remote, checkout


class PublisherTests(unittest.TestCase):
    def test_birdbuddy_private_fields_are_rejected_from_catalog_json(self) -> None:
        for field in (
            "authorization_confirmed_at",
            "birdbuddy",
            "email",
            "feeder_id",
            "password",
            "postcard_id",
            "refresh_token",
        ):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(CatalogPublishError, "Private field"),
            ):
                _check_json_privacy({field: "private"}, Path("manifest.json"))

    def test_parses_only_supported_github_repository_remotes(self) -> None:
        self.assertEqual(
            _remote_repository("https://github.com/example/inky-bird-frame.git"),
            "example/inky-bird-frame",
        )
        self.assertEqual(
            _remote_repository("git@github.com:example/inky-bird-frame.git"),
            "example/inky-bird-frame",
        )
        self.assertIsNone(_remote_repository("https://example.test/example/inky-bird-frame"))
        self.assertIsNone(
            _remote_repository("https://token@github.com/example/inky-bird-frame.git")
        )

    def test_checkout_requires_github_cli_owner_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            checkout = Path(temporary) / "checkout"
            subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
            _run_git(
                checkout,
                "remote",
                "add",
                "origin",
                "https://github.com/example/inky-bird-frame.git",
            )
            publication = PublicCatalogConfig(
                enabled=True,
                checkout_dir=checkout,
                repository="example/inky-bird-frame",
                gh_path=Path("/usr/bin/false"),
            )
            gh_result = subprocess.CompletedProcess(["gh"], 0, "intruder\n", "")

            with (
                patch("inky_bird_frame.publisher._gh", return_value=gh_result),
                self.assertRaisesRegex(CatalogPublishError, "repository owner 'example'"),
            ):
                _validate_checkout(checkout, publication)

    def test_checkout_reports_github_authentication_failure_after_api_error(self) -> None:
        with TemporaryDirectory() as temporary:
            checkout = Path(temporary) / "checkout"
            subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
            _run_git(
                checkout,
                "remote",
                "add",
                "origin",
                "https://github.com/example/inky-bird-frame.git",
            )
            publication = PublicCatalogConfig(
                enabled=True,
                checkout_dir=checkout,
                repository="example/inky-bird-frame",
                gh_path=Path("/usr/bin/false"),
            )

            with (
                patch(
                    "inky_bird_frame.publisher._gh",
                    side_effect=[
                        CatalogPublishError("gh api failed: invalid response"),
                        subprocess.CompletedProcess(
                            ["gh", "auth"],
                            1,
                            "",
                            "The token in hosts.yml is invalid.",
                        ),
                    ],
                ) as github,
                self.assertRaisesRegex(CatalogPublishError, "token in hosts.yml is invalid"),
            ):
                _validate_checkout(checkout, publication)

            self.assertEqual(
                github.call_args_list,
                [
                    call(publication, "api", "user", "--jq", ".login"),
                    call(
                        publication,
                        "auth",
                        "status",
                        "--hostname",
                        "github.com",
                        check=False,
                    ),
                ],
            )

    def test_repository_seed_catalog_is_publishable(self) -> None:
        catalog = Path(__file__).parents[1] / "catalog"

        entries = validate_public_catalog(catalog)

        self.assertTrue({7562, 9083, 11935, 12942}.issubset({entry.taxon_id for entry in entries}))

    def test_validates_and_syncs_an_approved_catalog(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            _create_species(source, 1, "Example Bird")

            result = sync_public_catalog(source, destination)

            self.assertEqual(result["already_present"], [])
            self.assertEqual(
                result["published"],
                [
                    {
                        "taxon_id": 1,
                        "common_name": "Example Bird",
                        "scientific_name": "Avis exemplaris",
                        "slug": "example-bird",
                    }
                ],
            )
            self.assertEqual(len(validate_public_catalog(destination)), 1)

            repeated = sync_public_catalog(source, destination)

            self.assertEqual(repeated["published"], [])
            self.assertEqual(repeated["replaced"], [])
            self.assertEqual(repeated["already_present"], [1])

    def test_sync_applies_a_hash_bound_catalog_replacement(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "destination"
            source = root / "source"
            replacement = root / "replacement"
            _create_species(destination, 1, "Example Bird")
            shutil.copytree(destination, source)
            replacement_directory = _create_species(replacement, 1, "Example Bird")
            portrait = replacement_directory / "portrait.png"
            Image.new("RGB", (1200, 1600), "black").save(portrait)
            manifest_path = replacement_directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["approved_at"] = "2026-07-10T00:00:00+00:00"
            manifest["assets"]["portrait"]["sha256"] = sha256_file(portrait)
            write_json_atomic(manifest_path, manifest)
            rebuild_catalog_index(replacement)
            replace_public_catalog_taxon(
                replacement,
                source,
                1,
                "Human review found incorrect proportions.",
            )

            result = sync_public_catalog(source, destination, allow_replacements=True)

            self.assertEqual(result["published"], [])
            self.assertEqual(
                result["replaced"],
                [
                    {
                        "taxon_id": 1,
                        "common_name": "Example Bird",
                        "scientific_name": "Avis exemplaris",
                        "slug": "example-bird",
                    }
                ],
            )
            self.assertEqual(result["already_present"], [])
            self.assertEqual(
                (destination / "species/1-example-bird/portrait.png").read_bytes(),
                (source / "species/1-example-bird/portrait.png").read_bytes(),
            )
            self.assertEqual(len(validate_public_catalog(destination)), 1)

    def test_sync_converges_migration_metadata_on_an_already_corrected_approval(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            published = root / "published"
            corrected_local = root / "corrected-local"
            replacement = root / "replacement"
            _create_species(published, 1, "Example Bird")
            replacement_directory = _create_portrait_replacement(
                replacement,
                1,
                "Example Bird",
                approved_at="2026-07-10T00:00:00+00:00",
                color="black",
            )
            shutil.copytree(replacement, corrected_local)
            replace_public_catalog_taxon(
                replacement,
                published,
                1,
                "Human review found incorrect proportions.",
            )
            self.assertNotIn(
                "catalog_migration",
                json.loads((replacement_directory / "manifest.json").read_text()),
            )

            result = sync_public_catalog(
                published,
                corrected_local,
                allow_replacements=True,
            )

            replaced = result["replaced"]
            self.assertIsInstance(replaced, list)
            assert isinstance(replaced, list)
            self.assertEqual([item["taxon_id"] for item in replaced], [1])
            self.assertEqual(
                (corrected_local / "species/1-example-bird/manifest.json").read_bytes(),
                (published / "species/1-example-bird/manifest.json").read_bytes(),
            )
            self.assertEqual(len(validate_public_catalog(corrected_local)), 1)

    def test_sync_rejects_a_valid_replacement_without_explicit_opt_in(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "destination"
            source = root / "source"
            replacement = root / "replacement"
            original = _create_species(destination, 1, "Example Bird")
            original_portrait = (original / "portrait.png").read_bytes()
            shutil.copytree(destination, source)
            _create_portrait_replacement(
                replacement,
                1,
                "Example Bird",
                approved_at="2026-07-10T00:00:00+00:00",
                color="black",
            )
            replace_public_catalog_taxon(
                replacement,
                source,
                1,
                "Human review found incorrect proportions.",
            )

            with self.assertRaisesRegex(CatalogPublishError, "immutable local approval"):
                sync_public_catalog(source, destination)

            self.assertEqual(
                (destination / "species/1-example-bird/portrait.png").read_bytes(),
                original_portrait,
            )

    def test_repeated_replacements_preserve_ancestry_for_a_skipped_upgrade(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original"
            first = root / "first"
            first_source = root / "first-source"
            second = root / "second"
            second_source = root / "second-source"
            skipped_controller = root / "skipped-controller"
            _create_species(original, 1, "Example Bird")
            shutil.copytree(original, first)
            _create_portrait_replacement(
                first_source,
                1,
                "Example Bird",
                approved_at="2026-07-10T00:00:00+00:00",
                color="black",
            )
            replace_public_catalog_taxon(
                first_source,
                first,
                1,
                "First reviewed correction.",
            )
            shutil.copytree(first, second)
            _create_portrait_replacement(
                second_source,
                1,
                "Example Bird",
                approved_at="2026-07-11T00:00:00+00:00",
                color="gray",
            )
            replace_public_catalog_taxon(
                second_source,
                second,
                1,
                "Second reviewed correction.",
            )

            second_manifest = json.loads(
                (second / "species/1-example-bird/manifest.json").read_text()
            )
            migration = second_manifest["catalog_migration"]
            self.assertEqual(migration["reason"], "Second reviewed correction.")
            self.assertEqual(
                [record["reason"] for record in migration["history"]],
                ["First reviewed correction."],
            )
            self.assertEqual(validate_catalog_additions(first, second), [])
            tampered = root / "tampered"
            shutil.copytree(second, tampered)
            tampered_manifest_path = tampered / "species/1-example-bird/manifest.json"
            tampered_manifest = json.loads(tampered_manifest_path.read_text())
            tampered_manifest["catalog_migration"].pop("history")
            write_json_atomic(tampered_manifest_path, tampered_manifest)
            rebuild_catalog_index(tampered)
            with self.assertRaisesRegex(CatalogPublishError, "preserve migration history"):
                validate_catalog_additions(first, tampered)

            shutil.copytree(original, skipped_controller)
            result = sync_public_catalog(
                second,
                skipped_controller,
                allow_replacements=True,
            )

            replaced = result["replaced"]
            self.assertIsInstance(replaced, list)
            assert isinstance(replaced, list)
            self.assertEqual([item["taxon_id"] for item in replaced], [1])
            self.assertEqual(
                (skipped_controller / "species/1-example-bird/portrait.png").read_bytes(),
                (second / "species/1-example-bird/portrait.png").read_bytes(),
            )
            self.assertEqual(len(validate_public_catalog(skipped_controller)), 1)

    def test_interrupted_committed_cleanup_keeps_the_installed_replacement(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "destination"
            source = root / "source"
            replacement = root / "replacement"
            _create_species(destination, 1, "Example Bird")
            shutil.copytree(destination, source)
            _create_portrait_replacement(
                replacement,
                1,
                "Example Bird",
                approved_at="2026-07-10T00:00:00+00:00",
                color="black",
            )
            replace_public_catalog_taxon(
                replacement,
                source,
                1,
                "Human review found incorrect proportions.",
            )
            real_rmtree = shutil.rmtree

            def interrupt_committed_cleanup(path: Path) -> None:
                transaction = Path(path)
                committed_prefix = _catalog_transaction_prefix(destination, committed=True)
                if transaction.name.startswith(committed_prefix):
                    backup = transaction / "approved/1-example-bird"
                    real_rmtree(backup)
                    raise OSError("simulated interrupted committed cleanup")
                real_rmtree(transaction)

            with (
                patch(
                    "inky_bird_frame.publisher.shutil.rmtree",
                    side_effect=interrupt_committed_cleanup,
                ),
                self.assertRaisesRegex(OSError, "interrupted committed cleanup"),
            ):
                sync_public_catalog(source, destination, allow_replacements=True)

            committed_prefix = _catalog_transaction_prefix(destination, committed=True)
            self.assertEqual(len(list(root.glob(f"{committed_prefix}*"))), 1)
            self.assertEqual(
                (destination / "species/1-example-bird/portrait.png").read_bytes(),
                (source / "species/1-example-bird/portrait.png").read_bytes(),
            )

            recovered = sync_public_catalog(source, destination, allow_replacements=True)

            self.assertEqual(recovered["already_present"], [1])
            self.assertFalse(list(root.glob(f"{committed_prefix}*")))
            self.assertEqual(len(validate_public_catalog(destination)), 1)

    def test_sync_rolls_back_a_replacement_when_a_later_taxon_conflicts(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "destination"
            source = root / "source"
            replacement = root / "replacement"
            original = _create_species(destination, 1, "First Bird")
            _create_species(destination, 2, "Second Bird")
            original_portrait = (original / "portrait.png").read_bytes()
            shutil.copytree(destination, source)
            replacement_directory = _create_species(replacement, 1, "First Bird")
            portrait = replacement_directory / "portrait.png"
            Image.new("RGB", (1200, 1600), "black").save(portrait)
            manifest_path = replacement_directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["approved_at"] = "2026-07-10T00:00:00+00:00"
            manifest["assets"]["portrait"]["sha256"] = sha256_file(portrait)
            write_json_atomic(manifest_path, manifest)
            rebuild_catalog_index(replacement)
            replace_public_catalog_taxon(
                replacement,
                source,
                1,
                "Human review found incorrect proportions.",
            )
            conflicting_portrait = source / "species/2-second-bird/portrait.png"
            Image.new("RGB", (1200, 1600), "gray").save(conflicting_portrait)
            conflicting_manifest_path = source / "species/2-second-bird/manifest.json"
            conflicting_manifest = json.loads(conflicting_manifest_path.read_text())
            conflicting_manifest["assets"]["portrait"]["sha256"] = sha256_file(conflicting_portrait)
            write_json_atomic(conflicting_manifest_path, conflicting_manifest)
            rebuild_catalog_index(source)

            with self.assertRaisesRegex(CatalogPublishError, "changed immutable taxon 2"):
                sync_public_catalog(source, destination, allow_replacements=True)

            self.assertEqual(
                (destination / "species/1-first-bird/portrait.png").read_bytes(),
                original_portrait,
            )
            active_prefix = _catalog_transaction_prefix(destination, committed=False)
            self.assertEqual(len(list(root.glob(f"{active_prefix}*"))), 1)

    def test_syncs_only_requested_taxa_for_a_contribution(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            _create_species(source, 1, "First Bird")
            _create_species(source, 2, "Second Bird")

            result = sync_public_catalog(source, destination, taxon_ids={2})

            self.assertEqual(
                result["published"],
                [
                    {
                        "taxon_id": 2,
                        "common_name": "Second Bird",
                        "scientific_name": "Avis exemplaris",
                        "slug": "second-bird",
                    }
                ],
            )
            self.assertEqual(
                [entry.taxon_id for entry in validate_public_catalog(destination)],
                [2],
            )

    def test_sync_recovers_complete_species_when_initial_index_was_interrupted(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            _create_species(source, 1, "Example Bird")
            (destination / "species").mkdir(parents=True)
            shutil.copytree(
                source / "species/1-example-bird",
                destination / "species/1-example-bird",
            )

            result = sync_public_catalog(source, destination)

            self.assertEqual(result["published"], [])
            self.assertEqual(result["already_present"], [1])
            self.assertEqual(len(validate_public_catalog(destination)), 1)

    def test_sync_removes_interrupted_staging_directory_before_retry(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            _create_species(source, 1, "Example Bird")
            active_prefix = _catalog_transaction_prefix(destination, committed=False)
            staging = root / f"{active_prefix}interrupted/1-example-bird"
            staging.mkdir(parents=True)
            (staging / "manifest.json").write_text("partial")

            result = sync_public_catalog(source, destination)

            self.assertFalse(staging.parent.exists())
            self.assertEqual(
                result["published"],
                [
                    {
                        "taxon_id": 1,
                        "common_name": "Example Bird",
                        "scientific_name": "Avis exemplaris",
                        "slug": "example-bird",
                    }
                ],
            )
            self.assertEqual(len(validate_public_catalog(destination)), 1)

    def test_sync_recovers_stale_index_only_with_interrupted_transaction_marker(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            _create_species(source, 1, "First Bird")
            _create_species(source, 2, "Second Bird")
            _create_species(destination, 1, "First Bird")
            shutil.copytree(
                source / "species/2-second-bird",
                destination / "species/2-second-bird",
            )
            active_prefix = _catalog_transaction_prefix(destination, committed=False)
            (root / f"{active_prefix}interrupted").mkdir()

            result = sync_public_catalog(source, destination)

            self.assertEqual(result["published"], [])
            self.assertEqual(result["already_present"], [1, 2])
            self.assertEqual(len(validate_public_catalog(destination)), 2)

    def test_sync_recovery_keeps_journal_until_index_rebuild_succeeds(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            replacement = root / "replacement"
            approved = _create_species(destination, 1, "Example Bird")
            original_portrait = (approved / "portrait.png").read_bytes()
            shutil.copytree(destination, source)
            replacement_directory = _create_portrait_replacement(
                replacement,
                1,
                "Example Bird",
                approved_at="2026-07-10T00:00:00+00:00",
                color="black",
            )
            active_prefix = _catalog_transaction_prefix(destination, committed=False)
            transaction = root / f"{active_prefix}interrupted"
            backup = transaction / "approved/1-example-bird"
            backup.parent.mkdir(parents=True)
            approved.replace(backup)
            shutil.copytree(
                replacement_directory,
                destination / "species/1-example-bird",
            )

            with (
                patch(
                    "inky_bird_frame.publisher.rebuild_catalog_index",
                    side_effect=OSError("simulated interrupted index rebuild"),
                ),
                self.assertRaisesRegex(OSError, "interrupted index rebuild"),
            ):
                sync_public_catalog(source, destination, allow_replacements=True)

            self.assertTrue(transaction.exists())
            self.assertEqual(
                (destination / "species/1-example-bird/portrait.png").read_bytes(),
                original_portrait,
            )

            recovered = sync_public_catalog(source, destination, allow_replacements=True)

            self.assertEqual(recovered["already_present"], [1])
            self.assertFalse(transaction.exists())
            self.assertEqual(len(validate_public_catalog(destination)), 1)

    def test_sync_recovery_discards_a_partial_destination_before_rollback(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            approved = _create_species(destination, 1, "Example Bird")
            shutil.copytree(destination, source)
            active_prefix = _catalog_transaction_prefix(destination, committed=False)
            transaction = root / f"{active_prefix}interrupted"
            backup = transaction / "approved/1-example-bird"
            backup.parent.mkdir(parents=True)
            approved.replace(backup)
            partial = destination / "species/1-example-bird"
            partial.mkdir()
            (partial / "manifest.json").write_text("{}")

            recovered = sync_public_catalog(source, destination)

            self.assertEqual(recovered["already_present"], [1])
            self.assertFalse(transaction.exists())
            self.assertEqual(len(validate_public_catalog(destination)), 1)

    def test_sync_preserves_transaction_marker_after_partial_failure(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            recovery_source = root / "recovery-source"
            destination = root / "destination"
            _create_species(source, 1, "First Bird")
            _create_species(source, 2, "Second Bird")
            _create_species(recovery_source, 1, "First Bird")
            _create_species(destination, 2, "Conflicting Bird")

            with self.assertRaisesRegex(CatalogPublishError, "conflicts"):
                sync_public_catalog(source, destination)

            active_prefix = _catalog_transaction_prefix(destination, committed=False)
            self.assertEqual(len(list(root.glob(f"{active_prefix}*"))), 1)
            recovered = sync_public_catalog(recovery_source, destination)

            self.assertEqual(recovered["already_present"], [1])
            self.assertFalse(list(root.glob(f"{active_prefix}*")))
            self.assertEqual(len(validate_public_catalog(destination)), 2)

    def test_rejects_a_requested_taxon_missing_from_the_source_catalog(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            _create_species(source, 1, "Example Bird")

            with self.assertRaisesRegex(CatalogPublishError, "does not contain taxon 2"):
                sync_public_catalog(source, destination, taxon_ids={2})
            self.assertFalse(destination.exists())

    def test_validates_add_only_catalog_contributions(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base"
            candidate = root / "candidate"
            _create_species(base, 1, "Existing Bird")
            shutil.copytree(base, candidate)
            _create_species(candidate, 2, "New Bird")

            additions = validate_catalog_additions(base, candidate)

            self.assertEqual([entry.taxon_id for entry in additions], [2])

    def test_rejects_changes_to_base_catalog_taxa(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base"
            candidate = root / "candidate"
            _create_species(base, 1, "Existing Bird")
            shutil.copytree(base, candidate)
            portrait = candidate / "species/1-existing-bird/portrait.png"
            Image.new("RGB", (1200, 1600), "black").save(portrait)
            manifest_path = candidate / "species/1-existing-bird/manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["assets"]["portrait"]["sha256"] = sha256_file(portrait)
            write_json_atomic(manifest_path, manifest)
            rebuild_catalog_index(candidate)

            with self.assertRaisesRegex(CatalogPublishError, "changed immutable taxon 1"):
                validate_catalog_additions(base, candidate)

    def test_validates_explicit_hash_bound_catalog_replacement(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base"
            candidate = root / "candidate"
            source = root / "source"
            original = _create_species(base, 1, "Existing Bird")
            shutil.copytree(base, candidate)
            replacement = _create_species(source, 1, "Existing Bird")
            Image.new("RGB", (1200, 1600), "black").save(replacement / "portrait.png")
            manifest_path = replacement / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["approved_at"] = "2026-07-10T00:00:00+00:00"
            manifest["assets"]["portrait"]["sha256"] = sha256_file(replacement / "portrait.png")
            write_json_atomic(manifest_path, manifest)
            rebuild_catalog_index(source)

            result = replace_public_catalog_taxon(
                source,
                candidate,
                1,
                "Human review found incorrect proportions.",
            )

            replaced = result["replaced"]
            self.assertIsInstance(replaced, dict)
            assert isinstance(replaced, dict)
            self.assertEqual(replaced["taxon_id"], 1)
            self.assertEqual(validate_catalog_additions(base, candidate), [])
            replaced_manifest = json.loads(
                (candidate / "species/1-existing-bird/manifest.json").read_text()
            )
            self.assertEqual(
                replaced_manifest["catalog_migration"],
                {
                    "reason": "Human review found incorrect proportions.",
                    "replaces": {
                        "approved_at": "2026-07-09T00:00:00+00:00",
                        "display_sha256": sha256_file(original / "display.png"),
                        "portrait_sha256": sha256_file(original / "portrait.png"),
                    },
                },
            )

    def test_catalog_replacement_recovers_an_interrupted_approval_move(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "destination"
            replacement = root / "replacement"
            approved = _create_species(destination, 1, "Existing Bird")
            _create_portrait_replacement(
                replacement,
                1,
                "Existing Bird",
                approved_at="2026-07-10T00:00:00+00:00",
                color="black",
            )
            active_prefix = _catalog_transaction_prefix(destination, committed=False)
            interrupted = root / f"{active_prefix}interrupted"
            backup = interrupted / "approved/1-existing-bird"
            backup.parent.mkdir(parents=True)
            approved.replace(backup)

            result = replace_public_catalog_taxon(
                replacement,
                destination,
                1,
                "Human review found incorrect proportions.",
            )

            replaced = result["replaced"]
            self.assertIsInstance(replaced, dict)
            assert isinstance(replaced, dict)
            self.assertEqual(replaced["taxon_id"], 1)
            self.assertFalse(list(root.glob(f"{active_prefix}*")))
            self.assertEqual(
                (destination / "species/1-existing-bird/portrait.png").read_bytes(),
                (replacement / "species/1-existing-bird/portrait.png").read_bytes(),
            )
            self.assertEqual(len(validate_public_catalog(destination)), 1)

    def test_rejects_catalog_replacement_with_wrong_base_hash(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base"
            candidate = root / "candidate"
            _create_species(base, 1, "Existing Bird")
            shutil.copytree(base, candidate)
            portrait = candidate / "species/1-existing-bird/portrait.png"
            Image.new("RGB", (1200, 1600), "black").save(portrait)
            manifest_path = candidate / "species/1-existing-bird/manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["approved_at"] = "2026-07-10T00:00:00+00:00"
            manifest["assets"]["portrait"]["sha256"] = sha256_file(portrait)
            manifest["catalog_migration"] = {
                "reason": "Human review found incorrect proportions.",
                "replaces": {
                    "approved_at": "2026-07-09T00:00:00+00:00",
                    "display_sha256": "0" * 64,
                    "portrait_sha256": "0" * 64,
                },
            }
            write_json_atomic(manifest_path, manifest)
            rebuild_catalog_index(candidate)

            with self.assertRaisesRegex(CatalogPublishError, "does not match"):
                validate_catalog_additions(base, candidate)

    def test_rejects_removing_a_base_catalog_taxon(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base"
            candidate = root / "candidate"
            _create_species(base, 1, "Existing Bird")
            _create_species(candidate, 2, "Different Bird")

            with self.assertRaisesRegex(CatalogPublishError, "removed immutable taxon 1"):
                validate_catalog_additions(base, candidate)

    def test_rejects_private_manifest_fields(self) -> None:
        for field in (
            "zip_code",
            "postal_code",
            "country_code",
            "geocoder",
            "geocoder_attribution",
            "geoapify_api_key",
            "geoapify_api_key_env",
            "birdnet_go_url",
            "apiKey",
            "api_key",
            "postalCode",
            "countryCode",
            "geocoderAttribution",
            "geoapifyApiKey",
            "birdnetGoUrl",
        ):
            with self.subTest(field=field), TemporaryDirectory() as temporary:
                catalog = Path(temporary) / "catalog"
                directory = _create_species(catalog, 1, "Example Bird")
                manifest_path = directory / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                manifest[field] = "private"
                write_json_atomic(manifest_path, manifest)

                with self.assertRaisesRegex(CatalogPublishError, "Private field"):
                    validate_public_catalog(catalog)

    def test_rejects_unexpected_catalog_root_files(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            _create_species(catalog, 1, "Example Bird")
            (catalog / "notes.txt").write_text("not part of the catalog contract")

            with self.assertRaisesRegex(CatalogPublishError, "Unexpected catalog root entries"):
                validate_public_catalog(catalog)

    def test_rejects_windows_unc_paths(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            directory = _create_species(catalog, 1, "Example Bird")
            manifest_path = directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["source_path"] = r"\\server\share\bird.png"
            write_json_atomic(manifest_path, manifest)

            with self.assertRaisesRegex(CatalogPublishError, "Local path"):
                validate_public_catalog(catalog)

    def test_rejects_duplicate_taxon_ids(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            _create_species(catalog, 1, "Example Bird")
            _create_species(catalog, 1, "Second Bird")

            with self.assertRaisesRegex(CatalogPublishError, "duplicate taxon ID 1"):
                validate_public_catalog(catalog)

    def test_rejects_symlinks_in_catalog_entries(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            directory = _create_species(catalog, 1, "Example Bird")
            profile = directory / "profile.json"
            profile.unlink()
            profile.symlink_to(directory / "manifest.json")

            with self.assertRaisesRegex(CatalogPublishError, "regular files"):
                validate_public_catalog(catalog)

    def test_rejects_symlinked_destination_root_before_writing(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            target = root / "target"
            target.mkdir()
            destination = root / "destination"
            destination.symlink_to(target, target_is_directory=True)
            _create_species(source, 1, "Example Bird")

            with self.assertRaisesRegex(CatalogPublishError, "Catalog root"):
                sync_public_catalog(source, destination)

    def test_rejects_image_metadata(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            directory = _create_species(catalog, 1, "Example Bird")
            portrait = directory / "portrait.png"
            metadata = PngImagePlugin.PngInfo()
            metadata.add_text("location", "private")
            Image.new("RGB", (1200, 1600), "white").save(portrait, pnginfo=metadata)
            manifest_path = directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["assets"]["portrait"]["sha256"] = sha256_file(portrait)
            write_json_atomic(manifest_path, manifest)

            with self.assertRaisesRegex(CatalogPublishError, "metadata is not allowed"):
                validate_public_catalog(catalog)

    def test_rejects_asset_checksum_mismatch(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            directory = _create_species(catalog, 1, "Example Bird")
            manifest_path = directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["assets"]["portrait"]["sha256"] = "0" * 64
            write_json_atomic(manifest_path, manifest)

            with self.assertRaisesRegex(CatalogPublishError, "checksum does not match"):
                validate_public_catalog(catalog)

    def test_rejects_index_that_does_not_match_manifests(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            _create_species(catalog, 1, "Example Bird")
            index_path = catalog / "index.json"
            index = json.loads(index_path.read_text())
            index["species"] = []
            write_json_atomic(index_path, index)

            with self.assertRaisesRegex(CatalogPublishError, "index does not match"):
                validate_public_catalog(catalog)

    def test_rejects_an_automated_review_below_threshold(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            directory = _create_species(catalog, 1, "Example Bird")
            manifest_path = directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["quality_review"]["species_accuracy"] = 3
            write_json_atomic(manifest_path, manifest)

            with self.assertRaisesRegex(CatalogPublishError, "lacks a publishable quality review"):
                validate_public_catalog(catalog)

    def test_rejects_passing_review_with_actionable_corrections(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            directory = _create_species(catalog, 1, "Example Bird")
            manifest_path = directory / "manifest.json"
            review_path = directory / "quality-review.json"
            manifest = json.loads(manifest_path.read_text())
            review = manifest["quality_review"]
            review["correction_findings"] = ["Correct the ruler"]
            write_json_atomic(review_path, review)
            write_json_atomic(manifest_path, manifest)

            with self.assertRaisesRegex(CatalogPublishError, "lacks a publishable quality review"):
                validate_public_catalog(catalog)

    def test_rejects_invalid_correction_source_checksum(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            directory = _create_species(catalog, 1, "Example Bird")
            manifest_path = directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["generation"]["correction_source_sha256"] = "invalid"
            write_json_atomic(manifest_path, manifest)

            with self.assertRaisesRegex(CatalogPublishError, "lacks a publishable quality review"):
                validate_public_catalog(catalog)

    def test_rejects_manifest_asset_path_traversal_before_reading_it(self) -> None:
        with TemporaryDirectory() as temporary:
            catalog = Path(temporary) / "catalog"
            directory = _create_species(catalog, 1, "Example Bird")
            manifest_path = directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["assets"]["portrait"]["filename"] = "../../private.png"
            write_json_atomic(manifest_path, manifest)

            with self.assertRaisesRegex(CatalogPublishError, "must use portrait.png"):
                validate_public_catalog(catalog)

    def test_rejects_changes_to_an_existing_public_taxon(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            _create_species(source, 1, "Example Bird")
            sync_public_catalog(source, destination)
            public_portrait = destination / "species/1-example-bird/portrait.png"
            public_portrait.write_bytes(b"changed")

            with self.assertRaisesRegex(CatalogPublishError, "checksum does not match"):
                sync_public_catalog(source, destination)

    def test_publishes_through_a_real_git_remote_and_supports_dry_run(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote, checkout = _initialize_remote(root)
            config = load_config(_write_config(root, checkout))
            _create_species(config.controller.catalog_dir, 1, "Example Bird")
            (config.controller.catalog_dir / ".staging").mkdir()

            def fake_gh(
                _publication: object,
                *arguments: str,
                input_text: str | None = None,
            ) -> subprocess.CompletedProcess[str]:
                del input_text
                if arguments[:2] == ("pr", "list"):
                    stdout = "[]\n"
                elif arguments[:2] == ("pr", "create"):
                    stdout = "https://github.com/example/inky-bird-frame/pull/1\n"
                elif arguments[:2] == ("pr", "merge"):
                    refs = _run_git(
                        remote,
                        "for-each-ref",
                        "--format=%(refname)",
                        "refs/heads/catalog/publish-*",
                    ).splitlines()
                    self.assertEqual(len(refs), 1)
                    commit = _run_git(remote, "rev-parse", refs[0])
                    _run_git(remote, "update-ref", "refs/heads/main", commit)
                    _run_git(remote, "update-ref", "-d", refs[0])
                    stdout = ""
                elif arguments[:2] == ("pr", "view"):
                    stdout = "MERGED\n"
                else:
                    self.fail(f"Unexpected gh call: {arguments}")
                return subprocess.CompletedProcess(["gh", *arguments], 0, stdout, "")

            with (
                patch(
                    "inky_bird_frame.publisher._validate_checkout",
                    return_value="example/inky-bird-frame",
                ),
                patch("inky_bird_frame.publisher._gh", side_effect=fake_gh),
            ):
                first = run_catalog_publish(config)

            self.assertTrue(first["pushed"])
            self.assertTrue(first["merged"])
            self.assertTrue(first["changed"])
            self.assertIsInstance(first["commit"], str)
            self.assertIn(
                "catalog/species/1-example-bird/manifest.json",
                _run_git(remote, "ls-tree", "-r", "--name-only", "main"),
            )

            with patch(
                "inky_bird_frame.publisher._validate_checkout",
                return_value="example/inky-bird-frame",
            ):
                second = run_catalog_publish(config)

            self.assertFalse(second["pushed"])
            self.assertFalse(second["changed"])

            _create_species(config.controller.catalog_dir, 2, "Second Bird")
            with patch(
                "inky_bird_frame.publisher._validate_checkout",
                return_value="example/inky-bird-frame",
            ):
                dry_run = run_catalog_publish(config, dry_run=True)

            self.assertTrue(dry_run["changed"])
            self.assertFalse(dry_run["pushed"])
            self.assertNotIn(
                "2-second-bird", _run_git(remote, "ls-tree", "-r", "--name-only", "main")
            )

            with (
                patch(
                    "inky_bird_frame.publisher._validate_checkout",
                    return_value="example/inky-bird-frame",
                ),
                patch("inky_bird_frame.publisher._gh", side_effect=fake_gh),
            ):
                final = run_catalog_publish(config)

            self.assertTrue(final["pushed"])
            self.assertIn(
                "catalog/species/2-second-bird/manifest.json",
                _run_git(remote, "ls-tree", "-r", "--name-only", "main"),
            )


if __name__ == "__main__":
    unittest.main()
