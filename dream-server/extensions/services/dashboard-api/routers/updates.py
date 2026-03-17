"""Version checking and update endpoints."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from config import DATA_DIR, INSTALL_DIR, get_runtime_version, load_version_state
from models import UpdateAction, VersionInfo
from security import verify_api_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["updates"])

GITHUB_BASE = "https://api.github.com/repos/Light-Heart-Labs/DreamServer"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_semver_parts(version: str) -> tuple[int, int, int]:
    digits = [int(tok) for tok in version.replace("-", ".").split(".") if tok.isdigit()]
    digits = (digits + [0, 0, 0])[:3]
    return digits[0], digits[1], digits[2]


def _resolve_update_script() -> Path | None:
    candidates = (
        Path(INSTALL_DIR) / "dream-update.sh",
        Path(INSTALL_DIR) / "scripts" / "dream-update.sh",
        Path(INSTALL_DIR).parent / "scripts" / "dream-update.sh",
    )
    for script in candidates:
        if script.exists():
            return script
    return None


def _resolve_compat_script() -> Path | None:
    candidates = (
        Path(INSTALL_DIR) / "scripts" / "check-compatibility.sh",
        Path(INSTALL_DIR).parent / "scripts" / "check-compatibility.sh",
    )
    for script in candidates:
        if script.exists():
            return script
    return None


def _fetch_latest_release() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github.v3+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = Request(
        f"{GITHUB_BASE}/releases/latest",
        headers=headers,
    )
    with urlopen(req, timeout=5) as resp:
        payload = json.loads(resp.read())
    return {
        "latest": str(payload.get("tag_name", "")).lstrip("v"),
        "changelog_url": str(payload.get("html_url", "")),
    }


def _parse_script_json_output(raw: str) -> dict[str, Any] | None:
    payload = raw.strip()
    if not payload:
        return None

    try:
        parsed = json.loads(payload)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Tolerate wrappers/noise around JSON payload.
    start = payload.find("{")
    end = payload.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        parsed = json.loads(payload[start : end + 1])
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        return None

    return None


def _build_version_info() -> dict[str, Any]:
    version_state = load_version_state(Path(INSTALL_DIR) / ".version")
    current = str(version_state.get("version") or "0.0.0")

    result: dict[str, Any] = {
        "current": current,
        "latest": None,
        "update_available": False,
        "status": "unknown",
        "changelog_url": None,
        "checked_at": _utc_now(),
        "last_check": version_state.get("last_check"),
        "last_update": version_state.get("last_update"),
    }

    script = _resolve_update_script()
    if script is not None:
        try:
            proc = subprocess.run(
                [str(script), "check", "--json"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            parsed = _parse_script_json_output(proc.stdout or "")
            if parsed is not None:
                result["latest"] = parsed.get("latest")
                result["update_available"] = bool(parsed.get("update_available", False))
                result["status"] = parsed.get("status", "unknown")
                result["changelog_url"] = parsed.get("changelog_url")
                result["checked_at"] = parsed.get("checked_at") or result["checked_at"]

                # Refresh from persisted state if script was able to update .version.
                refreshed = load_version_state(Path(INSTALL_DIR) / ".version")
                result["current"] = str(refreshed.get("version") or result["current"])
                result["last_check"] = refreshed.get("last_check")
                result["last_update"] = refreshed.get("last_update")
                return result
        except Exception:
            logger.exception("dream-update check --json failed; falling back to direct GitHub query")

    try:
        latest_release = _fetch_latest_release()
        latest = latest_release.get("latest") or ""
        if latest:
            result["latest"] = latest
            result["changelog_url"] = latest_release.get("changelog_url") or None
            result["update_available"] = _parse_semver_parts(current) < _parse_semver_parts(latest)
            result["status"] = "update_available" if result["update_available"] else "up_to_date"
        else:
            result["status"] = "no_release"
    except (URLError, json.JSONDecodeError, TimeoutError):
        result["status"] = "check_failed"
    except Exception:
        logger.exception("Version check failed")
        result["status"] = "check_failed"

    return result


def _collect_backup_state() -> dict[str, Any]:
    candidate_dirs = [
        Path(DATA_DIR) / "backups",
        Path.home() / ".dream-server" / "backups",
    ]

    backup_dir = None
    for candidate in candidate_dirs:
        if candidate.exists():
            backup_dir = candidate
            break
    if backup_dir is None:
        backup_dir = candidate_dirs[0]

    backups = sorted(
        [d for d in backup_dir.glob("backup-*") if d.is_dir()],
        key=lambda p: p.name,
        reverse=True,
    )
    latest = backups[0].name if backups else None
    return {
        "backup_dir": str(backup_dir),
        "backup_count": len(backups),
        "latest_backup": latest,
        "available": len(backups) > 0,
    }


def _check_compatibility() -> dict[str, Any]:
    script = _resolve_compat_script()
    state = {
        "available": False,
        "ok": None,
        "checked_at": _utc_now(),
        "details": "check-compatibility.sh not found",
    }
    if script is None:
        return state

    state["available"] = True
    try:
        proc = subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        combined = ((proc.stdout or "") + (proc.stderr or "")).strip()
        state["ok"] = proc.returncode == 0
        state["details"] = combined[-4000:] if combined else None
    except subprocess.TimeoutExpired:
        state["ok"] = False
        state["details"] = "Compatibility check timed out"
    except Exception as exc:
        logger.exception("Compatibility check failed")
        state["ok"] = False
        state["details"] = f"Compatibility check failed: {exc}"
    return state


@router.get("/api/version", response_model=VersionInfo, dependencies=[Depends(verify_api_key)])
async def get_version():
    """Get current Dream Server version and check for updates."""
    return _build_version_info()


@router.get("/api/update/readiness", dependencies=[Depends(verify_api_key)])
async def get_update_readiness():
    """Return update readiness summary for dashboard Settings page."""
    version_info = _build_version_info()
    compatibility = _check_compatibility()
    backups = _collect_backup_state()
    update_script = _resolve_update_script()

    return {
        **version_info,
        "update_system": {
            "available": update_script is not None,
            "script_path": str(update_script) if update_script else None,
        },
        "compatibility": compatibility,
        "rollback": backups,
        "checked_at": _utc_now(),
    }


@router.get("/api/releases/manifest", dependencies=[Depends(verify_api_key)])
async def get_release_manifest():
    """Get release manifest with version history."""
    req = Request(
        f"{GITHUB_BASE}/releases?per_page=5",
        headers={"Accept": "application/vnd.github.v3+json"},
    )
    try:
        with urlopen(req, timeout=5) as resp:
            releases = json.loads(resp.read())
        return {
            "releases": [
                {
                    "version": str(r.get("tag_name", "")).lstrip("v"),
                    "date": r.get("published_at", ""),
                    "title": r.get("name", ""),
                    "changelog": (
                        (r.get("body", "")[:500] + "...")
                        if len(r.get("body", "")) > 500
                        else r.get("body", "")
                    ),
                    "url": r.get("html_url", ""),
                    "prerelease": r.get("prerelease", False),
                }
                for r in releases
            ],
            "checked_at": _utc_now(),
        }
    except Exception:
        current = get_runtime_version()
        return {
            "releases": [
                {
                    "version": current,
                    "date": _utc_now(),
                    "title": f"Dream Server {current}",
                    "changelog": "Release information unavailable. Check GitHub directly.",
                    "url": "https://github.com/Light-Heart-Labs/DreamServer/releases",
                    "prerelease": False,
                }
            ],
            "checked_at": _utc_now(),
            "error": "Could not fetch release information",
        }


@router.post("/api/update")
async def trigger_update(
    action: UpdateAction,
    background_tasks: BackgroundTasks,
    _api_key: str = Depends(verify_api_key),
):
    """Trigger update actions via dashboard."""
    script_path = _resolve_update_script()

    if action.action == "check":
        return {"success": True, **_build_version_info()}

    if script_path is None:
        logger.error("dream-update.sh not found for action=%s", action.action)
        raise HTTPException(
            status_code=501,
            detail="Update script is not available in this runtime. Run updates from host CLI.",
        )

    if action.action == "backup":
        try:
            result = subprocess.run(
                [str(script_path), "backup", f"dashboard-{datetime.now().strftime('%Y%m%d-%H%M%S')}"],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            return {
                "success": result.returncode == 0,
                "output": (result.stdout or "") + (result.stderr or ""),
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="Backup timed out")
        except Exception:
            logger.exception("Backup failed")
            raise HTTPException(status_code=500, detail="Backup failed")

    if action.action == "update":
        def run_update() -> None:
            try:
                subprocess.run([str(script_path), "update"], capture_output=True, text=True, timeout=3600, check=False)
            except Exception:
                logger.exception("Background update failed")

        background_tasks.add_task(run_update)
        return {"success": True, "message": "Update started in background. Check logs for progress."}

    if action.action == "rollback":
        cmd = [str(script_path), "rollback"]
        if action.backup_id:
            cmd.append(action.backup_id)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            return {
                "success": result.returncode == 0,
                "output": (result.stdout or "") + (result.stderr or ""),
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="Rollback timed out")
        except Exception:
            logger.exception("Rollback failed")
            raise HTTPException(status_code=500, detail="Rollback failed")

    raise HTTPException(status_code=400, detail=f"Unknown action: {action.action}")
