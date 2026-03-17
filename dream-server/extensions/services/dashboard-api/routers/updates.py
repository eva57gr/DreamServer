"""Version checking and update endpoints."""

import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from config import INSTALL_DIR
from models import VersionInfo, UpdateAction
from security import verify_api_key

logger = logging.getLogger(__name__)

router = APIRouter(tags=["updates"])


def _utc_now_iso() -> str:
    """Return UTC time in ISO-8601 format with Z suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_release_state(version_file: Path) -> dict[str, str]:
    """Read .version from either legacy plain-text or JSON object format."""
    if not version_file.exists():
        return {"version": "0.0.0"}

    try:
        raw = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return {"version": "0.0.0"}

    if not raw:
        return {"version": "0.0.0"}

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            state = {str(k): str(v) for k, v in parsed.items() if v is not None}
            if not state.get("version"):
                state["version"] = "0.0.0"
            return state
    except json.JSONDecodeError:
        pass

    return {"version": raw.splitlines()[0].strip() or "0.0.0"}


def _semver_triplet(value: str) -> tuple[int, int, int]:
    match = re.match(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", value.strip())
    if not match:
        return (0, 0, 0)
    return (
        int(match.group(1)),
        int(match.group(2) or "0"),
        int(match.group(3) or "0"),
    )


def _resolve_update_script() -> Path | None:
    candidates = [
        Path(INSTALL_DIR) / "dream-update.sh",
        Path(INSTALL_DIR) / "scripts" / "dream-update.sh",
        Path(INSTALL_DIR).parent / "dream-update.sh",
        Path(INSTALL_DIR).parent / "scripts" / "dream-update.sh",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


@router.get("/api/version", response_model=VersionInfo, dependencies=[Depends(verify_api_key)])
async def get_version():
    """Get current Dream Server version and check for updates."""
    import urllib.request
    import urllib.error

    version_file = Path(INSTALL_DIR) / ".version"
    current = _read_release_state(version_file).get("version", "0.0.0")

    result = {"current": current, "latest": None, "update_available": False, "changelog_url": None, "checked_at": _utc_now_iso()}

    try:
        req = urllib.request.Request("https://api.github.com/repos/Light-Heart-Labs/DreamServer/releases/latest", headers={"Accept": "application/vnd.github.v3+json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            latest = data.get("tag_name", "").lstrip("v")
            if latest:
                result["latest"] = latest
                result["changelog_url"] = data.get("html_url")
                result["update_available"] = _semver_triplet(latest) > _semver_triplet(current)
    except Exception:
        pass

    return result


@router.get("/api/releases/manifest", dependencies=[Depends(verify_api_key)])
async def get_release_manifest():
    """Get release manifest with version history."""
    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request("https://api.github.com/repos/Light-Heart-Labs/DreamServer/releases?per_page=5", headers={"Accept": "application/vnd.github.v3+json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            releases = json.loads(resp.read())
            return {
                "releases": [
                    {"version": r.get("tag_name", "").lstrip("v"), "date": r.get("published_at", ""), "title": r.get("name", ""), "changelog": r.get("body", "")[:500] + "..." if len(r.get("body", "")) > 500 else r.get("body", ""), "url": r.get("html_url", ""), "prerelease": r.get("prerelease", False)}
                    for r in releases
                ],
                "checked_at": _utc_now_iso()
            }
    except Exception:
        version_file = Path(INSTALL_DIR) / ".version"
        current = _read_release_state(version_file).get("version", "0.0.0")
        return {
            "releases": [{"version": current, "date": _utc_now_iso(), "title": f"Dream Server {current}", "changelog": "Release information unavailable. Check GitHub directly.", "url": "https://github.com/Light-Heart-Labs/DreamServer/releases", "prerelease": False}],
            "checked_at": _utc_now_iso(),
            "error": "Could not fetch release information"
        }


@router.post("/api/update")
async def trigger_update(action: UpdateAction, background_tasks: BackgroundTasks, api_key: str = Depends(verify_api_key)):
    """Trigger update actions via dashboard."""
    script_path = _resolve_update_script()
    if script_path is None:
        logger.error("dream-update.sh not found at %s", script_path)
        raise HTTPException(status_code=501, detail="Update system not installed.")

    if action.action == "check":
        try:
            result = subprocess.run([str(script_path), "check", "--json"], capture_output=True, text=True, timeout=30)
            payload = {
                "success": result.returncode in (0, 2),
                "update_available": result.returncode == 2,
                "output": (result.stdout or "") + (result.stderr or ""),
            }
            try:
                parsed = json.loads(result.stdout or "{}")
                if isinstance(parsed, dict):
                    payload.update({
                        "success": bool(parsed.get("success", payload["success"])),
                        "update_available": bool(parsed.get("update_available", payload["update_available"])),
                        "current_version": parsed.get("current_version"),
                        "latest_version": parsed.get("latest_version"),
                        "status": parsed.get("status"),
                        "checked_at": parsed.get("checked_at"),
                        "changelog_url": parsed.get("changelog_url"),
                        "error": parsed.get("error"),
                    })
            except json.JSONDecodeError:
                logger.warning("Unable to parse JSON from dream-update check output")
            return payload
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="Update check timed out")
        except Exception:
            logger.exception("Update check failed")
            raise HTTPException(status_code=500, detail="Check failed")
    elif action.action == "backup":
        try:
            result = subprocess.run([str(script_path), "backup", f"dashboard-{datetime.now().strftime('%Y%m%d-%H%M%S')}"], capture_output=True, text=True, timeout=60)
            return {"success": result.returncode == 0, "output": result.stdout + result.stderr}
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="Backup timed out")
        except Exception:
            logger.exception("Backup failed")
            raise HTTPException(status_code=500, detail="Backup failed")
    elif action.action == "update":
        def run_update():
            subprocess.run([str(script_path), "update"], capture_output=True)
        background_tasks.add_task(run_update)
        return {"success": True, "message": "Update started in background. Check logs for progress."}
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action.action}")
