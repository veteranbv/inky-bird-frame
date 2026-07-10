"""Command-line interface for controller, catalog, and display-node operations."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .birds import BirdSpecies, ObservationWindow, parse_observation_window
from .catalog import (
    approve_candidate,
    find_taxon_directory,
    read_json,
    rebuild_catalog_index,
    reject_candidate,
)
from .config import AppConfig, load_config
from .controller import (
    discover_species,
    enqueue_seed_species,
    read_generation_queue,
    run_controller_cycle,
    run_generation_cycle,
    run_refresh_cycle,
)
from .display import show_on_inky
from .display_node import run_display_cycle
from .errors import InkyBirdFrameError
from .images import prepare_uploaded_image
from .publisher import run_catalog_publish
from .server import serve_catalog


def print_result(data: object) -> None:
    print(json.dumps({"ok": True, "data": data, "schema_version": 1}, indent=2, sort_keys=True))


def print_error(exc: Exception) -> None:
    print(
        json.dumps(
            {
                "ok": False,
                "error": {"type": type(exc).__name__, "message": str(exc)},
                "schema_version": 1,
            },
            indent=2,
            sort_keys=True,
        )
    )


def species_to_dict(species: BirdSpecies) -> dict[str, object]:
    return {
        "taxon_id": species.taxon_id,
        "common_name": species.common_name,
        "scientific_name": species.scientific_name,
        "observation_count": species.observation_count,
        "source": species.source,
    }


def _config(args: argparse.Namespace) -> AppConfig:
    return load_config(args.config)


def discover_command(args: argparse.Namespace) -> int:
    config = _config(args)
    location, species = discover_species(config)
    print_result(
        {
            "location": {
                "zip_code": location.zip_code,
                "place_name": location.place_name,
                "state": location.state,
            },
            "radius_km": config.discovery.radius_km,
            "window": config.discovery.observation_window.value,
            "species": [species_to_dict(item) for item in species],
        }
    )
    return 0


def controller_cycle_command(args: argparse.Namespace) -> int:
    print_result(run_controller_cycle(_config(args)))
    return 0


def refresh_command(args: argparse.Namespace) -> int:
    print_result(run_refresh_cycle(_config(args)))
    return 0


def generate_command(args: argparse.Namespace) -> int:
    print_result(run_generation_cycle(_config(args)))
    return 0


def seed_command(args: argparse.Namespace) -> int:
    print_result(
        enqueue_seed_species(
            _config(args),
            window=parse_observation_window(args.window),
            radius_km=args.radius_km,
            species_limit=args.species_limit,
            dry_run=args.dry_run,
        )
    )
    return 0


def approve_command(args: argparse.Namespace) -> int:
    config = _config(args)
    entry = approve_candidate(
        config.controller.state_dir,
        config.controller.catalog_dir,
        args.taxon_id,
    )
    print_result(entry.as_dict())
    return 0


def reject_command(args: argparse.Namespace) -> int:
    config = _config(args)
    destination = reject_candidate(config.controller.state_dir, args.taxon_id, args.reason)
    print_result({"taxon_id": args.taxon_id, "status": "rejected", "path": str(destination)})
    return 0


def retry_command(args: argparse.Namespace) -> int:
    config = _config(args)
    if find_taxon_directory(config.controller.state_dir / "pending", args.taxon_id):
        raise ValueError("Pending candidates must be approved or rejected before retrying")
    sources = list((config.controller.state_dir / "failed").glob(f"{args.taxon_id}-*"))
    rejected = find_taxon_directory(config.controller.state_dir / "rejected", args.taxon_id)
    if rejected is not None:
        sources.append(rejected)
    if not sources:
        raise ValueError(f"No failed or rejected candidate exists for taxon {args.taxon_id}")
    archive = config.controller.state_dir / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for source in sources:
        destination = archive / source.name
        counter = 1
        while destination.exists():
            destination = archive / f"{source.name}-{counter}"
            counter += 1
        shutil.move(str(source), destination)
        moved.append(str(destination))
    print_result({"taxon_id": args.taxon_id, "status": "eligible", "archived": moved})
    return 0


def status_command(args: argparse.Namespace) -> int:
    config = _config(args)
    entries = rebuild_catalog_index(config.controller.catalog_dir)
    pending = []
    for path in sorted((config.controller.state_dir / "pending").glob("*/manifest.json")):
        manifest = read_json(path)
        if isinstance(manifest, dict):
            pending.append(
                {
                    "taxon_id": manifest.get("taxon_id"),
                    "common_name": manifest.get("common_name"),
                    "quality_review": manifest.get("quality_review"),
                    "path": str(path.parent),
                }
            )
    print_result(
        {
            "approved": [entry.as_dict() for entry in entries],
            "pending": pending,
            "queued": [species_to_dict(item) for item in read_generation_queue(config)],
            "failed": [
                str(path) for path in sorted((config.controller.state_dir / "failed").glob("*"))
            ],
        }
    )
    return 0


def serve_command(args: argparse.Namespace) -> int:
    serve_catalog(_config(args).controller)
    return 0


def display_cycle_command(args: argparse.Namespace) -> int:
    result = run_display_cycle(_config(args).display_node, force=args.force)
    print_result(result)
    return 0


def catalog_publish_command(args: argparse.Namespace) -> int:
    print_result(run_catalog_publish(_config(args), dry_run=args.dry_run))
    return 0


def prepare_image_command(args: argparse.Namespace) -> int:
    portrait, display = prepare_uploaded_image(args.image, args.output_dir)
    if args.display:
        show_on_inky(display)
    print_result(
        {
            "source": str(args.image),
            "portrait": str(portrait),
            "display": str(display),
            "display_update": "sent" if args.display else "not-requested",
        }
    )
    return 0


def display_image_command(args: argparse.Namespace) -> int:
    size = show_on_inky(args.image)
    print_result({"display_update": "sent", "image": str(args.image), "display_size": size})
    return 0


def add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to TOML configuration",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inky-bird-frame",
        description="Generate, approve, serve, and display bird field-journal plates.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover_parser = subparsers.add_parser("discover", help="List species in the configured area")
    add_config_argument(discover_parser)
    discover_parser.set_defaults(func=discover_command)

    cycle_parser = subparsers.add_parser(
        "controller-cycle",
        help="Discover species and stage missing generated plates",
    )
    add_config_argument(cycle_parser)
    cycle_parser.set_defaults(func=controller_cycle_command)

    refresh_parser = subparsers.add_parser(
        "refresh",
        help="Refresh local observations and the active display catalog",
    )
    add_config_argument(refresh_parser)
    refresh_parser.set_defaults(func=refresh_command)

    generate_parser = subparsers.add_parser(
        "generate",
        help="Generate missing plates from the latest observation refresh",
    )
    add_config_argument(generate_parser)
    generate_parser.set_defaults(func=generate_command)

    seed_parser = subparsers.add_parser(
        "seed",
        help="Queue distinct species from a broader observation window for generation",
    )
    add_config_argument(seed_parser)
    seed_parser.add_argument(
        "--window",
        required=True,
        choices=[window.value for window in ObservationWindow],
    )
    seed_parser.add_argument("--radius-km", type=int)
    seed_parser.add_argument("--species-limit", type=int)
    seed_parser.add_argument("--dry-run", action="store_true")
    seed_parser.set_defaults(func=seed_command)

    approve_parser = subparsers.add_parser("approve", help="Publish a pending candidate")
    add_config_argument(approve_parser)
    approve_parser.add_argument("taxon_id", type=int)
    approve_parser.set_defaults(func=approve_command)

    reject_parser = subparsers.add_parser("reject", help="Reject a pending candidate")
    add_config_argument(reject_parser)
    reject_parser.add_argument("taxon_id", type=int)
    reject_parser.add_argument("--reason", required=True)
    reject_parser.set_defaults(func=reject_command)

    retry_parser = subparsers.add_parser(
        "retry",
        help="Make a failed or rejected taxon eligible for explicit regeneration",
    )
    add_config_argument(retry_parser)
    retry_parser.add_argument("taxon_id", type=int)
    retry_parser.set_defaults(func=retry_command)

    status_parser = subparsers.add_parser("status", help="List approved and pending plates")
    add_config_argument(status_parser)
    status_parser.set_defaults(func=status_command)

    serve_parser = subparsers.add_parser("serve", help="Serve the approved catalog over HTTP")
    add_config_argument(serve_parser)
    serve_parser.set_defaults(func=serve_command)

    display_cycle_parser = subparsers.add_parser(
        "display-cycle",
        help="Pull and display the next approved plate",
    )
    add_config_argument(display_cycle_parser)
    display_cycle_parser.add_argument("--force", action="store_true")
    display_cycle_parser.set_defaults(func=display_cycle_command)

    catalog_publish_parser = subparsers.add_parser(
        "catalog-publish",
        help="Validate and owner-merge approved plates into this repository's catalog",
    )
    add_config_argument(catalog_publish_parser)
    catalog_publish_parser.add_argument("--dry-run", action="store_true")
    catalog_publish_parser.set_defaults(func=catalog_publish_command)

    prepare_parser = subparsers.add_parser(
        "prepare-image", help="Prepare a portrait image for Inky"
    )
    prepare_parser.add_argument("image", type=Path)
    prepare_parser.add_argument("--output-dir", type=Path, default=Path("output"))
    prepare_parser.add_argument("--display", action="store_true")
    prepare_parser.set_defaults(func=prepare_image_command)

    display_parser = subparsers.add_parser("display-image", help="Send a 1600x1200 image to Inky")
    display_parser.add_argument("image", type=Path)
    display_parser.set_defaults(func=display_image_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (InkyBirdFrameError, OSError, ValueError) as exc:
        print_error(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
