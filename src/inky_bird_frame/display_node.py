"""Pull approved plates from a controller and rotate them on an Inky panel."""

from __future__ import annotations

import fcntl
import hashlib
import json
import random
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import quote

from .catalog import CatalogEntry, write_json_atomic
from .config import DisplayNodeConfig, RotationMode
from .display import show_on_inky
from .errors import CatalogError
from .http import get_bytes, get_json, write_bytes_atomic
from .selection import select_catalog_entry, select_shuffle_bag_entry

DISPLAY_STATE_SCHEMA_VERSION = 3


@dataclass(frozen=True)
class DisplayState:
    next_index: int = 0
    last_sha256: str = ""
    last_taxon_id: int | None = None
    shuffle_remaining: tuple[int, ...] = ()
    shuffle_bag_remaining: tuple[int, ...] = ()
    shuffle_bag_seen: tuple[int, ...] = ()
    last_prioritized_detection_at: str | None = None
    prioritized_detection_taxa: tuple[int, ...] = ()


@contextmanager
def exclusive_display_cycle_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "display-cycle.lock").open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CatalogError("Another display cycle is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def parse_catalog_entries(payload: object) -> list[CatalogEntry]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise CatalogError("Controller returned an unsupported catalog")
    species = payload.get("species")
    if not isinstance(species, list):
        raise CatalogError("Controller catalog has no species list")
    entries: list[CatalogEntry] = []
    taxon_ids: set[int] = set()
    for raw in species:
        if not isinstance(raw, dict):
            raise CatalogError("Controller catalog entry must be an object")
        taxon_id = raw.get("taxon_id")
        strings = {
            field: raw.get(field)
            for field in (
                "common_name",
                "scientific_name",
                "slug",
                "portrait_path",
                "portrait_sha256",
                "display_path",
                "display_sha256",
                "approved_at",
            )
        }
        if (
            not isinstance(taxon_id, int)
            or isinstance(taxon_id, bool)
            or taxon_id <= 0
            or any(not isinstance(value, str) for value in strings.values())
        ):
            raise CatalogError("Controller catalog entry has invalid fields")
        if taxon_id in taxon_ids:
            raise CatalogError(f"Controller catalog has duplicate taxon ID: {taxon_id}")
        taxon_ids.add(taxon_id)
        observation_count = raw.get("observation_count", 1)
        latest_detection_at = raw.get("latest_detection_at")
        if (
            not isinstance(observation_count, int)
            or isinstance(observation_count, bool)
            or observation_count < 0
        ):
            raise CatalogError("Controller catalog entry has invalid observation count")
        if latest_detection_at is not None:
            _detection_datetime(latest_detection_at, "controller catalog")
        entries.append(
            CatalogEntry(
                taxon_id=taxon_id,
                common_name=cast(str, strings["common_name"]),
                scientific_name=cast(str, strings["scientific_name"]),
                slug=cast(str, strings["slug"]),
                portrait_path=cast(str, strings["portrait_path"]),
                portrait_sha256=cast(str, strings["portrait_sha256"]),
                display_path=cast(str, strings["display_path"]),
                display_sha256=cast(str, strings["display_sha256"]),
                approved_at=cast(str, strings["approved_at"]),
                observation_count=observation_count,
                latest_detection_at=cast(str | None, latest_detection_at),
            )
        )
    if not entries:
        raise CatalogError("Controller catalog has no approved species")
    return entries


def _detection_datetime(value: object, source: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CatalogError(f"Invalid latest detection timestamp in {source}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CatalogError(f"Invalid latest detection timestamp in {source}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CatalogError(f"Latest detection timestamp must include a timezone in {source}")
    return parsed.astimezone(UTC)


def _latest_unseen_detection(
    entries: list[CatalogEntry],
    last_prioritized_at: str | None,
    prioritized_taxa: tuple[int, ...],
) -> CatalogEntry | None:
    watermark = (
        _detection_datetime(last_prioritized_at, "display-node state")
        if last_prioritized_at is not None
        else None
    )
    candidates = [
        (_detection_datetime(entry.latest_detection_at, "controller catalog"), entry)
        for entry in entries
        if entry.latest_detection_at is not None
    ]
    consumed_taxa = set(prioritized_taxa)
    unseen = [
        item
        for item in candidates
        if watermark is None
        or item[0] > watermark
        or (item[0] == watermark and item[1].taxon_id not in consumed_taxa)
    ]
    if not unseen:
        return None
    return max(unseen, key=lambda item: (item[0], -item[1].taxon_id))[1]


def _state_taxon_ids(raw: object, path: Path) -> tuple[int, ...]:
    if (
        not isinstance(raw, list)
        or any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in raw)
        or len(raw) != len(set(raw))
    ):
        raise CatalogError(f"Invalid display-node state: {path}")
    return tuple(raw)


def _read_state(path: Path) -> DisplayState:
    if not path.is_file():
        return DisplayState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogError(f"Invalid display-node state: {path}") from exc
    if not isinstance(raw, dict):
        raise CatalogError(f"Invalid display-node state: {path}")
    schema_version = raw.get("schema_version", 1)
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in {1, 2, DISPLAY_STATE_SCHEMA_VERSION}
    ):
        raise CatalogError(f"Invalid display-node state: {path}")
    if schema_version == DISPLAY_STATE_SCHEMA_VERSION and any(
        name not in raw
        for name in (
            "next_index",
            "last_sha256",
            "last_taxon_id",
            "shuffle_remaining",
            "shuffle_bag_remaining",
            "shuffle_bag_seen",
            "last_prioritized_detection_at",
            "prioritized_detection_taxa",
        )
    ):
        raise CatalogError(f"Invalid display-node state: {path}")
    next_index = raw.get("next_index", 0)
    last_sha256 = raw.get("last_sha256", "")
    last_taxon_id = raw.get("last_taxon_id")
    shuffle_remaining = _state_taxon_ids(raw.get("shuffle_remaining", []), path)
    shuffle_bag_remaining = _state_taxon_ids(raw.get("shuffle_bag_remaining", []), path)
    shuffle_bag_seen = _state_taxon_ids(raw.get("shuffle_bag_seen", []), path)
    last_prioritized_detection_at = raw.get("last_prioritized_detection_at")
    prioritized_detection_taxa = _state_taxon_ids(raw.get("prioritized_detection_taxa", []), path)
    if (
        not isinstance(next_index, int)
        or isinstance(next_index, bool)
        or next_index < 0
        or not isinstance(last_sha256, str)
        or (
            last_taxon_id is not None
            and (
                not isinstance(last_taxon_id, int)
                or isinstance(last_taxon_id, bool)
                or last_taxon_id <= 0
            )
        )
        or set(shuffle_bag_remaining) & set(shuffle_bag_seen)
        or (
            last_prioritized_detection_at is not None
            and not isinstance(last_prioritized_detection_at, str)
        )
        or (last_prioritized_detection_at is None and prioritized_detection_taxa)
        or (last_prioritized_detection_at is not None and not prioritized_detection_taxa)
    ):
        raise CatalogError(f"Invalid display-node state: {path}")
    if isinstance(last_prioritized_detection_at, str):
        _detection_datetime(last_prioritized_detection_at, "display-node state")
    return DisplayState(
        next_index=next_index,
        last_sha256=last_sha256,
        last_taxon_id=last_taxon_id,
        shuffle_remaining=shuffle_remaining,
        shuffle_bag_remaining=shuffle_bag_remaining,
        shuffle_bag_seen=shuffle_bag_seen,
        last_prioritized_detection_at=last_prioritized_detection_at,
        prioritized_detection_taxa=prioritized_detection_taxa,
    )


def _write_state(
    path: Path,
    *,
    selected: CatalogEntry,
    next_index: int,
    shuffle_remaining: list[int],
    shuffle_bag_remaining: list[int],
    shuffle_bag_seen: list[int],
    last_sha256: str,
    last_prioritized_detection_at: str | None,
    prioritized_detection_taxa: list[int],
) -> None:
    write_json_atomic(
        path,
        {
            "schema_version": DISPLAY_STATE_SCHEMA_VERSION,
            "next_index": next_index,
            "last_sha256": last_sha256,
            "last_taxon_id": selected.taxon_id,
            "shuffle_remaining": shuffle_remaining,
            "shuffle_bag_remaining": shuffle_bag_remaining,
            "shuffle_bag_seen": shuffle_bag_seen,
            "last_prioritized_detection_at": last_prioritized_detection_at,
            "prioritized_detection_taxa": prioritized_detection_taxa,
        },
    )


def _advanced_priority_state(state: DisplayState, selected: CatalogEntry) -> tuple[str, list[int]]:
    timestamp = selected.latest_detection_at
    if timestamp is None:
        raise CatalogError("Prioritized catalog entry has no detection timestamp")
    selected_at = _detection_datetime(timestamp, "controller catalog")
    if state.last_prioritized_detection_at is not None and selected_at == _detection_datetime(
        state.last_prioritized_detection_at, "display-node state"
    ):
        taxa = list(state.prioritized_detection_taxa)
        watermark = state.last_prioritized_detection_at
    else:
        taxa = []
        watermark = timestamp
    if selected.taxon_id not in taxa:
        taxa.append(selected.taxon_id)
    return watermark, taxa


def run_display_cycle(
    config: DisplayNodeConfig,
    *,
    force: bool = False,
    rng: random.Random | random.SystemRandom | None = None,
) -> dict[str, object]:
    with exclusive_display_cycle_lock(config.state_dir):
        catalog_payload = get_json(f"{config.controller_url}/v1/catalog", 20.0)
        entries = parse_catalog_entries(catalog_payload)
        state_path = config.state_dir / "state.json"
        state = _read_state(state_path)
        shuffle_remaining = list(state.shuffle_remaining)
        shuffle_bag_remaining = list(state.shuffle_bag_remaining)
        shuffle_bag_seen = list(state.shuffle_bag_seen)
        prioritized = (
            _latest_unseen_detection(
                entries,
                state.last_prioritized_detection_at,
                state.prioritized_detection_taxa,
            )
            if config.prioritize_latest_detection
            else None
        )
        prioritized_at = state.last_prioritized_detection_at
        prioritized_taxa = list(state.prioritized_detection_taxa)
        if prioritized is not None:
            selected = prioritized
            prioritized_at, prioritized_taxa = _advanced_priority_state(state, selected)
            following_index = state.next_index
            if config.rotation_mode is RotationMode.SEQUENTIAL:
                next_entry = entries[state.next_index % len(entries)]
                if next_entry.taxon_id == selected.taxon_id:
                    following_index = (state.next_index + 1) % len(entries)
            elif config.rotation_mode is RotationMode.SHUFFLE:
                active_taxa = {entry.taxon_id for entry in entries}
                shuffle_remaining = [
                    taxon_id
                    for taxon_id in shuffle_remaining
                    if taxon_id in active_taxa and taxon_id != selected.taxon_id
                ]
                if not shuffle_remaining:
                    shuffle_remaining = [
                        entry.taxon_id for entry in entries if entry.taxon_id != selected.taxon_id
                    ]
                    (rng or random.SystemRandom()).shuffle(shuffle_remaining)
            elif config.rotation_mode is RotationMode.SHUFFLE_BAG:
                shuffle_bag_remaining = [
                    taxon_id for taxon_id in shuffle_bag_remaining if taxon_id != selected.taxon_id
                ]
                if selected.taxon_id not in shuffle_bag_seen:
                    shuffle_bag_seen.append(selected.taxon_id)
        elif config.rotation_mode is RotationMode.SHUFFLE_BAG:
            selected, shuffle_bag_remaining, shuffle_bag_seen = select_shuffle_bag_entry(
                entries,
                last_taxon_id=state.last_taxon_id,
                shuffle_bag_remaining=shuffle_bag_remaining,
                shuffle_bag_seen=shuffle_bag_seen,
                rng=rng,
            )
            following_index = state.next_index
        else:
            selected, following_index, shuffle_remaining = select_catalog_entry(
                entries,
                config.rotation_mode,
                next_index=state.next_index,
                last_taxon_id=state.last_taxon_id,
                shuffle_remaining=shuffle_remaining,
                rng=rng,
            )
        if selected.display_sha256 == state.last_sha256 and not force:
            _write_state(
                state_path,
                selected=selected,
                next_index=following_index,
                shuffle_remaining=shuffle_remaining,
                shuffle_bag_remaining=shuffle_bag_remaining,
                shuffle_bag_seen=shuffle_bag_seen,
                last_sha256=state.last_sha256,
                last_prioritized_detection_at=prioritized_at,
                prioritized_detection_taxa=prioritized_taxa,
            )
            return {
                "display_update": "unchanged",
                "taxon_id": selected.taxon_id,
                "common_name": selected.common_name,
                "selection_reason": ("latest_detection" if prioritized is not None else "rotation"),
            }

        encoded_path = quote(selected.display_path, safe="/")
        image_bytes = get_bytes(f"{config.controller_url}/v1/assets/{encoded_path}", 60.0)
        actual_hash = hashlib.sha256(image_bytes).hexdigest()
        if actual_hash != selected.display_sha256:
            raise CatalogError(
                f"Downloaded asset checksum mismatch for {selected.common_name}: {actual_hash}"
            )
        image_path = config.state_dir / "cache" / f"{selected.taxon_id}-{actual_hash[:12]}.png"
        write_bytes_atomic(image_path, image_bytes)
        display_size = show_on_inky(image_path)
        _write_state(
            state_path,
            selected=selected,
            next_index=following_index,
            shuffle_remaining=shuffle_remaining,
            shuffle_bag_remaining=shuffle_bag_remaining,
            shuffle_bag_seen=shuffle_bag_seen,
            last_sha256=actual_hash,
            last_prioritized_detection_at=prioritized_at,
            prioritized_detection_taxa=prioritized_taxa,
        )
        return {
            "display_update": "sent",
            "taxon_id": selected.taxon_id,
            "common_name": selected.common_name,
            "display_size": display_size,
            "sha256": actual_hash,
            "selection_reason": "latest_detection" if prioritized is not None else "rotation",
        }
