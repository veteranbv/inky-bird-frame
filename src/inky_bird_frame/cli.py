"""Command-line interface for controller, catalog, and display-node operations."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import sys
import threading
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile

from . import __version__
from .birdbuddy import (
    AUTHORIZED_ACCESS_ATTESTATION,
    birdbuddy_status,
    login_birdbuddy,
    logout_birdbuddy,
)
from .birdnet_analyzer import import_birdnet_analyzer_csv
from .birds import BirdSpecies, DateRange, ObservationWindow, parse_observation_window
from .catalog import (
    approve_candidate,
    catalog_state_lock,
    read_json,
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
    add_collection_member,
    collection_status,
    discover_species,
    enqueue_seed_species,
    import_approved_collection,
    read_generation_queue_partition,
    read_generation_work,
    refresh_observations,
    remove_collection_member,
    run_controller_cycle,
    run_generation_cycle,
)
from .display import show_on_inky
from .display_node import run_display_cycle
from .ebird_archive import import_ebird_archive, read_ebird_archive_history
from .errors import DataSourceError, InkyBirdFrameError
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
    replace_public_catalog_taxon,
    run_catalog_publish,
    sync_public_catalog,
    validate_catalog_additions,
    validate_public_catalog,
)
from .recovery import retry_species
from .retry import RetryStore
from .scheduler import ScheduledJob, SubprocessCommandRunner, run_scheduler
from .server import serve_catalog
from .settings import initialize_config

CATALOG_SYNC_REVIEWED_MIGRATIONS_ENV = "INKY_CATALOG_SYNC_APPLY_REVIEWED_MIGRATIONS"
CATALOG_SYNC_RETAIN_INDEPENDENT_ENV = "INKY_CATALOG_SYNC_RETAIN_INDEPENDENT_APPROVALS"


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
    payload: dict[str, object] = {
        "taxon_id": species.taxon_id,
        "common_name": species.common_name,
        "scientific_name": species.scientific_name,
        "observation_count": species.observation_count,
        "source": species.source,
        "sources": list(species.sources),
    }
    if species.latest_detection_at is not None:
        payload["latest_detection_at"] = species.latest_detection_at
    return payload


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
    print_result(refresh_observations(_config(args)))
    return 0


def generate_command(args: argparse.Namespace) -> int:
    config = _config(args)
    if not config.controller.generation_enabled:
        print_result(run_generation_cycle(config))
        return 0
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
    raw_manual_retry_count = result.get("manual_retry_required_count", 0)
    manual_retry_count = (
        raw_manual_retry_count
        if isinstance(raw_manual_retry_count, int) and not isinstance(raw_manual_retry_count, bool)
        else 0
    )
    if transient_failures or manual_retry_count:
        descriptions = []
        if transient_failures:
            descriptions.append(
                f"{len(transient_failures)} species failed and were deferred without "
                "blocking the queue"
            )
        if manual_retry_count:
            descriptions.append(
                f"{manual_retry_count} retained species are no longer in the current "
                "observation or durable queue and require an explicit retry"
            )
        safe_record_degradation(
            config,
            key="generation-items",
            title=(
                "Some bird generations need attention"
                if transient_failures and manual_retry_count
                else "Some bird generations are retrying"
                if transient_failures
                else "Some bird generations need manual retry"
            ),
            body=(
                "; ".join(descriptions)
                + "."
                + (
                    " Run status, then retry each retained taxon you still want processed."
                    if manual_retry_count
                    else ""
                )
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
    if (args.latitude is None) != (args.longitude is None):
        raise ValueError("--latitude and --longitude must be provided together")
    if args.window is not None and args.end_date is not None:
        raise ValueError("--end-date cannot be combined with --window")
    date_range = None
    if args.start_date is not None:
        if args.end_date is None:
            raise ValueError("--start-date and --end-date must be provided together")
        try:
            date_range = DateRange(
                start=date.fromisoformat(args.start_date),
                end=date.fromisoformat(args.end_date),
            )
        except ValueError as exc:
            raise ValueError(f"invalid ISO date range: {exc}") from exc
    print_result(
        enqueue_seed_species(
            _config(args),
            window=parse_observation_window(args.window) if args.window is not None else None,
            date_range=date_range,
            latitude=args.latitude,
            longitude=args.longitude,
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


def collection_list_command(args: argparse.Namespace) -> int:
    print_result(collection_status(_config(args)))
    return 0


def collection_import_command(args: argparse.Namespace) -> int:
    print_result(import_approved_collection(_config(args), dry_run=args.dry_run))
    return 0


def collection_add_command(args: argparse.Namespace) -> int:
    print_result(add_collection_member(_config(args), args.taxon_id, dry_run=args.dry_run))
    return 0


def collection_remove_command(args: argparse.Namespace) -> int:
    print_result(remove_collection_member(_config(args), args.taxon_id, dry_run=args.dry_run))
    return 0


def approve_command(args: argparse.Namespace) -> int:
    config = _config(args)
    with catalog_state_lock(config.controller.state_dir):
        entry = approve_candidate(
            config.controller.state_dir,
            config.controller.catalog_dir,
            args.taxon_id,
        )
    print_result(entry.as_dict())
    return 0


def reject_command(args: argparse.Namespace) -> int:
    config = _config(args)
    with catalog_state_lock(config.controller.state_dir):
        destination = reject_candidate(config.controller.state_dir, args.taxon_id, args.reason)
    print_result({"taxon_id": args.taxon_id, "status": "rejected", "path": str(destination)})
    return 0


def retry_command(args: argparse.Namespace) -> int:
    print_result(
        retry_species(
            _config(args),
            args.taxon_id,
            replace_approved=bool(getattr(args, "replace_approved", False)),
            refresh_research=bool(getattr(args, "refresh_research", False)),
            reason=getattr(args, "reason", None),
            source_attempt=getattr(args, "source_attempt", None),
            source_candidate=getattr(args, "source_candidate", None),
            source_run=getattr(args, "source_run", None),
            corrections=getattr(args, "correction", None),
        )
    )
    return 0


def status_command(args: argparse.Namespace) -> int:
    config = _config(args)
    with catalog_state_lock(config.controller.state_dir):
        entries = validate_public_catalog(config.controller.catalog_dir)
        approved = {entry.taxon_id for entry in entries}
        queue = read_generation_queue_partition(config, approved=approved)
        retries = RetryStore(config.controller.state_dir / "generation-retries.json").records()
        generation = read_generation_work(config, approved=approved, retry_records=retries)
        collection = collection_status(config, approved=entries)
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
        failed = [str(path) for path in sorted((config.controller.state_dir / "failed").glob("*"))]
    print_result(
        {
            "approved": [entry.as_dict() for entry in entries],
            "collection": {key: value for key, value in collection.items() if key != "members"},
            "pending": pending,
            "queued": [species_to_dict(item) for item in queue.actionable],
            "terminal_blocked": [entry.as_dict() for entry in queue.terminal_blocked],
            "deferred": [record.as_dict() for record in retries],
            "failed": failed,
            "generation": generation.as_dict(),
        }
    )
    return 0


def access_bootstrap_command(args: argparse.Namespace) -> int:
    from .access import BOOTSTRAP_SECONDS, AccessStore

    if args.show_code and not (sys.stdout.isatty() and sys.stderr.isatty()):
        raise ValueError("--show-code requires a private interactive terminal, not a pipe or log")
    store = AccessStore(_config(args, load_secrets=False).controller.state_dir)
    path = store.bootstrap()
    if args.show_code:
        print("Copy this setup code into Inky's setup page:", file=sys.stderr)
        print(path.read_text().strip(), file=sys.stderr)
        print(
            f"It expires in {BOOTSTRAP_SECONDS // 60} minutes. "
            "Use it once, then sign in with the password you choose.",
            file=sys.stderr,
        )
    print_result(
        {
            "setup_secret_file": str(path),
            "expires_in_seconds": BOOTSTRAP_SECONDS,
            "owner_access_reset": False,
            "credentials_in_output": args.show_code,
        }
    )
    return 0


def access_status_command(args: argparse.Namespace) -> int:
    from .access import AccessStore

    store = AccessStore(_config(args, load_secrets=False).controller.state_dir)
    print_result({"owner_configured": store.owner_exists()})
    return 0


def access_recover_command(args: argparse.Namespace) -> int:
    from .access import AccessStore, normalize_username

    if not sys.stdin.isatty():
        raise ValueError("access recover requires a private interactive terminal")
    if (
        not args.yes
        and input(
            "Recovery signs out every device and clears the stored single-sign-on link. "
            "Bird data stays unchanged. Type yes to continue: "
        )
        .strip()
        .casefold()
        != "yes"
    ):
        raise ValueError("Recovery cancelled; owner access is unchanged")
    username = normalize_username(input("Inky username: "))
    password = getpass.getpass("New Inky password: ")
    if password != getpass.getpass("Confirm new password: "):
        raise ValueError("Passwords do not match; owner access is unchanged")
    store = AccessStore(_config(args, load_secrets=False).controller.state_dir)
    store.recover_owner(username, password)
    print_result({"owner_configured": True, "sessions_revoked": True})
    return 0


def serve_command(args: argparse.Namespace) -> int:
    config = _config(args, load_secrets=False)
    serve_catalog(config.controller, app_config=config)
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
    replace_approved = bool(getattr(args, "replace_approved", False))
    raw_reason = getattr(args, "reason", None)
    reason = raw_reason.strip() if isinstance(raw_reason, str) else None
    if replace_approved:
        if not reason:
            raise ValueError("--replace-approved requires a non-empty --reason")
        result = replace_public_catalog_taxon(
            args.source_catalog,
            args.catalog,
            args.taxon_id,
            reason,
        )
    else:
        if raw_reason is not None:
            raise ValueError("--reason requires --replace-approved")
        result = sync_public_catalog(
            args.source_catalog,
            args.catalog,
            taxon_ids={args.taxon_id},
            allow_replacements=False,
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
    apply_reviewed_migrations = args.apply_reviewed_migrations or (
        os.environ.get(CATALOG_SYNC_REVIEWED_MIGRATIONS_ENV) == "1"
    )
    retain_independent_approvals = bool(getattr(args, "retain_independent_approvals", False)) or (
        os.environ.get(CATALOG_SYNC_RETAIN_INDEPENDENT_ENV) == "1"
    )
    with lock:
        result = sync_public_catalog(
            args.source_catalog,
            args.catalog,
            allow_replacements=apply_reviewed_migrations,
            retain_independent_approvals=retain_independent_approvals,
        )
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
    deprecations: list[dict[str, str]] = []
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
            "deprecations": deprecations,
            "discovery": {
                "source": discovery_source_label(config.discovery.sources),
                "sources": [provider.value for provider in config.discovery.sources],
                "location_mode": location_mode,
                "geoapify_configured": config.discovery.geoapify_api_key is not None,
                "ebird_configured": config.discovery.ebird_api_key is not None,
                "birdweather_configured": config.discovery.birdweather_token is not None,
                "birdbuddy_selected": DiscoveryProvider.BIRDBUDDY in config.discovery.sources,
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
    if destination.exists():
        existing = load_config(destination, load_secrets=False)
        if existing.controller.configuration_ownership == "app":
            print_result({"installed": False, "preserved": True, "ownership": "app"})
            return 0
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


def config_init_command(args: argparse.Namespace) -> int:
    initialize_config(args.destination, container=args.container)
    print_result(
        {
            "created": True,
            "ownership": "app",
            "generation_enabled": False,
            "sources": [],
            "config": str(args.destination),
        }
    )
    return 0


def _confirm_birdbuddy_authorization(explicit_confirmation: bool) -> bool:
    if explicit_confirmation:
        return True
    if not sys.stdin.isatty():
        raise DataSourceError(
            "Bird Buddy authorized-access confirmation is required; "
            "use --confirm-authorized-access after obtaining permission"
        )
    print(AUTHORIZED_ACCESS_ATTESTATION, file=sys.stderr)
    print("Type 'yes' to confirm: ", end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip().casefold() == "yes"


def _birdbuddy_login_credentials() -> tuple[str, str]:
    email = os.environ.get("INKY_BIRDBUDDY_EMAIL", "").strip()
    password = os.environ.get("INKY_BIRDBUDDY_PASSWORD", "")
    if not email:
        if not sys.stdin.isatty():
            raise DataSourceError("INKY_BIRDBUDDY_EMAIL is required for noninteractive login")
        print("Bird Buddy email: ", end="", file=sys.stderr, flush=True)
        email = sys.stdin.readline().strip()
    if not password:
        if not sys.stdin.isatty():
            raise DataSourceError("INKY_BIRDBUDDY_PASSWORD is required for noninteractive login")
        password = getpass.getpass("Bird Buddy password: ")
    return email, password


def birdbuddy_login_command(args: argparse.Namespace) -> int:
    config = _config(args, load_secrets=False)
    confirmed = _confirm_birdbuddy_authorization(args.confirm_authorized_access)
    if not confirmed:
        raise DataSourceError("Bird Buddy authorized-access confirmation was declined")
    email, password = _birdbuddy_login_credentials()
    print_result(
        login_birdbuddy(
            config.controller.state_dir,
            email=email,
            password=password,
            authorization_confirmed=True,
            feeder_id=args.feeder_id,
        )
    )
    return 0


def birdbuddy_status_command(args: argparse.Namespace) -> int:
    config = _config(args, load_secrets=False)
    print_result(
        birdbuddy_status(
            config.controller.state_dir,
            include_manual_sightings=(config.discovery.birdbuddy_include_manual_sightings),
        )
    )
    return 0


def birdbuddy_logout_command(args: argparse.Namespace) -> int:
    if not args.yes:
        raise DataSourceError("birdbuddy logout requires --yes")
    config = _config(args, load_secrets=False)
    print_result(logout_birdbuddy(config.controller.state_dir))
    return 0


def birdnet_analyzer_import_command(args: argparse.Namespace) -> int:
    observed_on = None
    if args.observed_on is not None:
        try:
            observed_on = date.fromisoformat(args.observed_on)
        except ValueError as exc:
            raise ValueError(f"invalid --observed-on ISO date: {exc}") from exc
        if observed_on.isoformat() != args.observed_on:
            raise ValueError("invalid --observed-on ISO date: expected YYYY-MM-DD")
    config = _config(args, load_secrets=False)
    result = import_birdnet_analyzer_csv(
        args.csv,
        config.controller.state_dir,
        observed_on=observed_on,
        dry_run=args.dry_run,
    )
    print_result(result.as_dict())
    return 0


def ebird_archive_import_command(args: argparse.Namespace) -> int:
    config = _config(args, load_secrets=False)
    result = import_ebird_archive(
        args.archive,
        config.controller.state_dir,
        allow_history_reduction=args.allow_history_reduction,
        dry_run=args.dry_run,
    )
    print_result(result.as_dict())
    return 0


def ebird_archive_status_command(args: argparse.Namespace) -> int:
    config = _config(args, load_secrets=False)
    history = read_ebird_archive_history(
        config.controller.state_dir,
        window=ObservationWindow.ALL_TIME,
        limit=1,
    )
    print_result({"imported": True, **history.details()})
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


def controller_jobs(config: AppConfig, config_path: Path) -> list[ScheduledJob]:
    config_arguments = ("--config", str(config_path))
    jobs = [
        ScheduledJob(
            "refresh",
            ("refresh", *config_arguments),
            config.schedule.refresh_minutes * 60,
            policy_revision=config.discovery_revision,
        ),
    ]
    if config.controller.generation_enabled:
        jobs.append(
            ScheduledJob(
                "generate",
                ("generate", *config_arguments),
                config.schedule.generation_minutes * 60,
                requires_refresh=True,
                policy_revision=config.discovery_revision,
            )
        )
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

    return jobs


def scheduler_command(args: argparse.Namespace) -> int:
    config = load_config(args.config, load_secrets=False)
    jobs = controller_jobs(config, args.config)
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
            jobs_provider=lambda: controller_jobs(
                load_config(args.config, load_secrets=False), args.config
            ),
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
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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
        help="Add broader observations to the private collection and generation queue",
    )
    add_config_argument(seed_parser)
    seed_period = seed_parser.add_mutually_exclusive_group(required=True)
    seed_period.add_argument(
        "--window",
        choices=[window.value for window in ObservationWindow],
    )
    seed_period.add_argument("--start-date", help="Inclusive observation date (YYYY-MM-DD)")
    seed_parser.add_argument("--end-date", help="Inclusive observation date (YYYY-MM-DD)")
    seed_parser.add_argument(
        "--source",
        action="append",
        choices=[provider.value for provider in DiscoveryProvider],
        help="Override discovery with one provider; repeat to select multiple providers",
    )
    seed_parser.add_argument("--radius-km", type=int)
    seed_parser.add_argument("--latitude", type=float)
    seed_parser.add_argument("--longitude", type=float)
    seed_parser.add_argument("--species-limit", type=int)
    seed_parser.add_argument("--dry-run", action="store_true")
    seed_parser.set_defaults(func=seed_command)

    collection_parser = subparsers.add_parser(
        "collection", help="Manage private local display membership"
    )
    collection_subparsers = collection_parser.add_subparsers(
        dest="collection_command", required=True
    )
    collection_list_parser = collection_subparsers.add_parser(
        "list", help="List private collection membership and activation state"
    )
    add_config_argument(collection_list_parser)
    collection_list_parser.set_defaults(func=collection_list_command)
    collection_import_parser = collection_subparsers.add_parser(
        "import-approved", help="Explicitly add every currently approved local plate"
    )
    add_config_argument(collection_import_parser)
    collection_import_parser.add_argument("--dry-run", action="store_true")
    collection_import_parser.set_defaults(func=collection_import_command)
    collection_add_parser = collection_subparsers.add_parser(
        "add", help="Add one taxon to the private collection"
    )
    add_config_argument(collection_add_parser)
    collection_add_parser.add_argument("taxon_id", type=int)
    collection_add_parser.add_argument("--dry-run", action="store_true")
    collection_add_parser.set_defaults(func=collection_add_command)
    collection_remove_parser = collection_subparsers.add_parser(
        "remove", help="Remove one taxon from the private collection"
    )
    add_config_argument(collection_remove_parser)
    collection_remove_parser.add_argument("taxon_id", type=int)
    collection_remove_parser.add_argument("--dry-run", action="store_true")
    collection_remove_parser.set_defaults(func=collection_remove_command)

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
    retry_parser.add_argument(
        "--source-attempt",
        type=int,
        help="Edit a selected retained attempt instead of generating the first retry from scratch",
    )
    retry_parser.add_argument(
        "--source-candidate",
        help="Edit a human-rejected candidate retained under the controller archive",
    )
    retry_parser.add_argument(
        "--source-run",
        help="Select an archived failed generation run with --source-attempt",
    )
    retry_parser.add_argument(
        "--correction",
        action="append",
        help=(
            "Replace selected review findings with an operator-adjudicated correction; "
            "repeatable and requires a correction source"
        ),
    )
    retry_parser.add_argument(
        "--replace-approved",
        action="store_true",
        help="Withdraw a locally approved plate after human review and regenerate from scratch",
    )
    retry_parser.add_argument(
        "--reason",
        help="Human rejection reason required with --replace-approved",
    )
    retry_parser.add_argument(
        "--refresh-research",
        action="store_true",
        help="Discard cached species research and reference photos before retrying",
    )
    retry_parser.set_defaults(func=retry_command)

    status_parser = subparsers.add_parser("status", help="List approved and pending plates")
    add_config_argument(status_parser)
    status_parser.set_defaults(func=status_command)

    serve_parser = subparsers.add_parser("serve", help="Serve the approved catalog over HTTP")
    add_config_argument(serve_parser)
    serve_parser.set_defaults(func=serve_command)

    access_parser = subparsers.add_parser("access", help="Set up or recover private owner access")
    access_subparsers = access_parser.add_subparsers(dest="access_operation", required=True)
    access_bootstrap = access_subparsers.add_parser("bootstrap")
    add_config_argument(access_bootstrap)
    access_bootstrap.add_argument("--yes", action="store_true")
    access_bootstrap.add_argument(
        "--show-code",
        action="store_true",
        help="Show the setup code only in a private interactive terminal; never in JSON",
    )
    access_bootstrap.set_defaults(func=access_bootstrap_command)
    access_status = access_subparsers.add_parser("status")
    add_config_argument(access_status)
    access_status.set_defaults(func=access_status_command)
    access_recover = access_subparsers.add_parser(
        "recover",
        aliases=["reset"],
        help="Replace owner credentials locally without opening network setup",
    )
    add_config_argument(access_recover)
    access_recover.add_argument("--yes", action="store_true")
    access_recover.set_defaults(func=access_recover_command)

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
    catalog_prepare_parser.add_argument(
        "--replace-approved",
        action="store_true",
        help="Replace a published taxon through an explicit hash-bound migration",
    )
    catalog_prepare_parser.add_argument(
        "--reason",
        help="Human-reviewed migration reason required with --replace-approved",
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
    catalog_sync_parser.add_argument(
        "--apply-reviewed-migrations",
        action="store_true",
        help=(
            "Apply only hash-bound reviewed replacements from the source and retain a "
            "validated newer destination"
        ),
    )
    catalog_sync_parser.add_argument(
        "--retain-independent-approvals",
        action="store_true",
        help=(
            "Retain a validated destination approval when its exact taxon identity has "
            "no shared approval ancestry with the source"
        ),
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
    config_init_parser = config_subparsers.add_parser(
        "init", help="Create a private art-first configuration without replacing an existing file"
    )
    config_init_parser.add_argument("--destination", type=Path, required=True)
    config_init_parser.add_argument(
        "--container", action="store_true", help="Use /data paths inside Docker"
    )
    config_init_parser.set_defaults(func=config_init_command)

    birdbuddy_parser = subparsers.add_parser(
        "birdbuddy", help="Manage private Bird Buddy discovery authentication"
    )
    birdbuddy_subparsers = birdbuddy_parser.add_subparsers(dest="birdbuddy_command", required=True)
    birdbuddy_login_parser = birdbuddy_subparsers.add_parser(
        "login", help="Authorize and authenticate a Bird Buddy account"
    )
    add_config_argument(birdbuddy_login_parser)
    birdbuddy_login_parser.add_argument(
        "--feeder-id",
        help="Required when the account can access more than one feeder",
    )
    birdbuddy_login_parser.add_argument(
        "--confirm-authorized-access",
        action="store_true",
        help="Confirm Bird Buddy authorized automated API access for this account",
    )
    birdbuddy_login_parser.set_defaults(func=birdbuddy_login_command)
    birdbuddy_status_parser = birdbuddy_subparsers.add_parser(
        "status", help="Show redacted local Bird Buddy authentication and history status"
    )
    add_config_argument(birdbuddy_status_parser)
    birdbuddy_status_parser.set_defaults(func=birdbuddy_status_command)
    birdbuddy_logout_parser = birdbuddy_subparsers.add_parser(
        "logout", help="Remove local Bird Buddy authentication while preserving history"
    )
    add_config_argument(birdbuddy_logout_parser)
    birdbuddy_logout_parser.add_argument(
        "--yes", action="store_true", help="Remove local authentication state"
    )
    birdbuddy_logout_parser.set_defaults(func=birdbuddy_logout_command)

    birdnet_analyzer_parser = subparsers.add_parser(
        "birdnet-analyzer", help="Import private BirdNET Analyzer detection history"
    )
    birdnet_analyzer_subparsers = birdnet_analyzer_parser.add_subparsers(
        dest="birdnet_analyzer_command", required=True
    )
    birdnet_analyzer_import_parser = birdnet_analyzer_subparsers.add_parser(
        "import", help="Import a BirdNET Analyzer CSV export"
    )
    add_config_argument(birdnet_analyzer_import_parser)
    birdnet_analyzer_import_parser.add_argument("--csv", type=Path, required=True)
    birdnet_analyzer_import_parser.add_argument(
        "--observed-on",
        help="Explicit recording date (YYYY-MM-DD) applied to every row",
    )
    birdnet_analyzer_import_parser.add_argument("--dry-run", action="store_true")
    birdnet_analyzer_import_parser.set_defaults(func=birdnet_analyzer_import_command)

    ebird_parser = subparsers.add_parser(
        "ebird", help="Manage private history exported from your eBird account"
    )
    ebird_subparsers = ebird_parser.add_subparsers(dest="ebird_command", required=True)
    ebird_archive_parser = ebird_subparsers.add_parser(
        "archive", help="Import or inspect an official eBird Download My Data archive"
    )
    ebird_archive_subparsers = ebird_archive_parser.add_subparsers(
        dest="ebird_archive_command", required=True
    )
    ebird_archive_import_parser = ebird_archive_subparsers.add_parser(
        "import", help="Replace private eBird history from a complete ZIP or CSV export"
    )
    add_config_argument(ebird_archive_import_parser)
    ebird_archive_import_parser.add_argument("--archive", type=Path, required=True)
    ebird_archive_import_parser.add_argument(
        "--allow-history-reduction",
        action="store_true",
        help="Accept an export that omits previously imported checklists",
    )
    ebird_archive_import_parser.add_argument("--dry-run", action="store_true")
    ebird_archive_import_parser.set_defaults(func=ebird_archive_import_command)
    ebird_archive_status_parser = ebird_archive_subparsers.add_parser(
        "status", help="Show redacted local eBird archive history status"
    )
    add_config_argument(ebird_archive_status_parser)
    ebird_archive_status_parser.set_defaults(func=ebird_archive_status_command)

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
