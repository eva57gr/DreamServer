#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

fail() {
  echo "[FAIL] $1"
  exit 1
}

assert_contains() {
  local file="$1"
  local pattern="$2"
  if ! rg -n -- "$pattern" "$file" >/dev/null 2>&1; then
    fail "Expected pattern not found in $file: $pattern"
  fi
}

assert_not_contains() {
  local file="$1"
  local pattern="$2"
  if rg -n -- "$pattern" "$file" >/dev/null 2>&1; then
    fail "Unexpected hardcoded pattern found in $file: $pattern"
  fi
}

echo "[contract] platform default llama host port constants"
assert_contains "installers/macos/lib/constants.sh" "^OLLAMA_PORT_DEFAULT=11434$"
assert_contains "installers/windows/lib/constants.ps1" "OLLAMA_PORT_DEFAULT = 11434"

echo "[contract] platform env generators use OLLAMA_PORT default 11434"
assert_contains "installers/macos/lib/env-generator.sh" 'OLLAMA_PORT=\$\{ollama_port\}'
assert_contains "installers/windows/lib/env-generator.ps1" 'OLLAMA_PORT=\$ollamaPort'

echo "[contract] platform compose overlays are env-driven"
assert_contains "installers/macos/docker-compose.macos.yml" 'host\.docker\.internal:\$\{OLLAMA_PORT:-11434\}'
assert_contains "installers/windows/docker-compose.windows-amd.yml" 'host\.docker\.internal:\$\{OLLAMA_PORT:-11434\}'

echo "[contract] no hardcoded localhost/host.docker.internal llama 8080 endpoints"
for f in \
  "installers/macos/install-macos.sh" \
  "installers/macos/dream-macos.sh" \
  "installers/windows/install-windows.ps1" \
  "installers/windows/dream.ps1"; do
  assert_not_contains "$f" "localhost:8080"
  assert_not_contains "$f" "host\\.docker\\.internal:8080"
done

echo "[PASS] platform port remediation contract"
