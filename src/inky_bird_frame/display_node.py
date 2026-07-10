"""Pull approved plates from a controller and rotate them on an Inky panel."""

from __future__ import annotations

import fcntl
import hashlib
import json
import random
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import quote

from .catalog import CatalogEntry, write_json_atomic
from .config import DisplayNodeConfig, RotationMode
from .display import show_on_inky
from .errors import CatalogError
from .http import get_bytes, get_json, write_bytes_atomic
from .selection import select_catalog_entry, select_shuffle_bag_entry

DISPLAY_STATE_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class DisplayState:
    next_index: int = 0
    last_sha256: str = ""
    last_taxon_id: int | None = None
    shuffle_remaining: tuple[int, ...] = ()
    shuffle_bag_remaining: tuple[int, ...] = ()
    shuffle_bag_seen: tuple[int, ...] = ()


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
        if (
            not isinstance(observation_count, int)
            or isinstance(observation_count, bool)
            or observation_count < 0
        ):
            raise CatalogError("Controller catalog entry has invalid observation count")
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
            )
        )
    if not entries:
        raise CatalogError("Controller catalog has no approved species")
    return entries


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
        or schema_version not in {1, DISPLAY_STATE_SCHEMA_VERSION}
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
        )
    ):
        raise CatalogError(f"Invalid display-node state: {path}")
    next_index = raw.get("next_index", 0)
    last_sha256 = raw.get("last_sha256", "")
    last_taxon_id = raw.get("last_taxon_id")
    shuffle_remaining = _state_taxon_ids(raw.get("shuffle_remaining", []), path)
    shuffle_bag_remaining = _state_taxon_ids(raw.get("shuffle_bag_remaining", []), path)
    shuffle_bag_seen = _state_taxon_ids(raw.get("shuffle_bag_seen", []), path)
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
    ):
        raise CatalogError(f"Invalid display-node state: {path}")
    return DisplayState(
        next_index=next_index,
        last_sha256=last_sha256,
        last_taxon_id=last_taxon_id,
        shuffle_remaining=shuffle_remaining,
        shuffle_bag_remaining=shuffle_bag_remaining,
        shuffle_bag_seen=shuffle_bag_seen,
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
        },
    )


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
        if config.rotation_mode is RotationMode.SHUFFLE_BAG:
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
            )
            return {
                "display_update": "unchanged",
                "taxon_id": selected.taxon_id,
                "common_name": selected.common_name,
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
        )
        return {
            "display_update": "sent",
            "taxon_id": selected.taxon_id,
            "common_name": selected.common_name,
            "display_size": display_size,
            "sha256": actual_hash,
        }
