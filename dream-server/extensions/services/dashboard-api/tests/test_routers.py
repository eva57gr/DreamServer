"""Router-level integration tests for the Dream Server Dashboard API."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Health & Core
# ---------------------------------------------------------------------------


def test_health_returns_ok(test_client):
    """GET /health should return 200 with status 'ok' — no auth required."""
    resp = test_client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "timestamp" in data


# ---------------------------------------------------------------------------
# Auth enforcement — no Bearer token → 401
# ---------------------------------------------------------------------------


def test_setup_status_requires_auth(test_client):
    """GET /api/setup/status without auth header → 401."""
    resp = test_client.get("/api/setup/status")
    assert resp.status_code == 401


def test_api_status_requires_auth(test_client):
    """GET /api/status without auth header → 401."""
    resp = test_client.get("/api/status")
    assert resp.status_code == 401


def test_privacy_shield_status_requires_auth(test_client):
    """GET /api/privacy-shield/status without auth header → 401."""
    resp = test_client.get("/api/privacy-shield/status")
    assert resp.status_code == 401


def test_workflows_requires_auth(test_client):
    """GET /api/workflows without auth header → 401."""
    resp = test_client.get("/api/workflows")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Setup router
# ---------------------------------------------------------------------------


def test_setup_status_authenticated(test_client, setup_config_dir):
    """GET /api/setup/status with auth → 200, returns first_run and personas_available."""
    resp = test_client.get("/api/setup/status", headers=test_client.auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "first_run" in data
    assert "personas_available" in data
    assert isinstance(data["personas_available"], list)
    assert len(data["personas_available"]) > 0


def test_setup_status_first_run_true(test_client, setup_config_dir):
    """first_run is True when setup-complete.json does not exist."""
    resp = test_client.get("/api/setup/status", headers=test_client.auth_headers)
    assert resp.status_code == 200
    assert resp.json()["first_run"] is True


def test_setup_status_first_run_false(test_client, setup_config_dir):
    """first_run is False when setup-complete.json exists."""
    (setup_config_dir / "setup-complete.json").write_text('{"completed_at": "now"}')
    resp = test_client.get("/api/setup/status", headers=test_client.auth_headers)
    assert resp.status_code == 200
    assert resp.json()["first_run"] is False


def test_setup_persona_valid(test_client, setup_config_dir):
    """POST /api/setup/persona with valid persona → 200, writes persona.json."""
    resp = test_client.post(
        "/api/setup/persona",
        json={"persona": "general"},
        headers=test_client.auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["persona"] == "general"
    persona_file = setup_config_dir / "persona.json"
    assert persona_file.exists()


def test_setup_persona_invalid(test_client, setup_config_dir):
    """POST /api/setup/persona with invalid persona → 400."""
    resp = test_client.post(
        "/api/setup/persona",
        json={"persona": "nonexistent-persona"},
        headers=test_client.auth_headers,
    )
    assert resp.status_code == 400


def test_setup_complete(test_client, setup_config_dir):
    """POST /api/setup/complete → 200, writes setup-complete.json."""
    resp = test_client.post("/api/setup/complete", headers=test_client.auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert (setup_config_dir / "setup-complete.json").exists()


def test_list_personas(test_client):
    """GET /api/setup/personas → 200, returns list with at least general/coding/creative."""
    resp = test_client.get("/api/setup/personas", headers=test_client.auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "personas" in data
    persona_ids = [p["id"] for p in data["personas"]]
    assert "general" in persona_ids
    assert "coding" in persona_ids


def test_get_persona_info_existing(test_client):
    """GET /api/setup/persona/general → 200 with persona details."""
    resp = test_client.get("/api/setup/persona/general", headers=test_client.auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "general"
    assert "name" in data
    assert "system_prompt" in data


def test_get_persona_info_nonexistent(test_client):
    """GET /api/setup/persona/nonexistent → 404."""
    resp = test_client.get("/api/setup/persona/nonexistent", headers=test_client.auth_headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Preflight endpoints
# ---------------------------------------------------------------------------


def test_preflight_ports_empty_list(test_client):
    """POST /api/preflight/ports with empty ports list → 200, no conflicts."""
    resp = test_client.post(
        "/api/preflight/ports",
        json={"ports": []},
        headers=test_client.auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["conflicts"] == []
    assert data["available"] is True


def test_preflight_required_ports_no_auth(test_client):
    """GET /api/preflight/required-ports → 200, no auth required."""
    resp = test_client.get("/api/preflight/required-ports")
    assert resp.status_code == 200
    data = resp.json()
    assert "ports" in data
    assert isinstance(data["ports"], list)


# ---------------------------------------------------------------------------
# Workflow path-traversal and catalog miss
# ---------------------------------------------------------------------------


def test_workflow_enable_path_traversal(test_client):
    """POST with path-traversal chars in workflow_id → 400 (regex rejects it)."""
    resp = test_client.post(
        "/api/workflows/../../etc/passwd/enable",
        headers=test_client.auth_headers,
    )
    # FastAPI path matching will either 404 (no route match) or 400 (validation).
    # Either is acceptable — the traversal must NOT succeed (not 200).
    assert resp.status_code in (400, 404, 422)


def test_workflow_enable_unknown_id(test_client):
    """POST /api/workflows/valid-id/enable → 404 when not in catalog."""
    resp = test_client.post(
        "/api/workflows/valid-id/enable",
        headers=test_client.auth_headers,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Privacy Shield (mock subprocess so docker is not required)
# ---------------------------------------------------------------------------


def test_privacy_shield_status_with_mock(test_client):
    """GET /api/privacy-shield/status → 200 with mocked docker subprocess."""

    async def _fake_create_subprocess(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=_fake_create_subprocess):
        resp = test_client.get(
            "/api/privacy-shield/status",
            headers=test_client.auth_headers,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "enabled" in data
    assert "container_running" in data
    assert "port" in data


# ---------------------------------------------------------------------------
# Updates router
# ---------------------------------------------------------------------------


def test_version_endpoint_reads_json_version_state(test_client, tmp_path, monkeypatch):
    """GET /api/version should parse JSON .version contract."""
    import routers.updates as updates_router

    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / ".version").write_text(
        json.dumps(
            {
                "version": "2.0.0",
                "last_check": "2026-03-17T00:00:00Z",
                "last_update": "2026-03-16T00:00:00Z",
            }
        )
    )

    monkeypatch.setattr(updates_router, "INSTALL_DIR", str(install_dir))
    monkeypatch.setattr(updates_router, "_resolve_update_script", lambda: None)
    monkeypatch.setattr(
        updates_router,
        "_fetch_latest_release",
        lambda: {"latest": "2.1.0", "changelog_url": "https://example.com/release"},
    )

    resp = test_client.get("/api/version", headers=test_client.auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["current"] == "2.0.0"
    assert data["latest"] == "2.1.0"
    assert data["update_available"] is True
    assert data["last_check"] == "2026-03-17T00:00:00Z"


def test_version_endpoint_reads_legacy_plain_text(test_client, tmp_path, monkeypatch):
    """GET /api/version should support legacy plain-text .version files."""
    import routers.updates as updates_router

    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / ".version").write_text("1.9.0\n")

    monkeypatch.setattr(updates_router, "INSTALL_DIR", str(install_dir))
    monkeypatch.setattr(updates_router, "_resolve_update_script", lambda: None)
    monkeypatch.setattr(
        updates_router,
        "_fetch_latest_release",
        lambda: {"latest": "1.9.0", "changelog_url": "https://example.com/release"},
    )

    resp = test_client.get("/api/version", headers=test_client.auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["current"] == "1.9.0"
    assert data["update_available"] is False
    assert data["status"] == "up_to_date"


def test_update_readiness_has_expected_sections(test_client, monkeypatch):
    """GET /api/update/readiness should include update, compatibility, rollback sections."""
    import routers.updates as updates_router

    monkeypatch.setattr(
        updates_router,
        "_build_version_info",
        lambda: {
            "current": "2.0.0",
            "latest": "2.1.0",
            "update_available": True,
            "status": "update_available",
            "changelog_url": "https://example.com/release",
            "checked_at": "2026-03-17T00:00:00Z",
            "last_check": "2026-03-17T00:00:00Z",
            "last_update": "2026-03-16T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        updates_router,
        "_check_compatibility",
        lambda: {
            "available": True,
            "ok": True,
            "checked_at": "2026-03-17T00:00:00Z",
            "details": "[PASS] compatibility",
        },
    )
    monkeypatch.setattr(
        updates_router,
        "_collect_backup_state",
        lambda: {
            "backup_dir": "/tmp/backups",
            "backup_count": 2,
            "latest_backup": "backup-20260317-010101",
            "available": True,
        },
    )
    monkeypatch.setattr(updates_router, "_resolve_update_script", lambda: Path("/tmp/dream-update.sh"))

    resp = test_client.get("/api/update/readiness", headers=test_client.auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["update_system"]["available"] is True
    assert data["compatibility"]["ok"] is True
    assert data["rollback"]["backup_count"] == 2


def test_update_check_works_without_script(test_client, monkeypatch):
    """POST /api/update action=check should work even when update script is unavailable."""
    import routers.updates as updates_router

    monkeypatch.setattr(updates_router, "_resolve_update_script", lambda: None)
    monkeypatch.setattr(
        updates_router,
        "_build_version_info",
        lambda: {
            "current": "2.0.0",
            "latest": "2.0.0",
            "update_available": False,
            "status": "up_to_date",
            "checked_at": "2026-03-17T00:00:00Z",
        },
    )

    resp = test_client.post(
        "/api/update",
        json={"action": "check"},
        headers=test_client.auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["status"] == "up_to_date"


def test_update_backup_requires_script(test_client, monkeypatch):
    """POST /api/update action=backup returns 501 when update script is missing."""
    import routers.updates as updates_router

    monkeypatch.setattr(updates_router, "_resolve_update_script", lambda: None)
    resp = test_client.post(
        "/api/update",
        json={"action": "backup"},
        headers=test_client.auth_headers,
    )
    assert resp.status_code == 501


def test_version_endpoint_parses_multiline_script_json(test_client, tmp_path, monkeypatch):
    """GET /api/version should parse pretty-printed JSON from dream-update.sh --json."""
    import routers.updates as updates_router

    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / ".version").write_text(json.dumps({"version": "2.0.0"}))

    script_path = install_dir / "dream-update.sh"
    script_path.write_text("#!/bin/sh\n")

    monkeypatch.setattr(updates_router, "INSTALL_DIR", str(install_dir))
    monkeypatch.setattr(updates_router, "_resolve_update_script", lambda: script_path)

    fake_result = MagicMock()
    fake_result.returncode = 2
    fake_result.stdout = """{
  "success": true,
  "current": "2.0.0",
  "latest": "2.1.0",
  "update_available": true,
  "status": "update_available",
  "checked_at": "2026-03-17T00:00:00Z"
}
"""
    fake_result.stderr = ""

    with patch("routers.updates.subprocess.run", return_value=fake_result):
        resp = test_client.get("/api/version", headers=test_client.auth_headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["current"] == "2.0.0"
    assert data["latest"] == "2.1.0"
    assert data["update_available"] is True
    assert data["status"] == "update_available"


def test_update_rollback_passes_backup_id(test_client, monkeypatch):
    """POST /api/update action=rollback should pass backup_id to script invocation."""
    import routers.updates as updates_router

    script_path = Path("/tmp/dream-update.sh")
    monkeypatch.setattr(updates_router, "_resolve_update_script", lambda: script_path)

    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = "ok"
    fake_result.stderr = ""

    with patch("routers.updates.subprocess.run", return_value=fake_result) as mock_run:
        resp = test_client.post(
            "/api/update",
            json={"action": "rollback", "backup_id": "backup-20260317-010101"},
            headers=test_client.auth_headers,
        )

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    called_args = mock_run.call_args[0][0]
    assert called_args[-1] == "backup-20260317-010101"
