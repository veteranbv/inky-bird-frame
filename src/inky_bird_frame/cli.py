"""Command-line interface for controller, catalog, and display-node operations."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import threading
from contextlib import nullcontext
from pathlib import Path
from tempfile import NamedTemporaryFile

from .birds import BirdSpecies, ObservationWindow, parse_observation_window
from .catalog import (
    approve_candidate,
    catalog_state_lock,
    find_taxon_directory,
    read_json,
    rebuild_catalog_index,
    reject_candidate,
)
from .config import (
    AppConfig,
    DiscoveryProvider,
    NotificationEvent,
    discovery_source_label,
    load_config,
)
from .controller import (
    discover_species,
    enqueue_seed_species,
    exclusive_cycle_lock,
    read_generation_queue,
    run_controller_cycle,
    run_generation_cycle,
    run_refresh_cycle,
)
from .display import show_on_inky
from .display_node import run_display_cycle
from .errors import InkyBirdFrameError
from .images import prepare_uploaded_image
from .installation import InstallationRole, doctor, setup
from .notifications import (
    check_display_heartbeat,
    dispatch_notifications,
    notification_status,
    requeue_dead_letters,
    safe_notify,
    safe_record_degradation,
    safe_record_recovery,
    send_notification_test,
    validate_notification_destinations,
)
from .publisher import (
    run_catalog_publish,
    sync_public_catalog,
    validate_catalog_additions,
    validate_public_catalog,
)
from .retry import RetryStore
from .scheduler import ScheduledJob, SubprocessCommandRunner, run_scheduler
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
        "sources": list(species.sources),
    }


def _config(args: argparse.Namespace, *, load_secrets: bool = True) -> AppConfig:
    return load_config(args.config, load_secrets=load_secrets)


def _failure_notification(operation: str, exc: Exception) -> str:
    return f"{operation} failed ({type(exc).__name__}). Check controller logs for details."


def discover_command(args: argparse.Namespace) -> int:
    config = _config(args)
    discovery = discover_species(config, persist_taxonomy_cache=False)
    location = discovery.location
    print_result(
        {
            "location": (
                {
                    "zip_code": (
                        location.postal_code if location.geocoder == "zippopotam" else None
                    ),
                    "postal_code": location.postal_code,
                    "country_code": location.country_code,
                    "place_name": location.place_name,
                    "state": location.state,
                    "geocoder": location.geocoder,
                    "geocoder_attribution": location.geocoder_attribution,
                }
                if location is not None
                else None
            ),
            "radius_km": config.discovery.radius_km,
            "window": config.discovery.observation_window.value,
            "source": discovery_source_label(config.discovery.sources),
            "sources": [provider.value for provider in config.discovery.sources],
            "providers": [provider.as_dict() for provider in discovery.providers],
            "unresolved_count": len(discovery.unresolved),
            "species": [species_to_dict(item) for item in discovery.species],
        }
    )
    return 0


def controller_cycle_command(args: argparse.Namespace) -> int:
    print_result(run_controller_cycle(_config(args)))
    return 0


def refresh_command(args: argparse.Namespace) -> int:
    config = _config(args)
    try:
        result = run_refresh_cycle(config)
    except (InkyBirdFrameError, OSError) as exc:
        safe_record_degradation(
            config,
            key="observation-refresh",
            title="Bird discovery is degraded",
            body=_failure_notification("Observation refresh", exc),
        )
        raise
    safe_record_recovery(
        config,
        key="observation-refresh",
        title="Bird discovery recovered",
        body="Observation refresh is succeeding again.",
    )
    providers = result.get("providers")
    successful_providers: set[str] = set()
    if isinstance(providers, list):
        for provider in providers:
            if not isinstance(provider, dict) or not isinstance(provider.get("name"), str):
                continue
            name = provider["name"]
            if provider.get("status") == "error":
                safe_record_degradation(
                    config,
                    key=f"observation-provider-{name}",
                    title=f"{name} bird discovery is degraded",
                    body=f"The {name} provider failed; another configured provider supplied data.",
                )
            else:
                successful_providers.add(name)
                safe_record_recovery(
                    config,
                    key=f"observation-provider-{name}",
                    title=f"{name} bird discovery recovered",
                    body=f"The {name} provider is succeeding again.",
                )
    unresolved = result.get("unresolved_species")
    unresolved_by_provider: dict[str, int] = {}
    if isinstance(unresolved, list):
        for item in unresolved:
            if not isinstance(item, dict) or not isinstance(item.get("provider"), str):
                continue
            provider = item["provider"]
            unresolved_by_provider[provider] = unresolved_by_provider.get(provider, 0) + 1
    taxonomy_providers = {"ebird": "eBird", "birdweather": "BirdWeather"}
    for provider, display_name in taxonomy_providers.items():
        unresolved_count = unresolved_by_provider.get(provider, 0)
        if unresolved_count:
            safe_record_degradation(
                config,
                key=f"{provider}-taxonomy",
                title=f"Some {display_name} species are awaiting taxonomy matching",
                body=(f"{unresolved_count} species were deferred without blocking bird discovery."),
            )
        elif provider in successful_providers:
            safe_record_recovery(
                config,
                key=f"{provider}-taxonomy",
                title=f"{display_name} taxonomy matching recovered",
                body=f"Deferred {display_name} taxonomy matches have cleared.",
            )
    new_species = result.get("new_species")
    if isinstance(new_species, list) and new_species:
        names: list[str] = []
        taxon_ids: list[str] = []
        for item in new_species:
            if not isinstance(item, dict):
                continue
            common_name = item.get("common_name")
            taxon_id = item.get("taxon_id")
            if isinstance(common_name, str):
                names.append(common_name)
            if isinstance(taxon_id, int):
                taxon_ids.append(str(taxon_id))
        safe_notify(
            config,
            NotificationEvent.DISCOVERY,
            dedupe_key=":".join(taxon_ids),
            title=f"{len(names)} new bird species discovered",
            body=", ".join(names),
        )
    print_result(result)
    return 0


def generate_command(args: argparse.Namespace) -> int:
    config = _config(args)
    try:
        result = run_generation_cycle(config)
    except (InkyBirdFrameError, OSError) as exc:
        safe_record_degradation(
            config,
            key="generation-cycle",
            title="Bird generation is degraded",
            body=_failure_notification("Generation cycle", exc),
        )
        raise
    notified_taxa: set[int] = set()
    for result_key in ("published_pending", "generated"):
        approved = result.get(result_key)
        if not isinstance(approved, list):
            continue
        for item in approved:
            if not isinstance(item, dict):
                continue
            taxon_id = item.get("taxon_id")
            common_name = item.get("common_name")
            if (
                isinstance(taxon_id, int)
                and taxon_id not in notified_taxa
                and isinstance(common_name, str)
            ):
                notified_taxa.add(taxon_id)
                safe_notify(
                    config,
                    NotificationEvent.GENERATION_APPROVED,
                    dedupe_key=str(taxon_id),
                    title=f"{common_name} plate approved",
                    body="The generated plate passed factual and visual review.",
                )
    failures = result.get("failures")
    transient_failures = []
    if isinstance(failures, list):
        for item in failures:
            if not isinstance(item, dict):
                continue
            if item.get("terminal") is True:
                safe_notify(
                    config,
                    NotificationEvent.TERMINAL_ERROR,
                    dedupe_key=f"generation:{item.get('taxon_id')}:{item.get('failure')}",
                    title=f"Generation stopped for {item.get('common_name', 'a bird')}",
                    body="Generation reached a terminal error. Check controller logs for details.",
                )
            else:
                transient_failures.append(item)
    if transient_failures:
        safe_record_degradation(
            config,
            key="generation-items",
            title="Some bird generations are retrying",
            body=(
                f"{len(transient_failures)} species failed and were deferred without "
                "blocking the queue."
            ),
        )
    elif result.get("outstanding_retry_count") == 0:
        safe_record_recovery(
            config,
            key="generation-items",
            title="Bird generation recovered",
            body="Deferred generation errors have cleared.",
        )
    safe_record_recovery(
        config,
        key="generation-cycle",
        title="Bird generation recovered",
        body="Generation cycles are succeeding again.",
    )
    print_result(result)
    return 0


def seed_command(args: argparse.Namespace) -> int:
    source_values = args.source
    if source_values is not None and len(source_values) != len(set(source_values)):
        raise ValueError("--source must not repeat a provider")
    print_result(
        enqueue_seed_species(
            _config(args),
            window=parse_observation_window(args.window),
            sources=(
                tuple(DiscoveryProvider(value) for value in source_values)
                if source_values is not None
                else None
            ),
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
    with exclusive_cycle_lock(config.controller.state_dir):
        if find_taxon_directory(config.controller.state_dir / "pending", args.taxon_id):
            raise ValueError("Pending candidates must be approved or rejected before retrying")
        sources = list((config.controller.state_dir / "failed").glob(f"{args.taxon_id}-*"))
        rejected = find_taxon_directory(config.controller.state_dir / "rejected", args.taxon_id)
        if rejected is not None:
            sources.append(rejected)
        retry_store = RetryStore(config.controller.state_dir / "generation-retries.json")
        deferred = retry_store.get(args.taxon_id) is not None
        if not sources and not deferred:
            raise ValueError(
                f"No failed, rejected, or deferred candidate exists for taxon {args.taxon_id}"
            )
        profile_cache = config.controller.state_dir / "profiles" / str(args.taxon_id)
        cleared_cached_profile = profile_cache.exists()
        if cleared_cached_profile:
            sources.append(profile_cache)
        reference_cache = config.controller.state_dir / "references" / str(args.taxon_id)
        cleared_cached_references = reference_cache.exists()
        if cleared_cached_references:
            sources.append(reference_cache)
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
        retry_store.clear(args.taxon_id)
    print_result(
        {
            "taxon_id": args.taxon_id,
            "status": "eligible",
            "archived": moved,
            "cleared_deferred_retry": deferred,
            "cleared_cached_profile": cleared_cached_profile,
            "cleared_cached_references": cleared_cached_references,
        }
    )
    return 0


def status_command(args: argparse.Namespace) -> int:
    config = _config(args)
    entries = rebuild_catalog_index(config.controller.catalog_dir)
    queued = read_generation_queue(config)
    retries = RetryStore(config.controller.state_dir / "generation-retries.json")
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
            "queued": [species_to_dict(item) for item in queued],
            "deferred": [record.as_dict() for record in retries.records()],
            "failed": [
                str(path) for path in sorted((config.controller.state_dir / "failed").glob("*"))
            ],
        }
    )
    return 0


def serve_command(args: argparse.Namespace) -> int:
    serve_catalog(_config(args, load_secrets=False).controller)
    return 0


def display_cycle_command(args: argparse.Namespace) -> int:
    result = run_display_cycle(_config(args).display_node, force=args.force)
    print_result(result)
    return 0


def catalog_publish_command(args: argparse.Namespace) -> int:
    config = _config(args)
    try:
        result = run_catalog_publish(config, dry_run=args.dry_run)
    except (InkyBirdFrameError, OSError) as exc:
        if not args.dry_run:
            safe_record_degradation(
                config,
                key="catalog-publication",
                title="Catalog publication is degraded",
                body=_failure_notification("Catalog publication", exc),
                event=NotificationEvent.PUBLICATION_ERROR,
            )
        raise
    if not args.dry_run:
        safe_record_recovery(
            config,
            key="catalog-publication",
            title="Catalog publication recovered",
            body="Catalog publication is succeeding again.",
            event=NotificationEvent.PUBLICATION_RECOVERED,
        )
    print_result(result)
    return 0


def catalog_prepare_command(args: argparse.Namespace) -> int:
    result = sync_public_catalog(
        args.source_catalog,
        args.catalog,
        taxon_ids={args.taxon_id},
    )
    print_result(
        {
            **result,
            "catalog": str(args.catalog),
            "source_catalog": str(args.source_catalog),
            "taxon_id": args.taxon_id,
        }
    )
    return 0


def catalog_sync_command(args: argparse.Namespace) -> int:
    lock = catalog_state_lock(args.state_dir) if args.state_dir is not None else nullcontext()
    with lock:
        result = sync_public_catalog(args.source_catalog, args.catalog)
    print_result(
        {
            **result,
            "catalog": str(args.catalog),
            "source_catalog": str(args.source_catalog),
        }
    )
    return 0


def catalog_validate_command(args: argparse.Namespace) -> int:
    entries = validate_public_catalog(args.catalog)
    result: dict[str, object] = {
        "catalog": str(args.catalog),
        "valid": True,
        "species_count": len(entries),
    }
    if args.base_catalog is not None:
        additions = validate_catalog_additions(args.base_catalog, args.catalog)
        result["base_catalog"] = str(args.base_catalog)
        result["additions"] = [entry.as_dict() for entry in additions]
    print_result(result)
    return 0


def config_validate_command(args: argparse.Namespace) -> int:
    config = _config(args)
    destinations = validate_notification_destinations(config)
    if config.discovery.latitude is not None:
        location_mode = "coordinates"
    elif config.discovery.postal_code is not None:
        location_mode = "geoapify"
    elif config.discovery.zip_code is not None:
        location_mode = "zippopotam"
    else:
        location_mode = None
    print_result(
        {
            "config": str(args.config),
            "valid": True,
            "discovery": {
                "source": discovery_source_label(config.discovery.sources),
                "sources": [provider.value for provider in config.discovery.sources],
                "location_mode": location_mode,
                "geoapify_configured": config.discovery.geoapify_api_key is not None,
                "ebird_configured": config.discovery.ebird_api_key is not None,
                "birdweather_configured": config.discovery.birdweather_token is not None,
                "window": config.discovery.observation_window.value,
                "radius_km": config.discovery.radius_km,
            },
            "notifications": {
                "enabled": config.notifications.enabled,
                "destinations": destinations,
            },
        }
    )
    return 0


def config_install_command(args: argparse.Namespace) -> int:
    destination = args.destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            os.chmod(temporary_path, 0o600)
            handle.write(sys.stdin.read())
            handle.flush()
            os.fsync(handle.fileno())
        load_config(temporary_path)
        os.replace(temporary_path, destination)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    print_result({"config": str(destination), "installed": True, "valid": True})
    return 0


def notifications_status_command(args: argparse.Namespace) -> int:
    print_result(notification_status(_config(args)))
    return 0


def notifications_test_command(args: argparse.Namespace) -> int:
    config = _config(args)
    result = send_notification_test(config)
    if result["failures"]:
        raise ValueError("Notification test was not delivered to every configured destination")
    print_result(result)
    return 0


def notifications_dispatch_command(args: argparse.Namespace) -> int:
    config = _config(args)
    display_heartbeat = check_display_heartbeat(config)
    print_result({**dispatch_notifications(config), "display_heartbeat": display_heartbeat})
    return 0


def notifications_retry_command(args: argparse.Namespace) -> int:
    config = _config(args)
    requeued = requeue_dead_letters(config)
    print_result({"requeued": requeued, "delivery": dispatch_notifications(config)})
    return 0


def scheduler_command(args: argparse.Namespace) -> int:
    config = _config(args)
    config_arguments = ("--config", str(args.config))
    jobs = [
        ScheduledJob(
            "refresh",
            ("refresh", *config_arguments),
            config.schedule.refresh_minutes * 60,
        ),
        ScheduledJob(
            "generate",
            ("generate", *config_arguments),
            config.schedule.generation_minutes * 60,
            requires_refresh=True,
        ),
    ]
    if config.public_catalog.enabled:
        jobs.append(
            ScheduledJob(
                "catalog-publish",
                ("catalog-publish", *config_arguments),
                config.schedule.catalog_publish_minutes * 60,
            )
        )
    if config.notifications.enabled:
        jobs.append(
            ScheduledJob(
                "notifications",
                ("notifications", "dispatch", *config_arguments),
                config.notifications.delivery_retry_minutes * 60,
            )
        )

    stop = threading.Event()

    command_runner = SubprocessCommandRunner((sys.executable, "-m", "inky_bird_frame.cli"))

    def request_stop(signum: int, _frame: object) -> None:
        stop.set()
        command_runner.terminate(signum)

    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    previous_sigint = signal.signal(signal.SIGINT, request_stop)

    def wait(seconds: float) -> None:
        stop.wait(seconds)

    try:
        run_scheduler(
            jobs,
            command_runner,
            stop_requested=stop.is_set,
            wait=wait,
        )
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
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


def setup_command(args: argparse.Namespace) -> int:
    print_result(
        setup(
            InstallationRole(args.role),
            args.config,
            apply=args.yes,
            source_dir=args.source_dir,
            app_dir=args.app_dir,
            support_dir=args.support_dir,
            uv_bin=args.uv_bin,
            python_version=args.python_version,
            venv=args.venv,
        )
    )
    return 0


def doctor_command(args: argparse.Namespace) -> int:
    report = doctor(InstallationRole(args.role), args.config)
    print_result(report.as_dict())
    return 0 if report.ready else 1


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

    setup_parser = subparsers.add_parser(
        "setup", help="Preview or install a controller or display-node service"
    )
    setup_subparsers = setup_parser.add_subparsers(dest="role", required=True)
    for role in InstallationRole:
        role_parser = setup_subparsers.add_parser(role.value, help=f"Set up the {role.value} role")
        add_config_argument(role_parser)
        role_parser.add_argument("--yes", action="store_true", help="Apply the described changes")
        role_parser.add_argument(
            "--source-dir",
            type=Path,
            help="Source checkout containing the deployment scripts",
        )
        role_parser.add_argument("--app-dir", type=Path, help="Managed application directory")
        if role is InstallationRole.CONTROLLER:
            role_parser.add_argument("--support-dir", type=Path, help="Managed support directory")
            role_parser.add_argument("--uv-bin", type=Path, help="Path to the uv executable")
            role_parser.add_argument("--python-version", help="Python version for the controller")
            role_parser.set_defaults(venv=None)
        else:
            role_parser.add_argument(
                "--venv", type=Path, help="Pimoroni Python environment for the display node"
            )
            role_parser.set_defaults(support_dir=None, uv_bin=None, python_version=None)
        role_parser.set_defaults(func=setup_command)

    doctor_parser = subparsers.add_parser(
        "doctor", help="Run read-only controller or display-node diagnostics"
    )
    doctor_subparsers = doctor_parser.add_subparsers(dest="role", required=True)
    for role in InstallationRole:
        role_parser = doctor_subparsers.add_parser(
            role.value, help=f"Diagnose the {role.value} role"
        )
        add_config_argument(role_parser)
        role_parser.set_defaults(func=doctor_command)

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
    seed_parser.add_argument(
        "--source",
        action="append",
        choices=[provider.value for provider in DiscoveryProvider],
        help="Override discovery with one provider; repeat to select multiple providers",
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

    scheduler_parser = subparsers.add_parser(
        "scheduler", help="Run controller maintenance jobs on their configured schedules"
    )
    add_config_argument(scheduler_parser)
    scheduler_parser.set_defaults(func=scheduler_command)

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

    catalog_parser = subparsers.add_parser(
        "catalog", help="Prepare and validate public catalog contributions"
    )
    catalog_subparsers = catalog_parser.add_subparsers(dest="catalog_command", required=True)
    catalog_prepare_parser = catalog_subparsers.add_parser(
        "prepare", help="Copy one approved taxon into a repository catalog"
    )
    catalog_prepare_parser.add_argument("taxon_id", type=int)
    catalog_prepare_parser.add_argument(
        "--source-catalog",
        type=Path,
        required=True,
        help="Approved local catalog containing the taxon",
    )
    catalog_prepare_parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("catalog"),
        help="Repository catalog to update (default: catalog)",
    )
    catalog_prepare_parser.set_defaults(func=catalog_prepare_command)
    catalog_sync_parser = catalog_subparsers.add_parser(
        "sync", help="Add immutable species from one catalog to another"
    )
    catalog_sync_parser.add_argument("--source-catalog", type=Path, required=True)
    catalog_sync_parser.add_argument("--catalog", type=Path, required=True)
    catalog_sync_parser.add_argument(
        "--state-dir",
        type=Path,
        help="Optional controller state directory used to lock catalog writes",
    )
    catalog_sync_parser.set_defaults(func=catalog_sync_command)
    catalog_validate_parser = catalog_subparsers.add_parser(
        "validate", help="Validate catalog files, privacy, and immutability"
    )
    catalog_validate_parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("catalog"),
        help="Catalog to validate (default: catalog)",
    )
    catalog_validate_parser.add_argument(
        "--base-catalog",
        type=Path,
        help="Optional base catalog used to enforce add-only changes",
    )
    catalog_validate_parser.set_defaults(func=catalog_validate_command)

    config_parser = subparsers.add_parser("config", help="Validate application configuration")
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)
    config_validate_parser = config_subparsers.add_parser("validate", help="Validate TOML settings")
    add_config_argument(config_validate_parser)
    config_validate_parser.set_defaults(func=config_validate_command)
    config_install_parser = config_subparsers.add_parser(
        "install", help="Validate TOML from standard input and install it atomically"
    )
    config_install_parser.add_argument("--destination", type=Path, required=True)
    config_install_parser.set_defaults(func=config_install_command)

    notifications_parser = subparsers.add_parser(
        "notifications", help="Inspect and test notification delivery"
    )
    notifications_subparsers = notifications_parser.add_subparsers(
        dest="notifications_command", required=True
    )
    notifications_status_parser = notifications_subparsers.add_parser(
        "status", help="Show redacted notification state"
    )
    add_config_argument(notifications_status_parser)
    notifications_status_parser.set_defaults(func=notifications_status_command)
    notifications_test_parser = notifications_subparsers.add_parser(
        "test", help="Send a notification through configured destinations"
    )
    add_config_argument(notifications_test_parser)
    notifications_test_parser.set_defaults(func=notifications_test_command)
    notifications_dispatch_parser = notifications_subparsers.add_parser(
        "dispatch", help="Deliver due messages from the durable outbox"
    )
    add_config_argument(notifications_dispatch_parser)
    notifications_dispatch_parser.set_defaults(func=notifications_dispatch_command)
    notifications_retry_parser = notifications_subparsers.add_parser(
        "retry", help="Requeue dead-letter notifications and attempt delivery"
    )
    add_config_argument(notifications_retry_parser)
    notifications_retry_parser.set_defaults(func=notifications_retry_command)

    prepare_parser = subparsers.add_parser(
        "prepare-image", help="Prepare a portrait image for Inky"
    )
    prepare_parser.add_argument("image", type=Path)
    prepare_parser.add_argument("--output-dir", type=Path, default=Path("output"))
    prepare_parser.add_argument("--display", action="store_true")
    prepare_parser.set_defaults(func=prepare_image_command)

    display_parser = subparsers.add_parser(
        "display-image", help="Send a canonical 1600x1200 image to a supported Inky"
    )
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
