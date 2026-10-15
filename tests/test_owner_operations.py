from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from inky_bird_frame.config import load_config
from inky_bird_frame.owner_operations import (
    MAX_RESULT_BYTES,
    OwnerOperationBusyError,
    OwnerOperationError,
    OwnerOperations,
    execute_owner_operation,
)
from inky_bird_frame.settings import initialize_config, settings_status
from inky_bird_frame.web import create_app


def completed(read: Callable[[], dict[str, object]]) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        receipt = read()
        if receipt["status"] not in {"pending", "running"}:
            return receipt
        time.sleep(0.005)
    raise AssertionError("Synthetic operation did not finish")


def test_private_receipts_are_idempotent_and_do_not_replay(tmp_path: Path) -> None:
    worker = OwnerOperations(tmp_path)
    calls: list[str] = []
    key = "synthetic-request-key"

    def execute() -> dict[str, object]:
        calls.append("called")
        return {"count": 2}

    def duplicate_execute() -> dict[str, object]:
        calls.append("wrong")
        return {}

    try:
        receipt = worker.submit(
            "refresh",
            key=key,
            inputs={"revision": "a"},
            execute=execute,
        )
        result = completed(lambda: worker.status(str(receipt["operation_id"])))
        assert result["status"] == "succeeded"
        assert result["result"] == {"count": 2}
        assert "key_hash" not in json.dumps(result)
        assert worker.path.stat().st_mode & 0o777 == 0o600
        duplicate = worker.submit(
            "refresh",
            key=key,
            inputs={"revision": "a"},
            execute=duplicate_execute,
        )
        assert duplicate["operation_id"] == receipt["operation_id"]
        assert calls == ["called"]
        with pytest.raises(OwnerOperationError, match="different"):
            worker.submit("refresh", key=key, inputs={"revision": "b"}, execute=lambda: {})
    finally:
        worker.close()


def test_one_active_operation_across_workers_and_no_unbounded_queue(tmp_path: Path) -> None:
    first, second = OwnerOperations(tmp_path), OwnerOperations(tmp_path)
    release, started = threading.Event(), threading.Event()

    def execute() -> dict[str, object]:
        started.set()
        assert release.wait(5)
        return {}

    try:
        receipt = first.submit("refresh", key="first-synthetic-key", inputs={}, execute=execute)
        assert started.wait(2)
        assert second.status(str(receipt["operation_id"]))["status"] == "running"
        assert (
            second.submit("refresh", key="first-synthetic-key", inputs={}, execute=lambda: {})[
                "operation_id"
            ]
            == receipt["operation_id"]
        )
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [
                pool.submit(
                    second.submit,
                    "refresh",
                    key=f"another-synthetic-{n}",
                    inputs={},
                    execute=lambda: {},
                )
                for n in range(3)
            ]
            for future in futures:
                with pytest.raises(OwnerOperationBusyError):
                    future.result()
        release.set()
        completed(lambda: first.status(str(receipt["operation_id"])))
        follow = second.submit("refresh", key="next-synthetic-key", inputs={}, execute=lambda: {})
        completed(lambda: second.status(str(follow["operation_id"])))
        assert len(first.status()["operations"]) == 2  # type: ignore[arg-type]
    finally:
        release.set()
        first.close()
        second.close()


def test_restart_marks_unknown_work_interrupted_without_replay(tmp_path: Path) -> None:
    worker = OwnerOperations(tmp_path)
    receipt = worker.submit(
        "retry", key="interrupted-synthetic-key", inputs={"taxon_id": 1}, execute=lambda: {}
    )
    completed(lambda: worker.status(str(receipt["operation_id"])))
    worker.close()
    raw = json.loads(worker.path.read_text())
    raw["receipts"][0]["status"] = "running"
    raw["receipts"][0]["result"] = None
    worker.path.write_text(json.dumps(raw))
    restarted = OwnerOperations(tmp_path)
    try:
        assert restarted.status(str(receipt["operation_id"]))["status"] == "interrupted"
        with patch("inky_bird_frame.owner_operations._stamp"):
            duplicate = restarted.submit(
                "retry",
                key="interrupted-synthetic-key",
                inputs={"taxon_id": 1},
                execute=lambda: pytest.fail("replayed"),
            )
        assert duplicate["status"] == "interrupted"
    finally:
        restarted.close()


