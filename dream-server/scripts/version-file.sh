#!/usr/bin/env bash
# Shared helpers for reading/writing Dream Server .version state.
#
# Backward compatible with:
# - legacy plain-text format: "2.0.0"
# - JSON object format: {"version":"2.0.0","last_check":"...","last_update":"..."}

_version_file_python() {
    python3 - "$@"
}

# Return one field from .version. Supports legacy plain text.
# Usage: version_file_get_field <file> <field> [default]
version_file_get_field() {
    local version_file="$1"
    local field="$2"
    local default_value="${3:-}"

    _version_file_python "$version_file" "$field" "$default_value" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
field = sys.argv[2]
default = sys.argv[3]

if not path.exists():
    print(default)
    raise SystemExit(0)

raw = path.read_text(encoding="utf-8").strip()
if not raw:
    print(default)
    raise SystemExit(0)

data = None
try:
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        data = parsed
except Exception:
    data = None

if data is not None:
    value = data.get(field, default)
    if value is None:
        value = default
    print(str(value))
    raise SystemExit(0)

# Legacy plain-text fallback only supports "version".
if field == "version":
    print(raw.splitlines()[0].strip() or default)
else:
    print(default)
PY
}

# Convenience wrapper for reading current version.
# Usage: version_file_get_current <file> [default]
version_file_get_current() {
    local version_file="$1"
    local default_value="${2:-0.0.0}"
    version_file_get_field "$version_file" "version" "$default_value"
}

# Upsert key=value fields and persist canonical JSON format.
# Usage: version_file_upsert_fields <file> key=value [key=value...]
version_file_upsert_fields() {
    local version_file="$1"
    shift || true

    _version_file_python "$version_file" "$@" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
pairs = sys.argv[2:]

data = {}
if path.exists():
    raw = path.read_text(encoding="utf-8").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            # Legacy plain text version file.
            first_line = raw.splitlines()[0].strip()
            if first_line:
                data = {"version": first_line}

for pair in pairs:
    if "=" not in pair:
        continue
    key, value = pair.split("=", 1)
    data[key] = value

if not str(data.get("version", "")).strip():
    data["version"] = "0.0.0"

path.parent.mkdir(parents=True, exist_ok=True)
fd, tmp_name = tempfile.mkstemp(
    prefix=".version.tmp.",
    dir=str(path.parent),
    text=True,
)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2, sort_keys=True)
    handle.write("\n")

os.replace(tmp_name, path)
PY
}
