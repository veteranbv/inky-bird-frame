"""Pull approved plates from a controller and rotate them on an Inky panel."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast
from urllib.parse import quote

from .catalog import CatalogEntry, write_json_atomic
from .config import DisplayNodeConfig
from .display import show_on_inky
from .errors import CatalogError
from .http import get_bytes, get_json, write_bytes_atomic


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
            )
        )
    if not entries:
        raise CatalogError("Controller catalog has no approved species")
    return entries


def _read_state(path: Path) -> tuple[int, str]:
    if not path.is_file():
        return 0, ""
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise CatalogError(f"Invalid display-node state: {path}") from exc
    if not isinstance(raw, dict):
        raise CatalogError(f"Invalid display-node state: {path}")
    next_index = raw.get("next_index", 0)
    last_sha256 = raw.get("last_sha256", "")
    if not isinstance(next_index, int) or not isinstance(last_sha256, str):
        raise CatalogError(f"Invalid display-node state: {path}")
    return next_index, last_sha256


def run_display_cycle(config: DisplayNodeConfig, *, force: bool = False) -> dict[str, object]:
    catalog_payload = get_json(f"{config.controller_url}/v1/catalog", 20.0)
    entries = parse_catalog_entries(catalog_payload)
    state_path = config.state_dir / "state.json"
    next_index, last_sha256 = _read_state(state_path)
    selected = entries[next_index % len(entries)]
    following_index = (next_index + 1) % len(entries)
    if selected.display_sha256 == last_sha256 and not force:
        write_json_atomic(
            state_path,
            {
                "schema_version": 1,
                "next_index": following_index,
                "last_sha256": last_sha256,
                "last_taxon_id": selected.taxon_id,
            },
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
    write_json_atomic(
        state_path,
        {
            "schema_version": 1,
            "next_index": following_index,
            "last_sha256": actual_hash,
            "last_taxon_id": selected.taxon_id,
        },
    )
    return {
        "display_update": "sent",
        "taxon_id": selected.taxon_id,
        "common_name": selected.common_name,
        "display_size": display_size,
        "sha256": actual_hash,
    }
