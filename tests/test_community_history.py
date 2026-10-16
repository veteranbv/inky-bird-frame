from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from inky_bird_frame.community import CommunityHistory
from inky_bird_frame.errors import DataSourceError
from inky_bird_frame.http import read_private_json
from inky_bird_frame.journal import (
    JournalConflictError,
    JournalError,
    read_journal,
    save_collection,
)


def choices() -> dict[str, object]:
    return {
        "label": "Community trip",
        "start_date": "2001-03-01",
        "end_date": "2001-03-02",
        "latitude": "0.125",
        "longitude": "0.25",
        "radius_km": "10",
    }


def result() -> dict[str, object]:
    return {
        "total_results": 1,
        "results": [
            {
                "count": 4,
                "taxon": {
                    "id": 1,
                    "rank": "species",
                    "preferred_common_name": "Robin",
                    "name": "Turdus migratorius",
                },
            }
        ],
    }


def test_community_preview_is_complete_private_and_never_personal(tmp_path: Path) -> None:
    history = CommunityHistory(tmp_path)
    with patch("inky_bird_frame.community.get_json", return_value=result()) as query:
        history.preview(choices())
    assert "d1=2001-03-01" in query.call_args.args[0]
    assert not (tmp_path / "journal.json").exists()
    preview = history.status()
    encoded = history.path.read_text()
    assert "0.125" not in encoded and "0.25" not in encoded
    assert history.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(JournalConflictError):
        history.save("0" * 64)
    saved = history.save(str(preview["preview_hash"]))
    assert history.save(str(preview["preview_hash"])) == saved
    group = read_journal(tmp_path).collections[0]
    assert group.kind == "community-history"
    assert group.members[0].origin == "community-report" and not group.members[0].evidence
    history.cancel()
    assert len(read_journal(tmp_path).collections) == 1
    assert not (tmp_path / "generation-queue.json").exists()
    assert not (tmp_path / "collection.json").exists()


@pytest.mark.parametrize(
    "payload", [{"total_results": 501}, {"total_results": 2, "results": []}, {"results": []}]
)
def test_community_partial_or_oversized_response_never_commits(
    tmp_path: Path, payload: object
) -> None:
    history = CommunityHistory(tmp_path)
    with (
        patch("inky_bird_frame.community.get_json", return_value=payload),
        pytest.raises(JournalError),
    ):
        history.preview(choices())
    assert history.status() == {"status": "empty"}


def test_community_cas_empty_expiry_and_corrupt_preview(tmp_path: Path) -> None:
    history = CommunityHistory(tmp_path)
    with patch(
        "inky_bird_frame.community.get_json", return_value={"total_results": 0, "results": []}
    ):
        history.preview(choices())
    preview = history.status()
    save_collection(tmp_path, revision=0, label="Concurrent edit", taxon_ids=[])
    with pytest.raises(JournalConflictError):
        history.save(str(preview["preview_hash"]))
    raw = json.loads(history.path.read_text())
    raw["expires_at"] = "2000-01-01T00:00:00+00:00"
    history.path.write_text(json.dumps(raw))
    assert history.status() == {"status": "empty"}
    history.path.write_text('{"invalid": true}')
    history.path.chmod(0o600)
    with pytest.raises(JournalError):
        history.status()


def test_private_json_rejects_unsafe_oversized_and_symlinked_state(tmp_path: Path) -> None:
    path = tmp_path / "private.json"
    path.write_text('{"safe":true}')
    path.chmod(0o644)
    with pytest.raises(DataSourceError):
        read_private_json(path, maximum=100)
    path.chmod(0o600)
    assert read_private_json(path, maximum=100) == {"safe": True}
    with pytest.raises(DataSourceError):
        read_private_json(path, maximum=4)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(DataSourceError):
        read_private_json(link, maximum=100)