def test_errors_and_oversized_results_are_sanitized(tmp_path: Path) -> None:
    worker = OwnerOperations(tmp_path)
    try:

        def fail() -> dict[str, object]:
            raise RuntimeError("DO_NOT_STORE email@example.test /private/path token=DO_NOT_STORE")

        receipt = worker.submit("refresh", key="failure-synthetic-key", inputs={}, execute=fail)
        result = completed(lambda: worker.status(str(receipt["operation_id"])))
        assert result["status"] == "failed"
        assert "DO_NOT_STORE" not in worker.path.read_text()
        receipt = worker.submit(
            "refresh",
            key="oversize-synthetic-key",
            inputs={},
            execute=lambda: {"too_large": "x" * MAX_RESULT_BYTES},
        )
        result = completed(lambda: worker.status(str(receipt["operation_id"])))
        assert result["status"] == "failed"
        assert "exceeds" in str(result["result"])
    finally:
        worker.close()


@pytest.mark.parametrize("kind", ["permissions", "symlink", "invalid", "version"])
def test_corrupt_or_insecure_receipts_fail_closed(tmp_path: Path, kind: str) -> None:
    worker = OwnerOperations(tmp_path)
    receipt = worker.submit("refresh", key="private-synthetic-key", inputs={}, execute=lambda: {})
    completed(lambda: worker.status(str(receipt["operation_id"])))
    worker.close()
    if kind == "permissions":
        worker.path.chmod(0o644)
    elif kind == "symlink":
        other = tmp_path / "private-backup.json"
        worker.path.rename(other)
        worker.path.symlink_to(other)
    elif kind == "invalid":
        worker.path.write_text("DO_NOT_ECHO")
    else:
        raw = json.loads(worker.path.read_text())
        raw["schema_version"] = True
        worker.path.write_text(json.dumps(raw))
    restarted = OwnerOperations(tmp_path)
    try:
        with pytest.raises(OwnerOperationError) as error:
            restarted.status()
        assert "DO_NOT_ECHO" not in str(error.value) and str(tmp_path) not in str(error.value)
    finally:
        restarted.close()


def test_adapter_refresh_uses_real_zero_source_logic_and_rejects_stale_settings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    initialize_config(path)
    config = load_config(path)
    revision = str(settings_status(config)["revision"])
    with patch("inky_bird_frame.controller.generate_candidate") as model:
        result = execute_owner_operation(config, "refresh", revision=revision)
    model.assert_not_called()
    assert result["species_count"] == 0
    assert result["providers"] == []
    with pytest.raises(OwnerOperationError, match="Settings changed"):
        execute_owner_operation(config, "refresh", revision="old")


def test_adapter_test_delivery_never_claims_failed_destinations_received_it(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    initialize_config(path)
    config = load_config(path)
    with patch(
        "inky_bird_frame.notifications.send_notification_test",
        return_value={
            "delivered": ["one"],
            "failures": [{"name": "two", "error_type": "RuntimeError"}],
        },
    ):
        result = execute_owner_operation(
            config, "notification-test", revision=str(settings_status(config)["revision"])
        )
    assert result["failed_count"] == result["delivered_count"] == 1
    assert "failed" in str(result["message"])


def test_http_operation_boundary_and_no_secret_or_model_operation(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    initialize_config(path)
    origin = "https://inky.example.test"
    path.write_text(
        path.read_text().replace(
            '["controller"]', '["controller"]\nowner_allowed_origins = ["' + origin + '"]'
        )
    )
    config = load_config(path)
    with TestClient(create_app(config.controller, app_config=config), base_url=origin) as client:
        assert client.get("/v1/owner/operations").status_code == 401
        signed = client.post(
            "/v1/owner/register",
            json={
                "username": "fixture-owner",
                "passphrase": "long synthetic password",
            },
            headers={"Origin": origin},
        )
        headers = {"Origin": origin, "X-Inky-CSRF": signed.json()["data"]["csrf"]}
        body = {
            "kind": "refresh",
            "key": "http-synthetic-operation",
            "revision": settings_status(config)["revision"],
            "confirm": True,
        }
        assert client.post("/v1/owner/operations", json=body).status_code == 403
        assert (
            client.post(
                "/v1/owner/operations", json={**body, "password": "DO_NOT_STORE"}, headers=headers
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/v1/owner/operations", json={**body, "kind": "generate"}, headers=headers
            ).status_code
            == 400
        )
        response = client.post("/v1/owner/operations", json=body, headers=headers)
        assert response.status_code == 200
        receipt_id = response.json()["data"]["operation_id"]
        result = completed(lambda: client.get("/v1/owner/operations/" + receipt_id).json()["data"])
        assert result["status"] == "succeeded"
        assert (
            client.post("/v1/owner/operations", json=body, headers=headers).json()["data"][
                "operation_id"
            ]
            == receipt_id
        )
        assert (
            "DO_NOT_STORE"
            not in (config.controller.state_dir / "owner-operations.json").read_text()
        )
        assert not (config.controller.state_dir / "generation-queue.json").exists()
