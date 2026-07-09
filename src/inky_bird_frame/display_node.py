"""Pull approved plates from a controller and rotate them on an Inky panel."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import quote

from .catalog import CatalogEntry, write_json_atomic
from .config import DisplayNodeConfig
from .display import show_on_inky
from .errors import CatalogError
from .http import get_bytes, get_json, write_bytes_atomic
from .selection import select_catalog_entry


@dataclass(frozen=True)
class DisplayState:
    next_index: int = 0
    last_sha256: str = ""
    last_taxon_id: int | None = None
    shuffle_remaining: tuple[int, ...] = ()


def parse_catalog_entries(payload: object) -> list[CatalogEntry]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise CatalogError("Controller returned an unsupported catalog")
    species = payload.get("species")
    if not isinstance(species, list):
        raise CatalogError("Controller catalog has no species list")
    entries: list[CatalogEntry] = []
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
        if not isinstance(taxon_id, int) or any(
            not isinstance(value, str) for value in strings.values()
        ):
            raise CatalogError("Controller catalog entry has invalid fields")
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


def _read_state(path: Path) -> DisplayState:
    if not path.is_file():
        return DisplayState()
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise CatalogError(f"Invalid display-node state: {path}") from exc
    if not isinstance(raw, dict):
        raise CatalogError(f"Invalid display-node state: {path}")
    next_index = raw.get("next_index", 0)
    last_sha256 = raw.get("last_sha256", "")
    last_taxon_id = raw.get("last_taxon_id")
    shuffle_remaining = raw.get("shuffle_remaining", [])
    if (
        not isinstance(next_index, int)
        or isinstance(next_index, bool)
        or next_index < 0
        or not isinstance(last_sha256, str)
        or (
            last_taxon_id is not None
            and (not isinstance(last_taxon_id, int) or isinstance(last_taxon_id, bool))
        )
        or not isinstance(shuffle_remaining, list)
        or any(not isinstance(item, int) or isinstance(item, bool) for item in shuffle_remaining)
    ):
        raise CatalogError(f"Invalid display-node state: {path}")
    return DisplayState(
        next_index=next_index,
        last_sha256=last_sha256,
        last_taxon_id=last_taxon_id,
        shuffle_remaining=tuple(shuffle_remaining),
    )


def _write_state(
    path: Path,
    *,
    selected: CatalogEntry,
    next_index: int,
    shuffle_remaining: list[int],
    last_sha256: str,
) -> None:
    write_json_atomic(
        path,
        {
            "schema_version": 1,
            "next_index": next_index,
            "last_sha256": last_sha256,
            "last_taxon_id": selected.taxon_id,
            "shuffle_remaining": shuffle_remaining,
        },
    )


def run_display_cycle(
    config: DisplayNodeConfig,
    *,
    force: bool = False,
    rng: random.Random | random.SystemRandom | None = None,
) -> dict[str, object]:
    catalog_payload = get_json(f"{config.controller_url}/v1/catalog", 20.0)
    entries = parse_catalog_entries(catalog_payload)
    state_path = config.state_dir / "state.json"
    state = _read_state(state_path)
    selected, following_index, shuffle_remaining = select_catalog_entry(
        entries,
        config.rotation_mode,
        next_index=state.next_index,
        last_taxon_id=state.last_taxon_id,
        shuffle_remaining=list(state.shuffle_remaining),
        rng=rng,
    )
    if selected.display_sha256 == state.last_sha256 and not force:
        _write_state(
            state_path,
            selected=selected,
            next_index=following_index,
            shuffle_remaining=shuffle_remaining,
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
        last_sha256=actual_hash,
    )
    return {
        "display_update": "sent",
        "taxon_id": selected.taxon_id,
        "common_name": selected.common_name,
        "display_size": display_size,
        "sha256": actual_hash,
    }
