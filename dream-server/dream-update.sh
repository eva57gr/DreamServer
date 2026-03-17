#!/bin/bash
# dream-update.sh - Dream Server Update Manager
#
# Commands:
#   check      - Check for updates against GitHub releases
#   status     - Show current version, install path, last check
#   backup     - Backup compose files, .env, and version state
#   update     - Pull new version, run migrations, restart services
#   rollback   - Restore from last backup
#   changelog  - Show version changelog
#   health     - Run health checks on all services

set -euo pipefail

#==============================================================================
# CONFIGURATION
#==============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="${SCRIPT_DIR}"
VERSION_FILE="${INSTALL_DIR}/.version"
BACKUP_DIR="${HOME}/.dream-server/backups"
MAX_BACKUPS="${MAX_BACKUPS:-10}"
UPDATE_CHANNEL="${UPDATE_CHANNEL:-stable}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"
GITHUB_REPO="${GITHUB_REPO:-Light-Heart-Labs/DreamServer}"
VERSION_LIB="${INSTALL_DIR}/scripts/version-file.sh"

if [[ ! -f "$VERSION_LIB" ]]; then
    echo "Error: missing version helper at ${VERSION_LIB}" >&2
    exit 1
fi
# shellcheck source=/dev/null
. "$VERSION_LIB"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Prerequisites check
command -v jq >/dev/null 2>&1 || { echo -e "${RED}Error: jq is required but not installed.${NC}" >&2; echo "Install with: apt install jq (Debian/Ubuntu) or brew install jq (macOS)" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo -e "${RED}Error: curl is required but not installed.${NC}" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo -e "${RED}Error: python3 is required but not installed.${NC}" >&2; exit 1; }

#==============================================================================
# HELPER FUNCTIONS
#==============================================================================

log_info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

get_current_version() {
    version_file_get_current "$VERSION_FILE" "0.0.0"
}

# Convert version string to "major minor patch" numeric triplet.
_semver_triplet() {
    local raw="${1#v}"
    if [[ "$raw" =~ ^([0-9]+)(\.([0-9]+))?(\.([0-9]+))? ]]; then
        echo "${BASH_REMATCH[1]} ${BASH_REMATCH[3]:-0} ${BASH_REMATCH[5]:-0}"
    else
        # Non-semver strings (e.g. git hash) are treated as 0.0.0
        echo "0 0 0"
    fi
}

# Semver compare: returns 0 if equal, 1 if v1 > v2, 2 if v1 < v2.
semver_compare() {
    local a_major a_minor a_patch
    local b_major b_minor b_patch
    read -r a_major a_minor a_patch <<< "$(_semver_triplet "$1")"
    read -r b_major b_minor b_patch <<< "$(_semver_triplet "$2")"

    if ((a_major == b_major && a_minor == b_minor && a_patch == b_patch)); then
        return 0
    fi

    if ((a_major > b_major)); then
        return 1
    elif ((a_major < b_major)); then
        return 2
    fi

    if ((a_minor > b_minor)); then
        return 1
    elif ((a_minor < b_minor)); then
        return 2
    fi

    if ((a_patch > b_patch)); then
        return 1
    fi
    return 2
}

emit_check_json() {
    local success="$1"
    local current_version="$2"
    local latest_version="$3"
    local update_available="$4"
    local status="$5"
    local checked_at="$6"
    local changelog_url="$7"
    local error_message="$8"

    jq -n \
        --arg current_version "$current_version" \
        --arg latest_version "$latest_version" \
        --arg status "$status" \
        --arg checked_at "$checked_at" \
        --arg changelog_url "$changelog_url" \
        --arg error_message "$error_message" \
        --argjson success "$success" \
        --argjson update_available "$update_available" \
        '{
            success: $success,
            current_version: $current_version,
            latest_version: (if $latest_version == "" then null else $latest_version end),
            update_available: $update_available,
            status: $status,
            checked_at: $checked_at,
            changelog_url: (if $changelog_url == "" then null else $changelog_url end),
            error: (if $error_message == "" then null else $error_message end)
        }'
}

#==============================================================================
# COMMAND: CHECK
#==============================================================================

cmd_check() {
    local json_mode="false"
    if [[ "${1:-}" == "--json" ]]; then
        json_mode="true"
    elif [[ -n "${1:-}" ]]; then
        log_error "Unknown option for check: $1"
        return 1
    fi

    local current_version latest_version changelog_url checked_at
    local update_available="false"
    local status="up_to_date"
    local response
    local api_url="https://api.github.com/repos/${GITHUB_REPO}/releases/latest"
    local curl_args=(-sf --max-time 15)
    local cmp_result=0

    current_version=$(get_current_version)
    latest_version=""
    changelog_url=""
    checked_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    if [[ "$json_mode" != "true" ]]; then
        log_info "Checking for updates..."
        log_info "Current version: ${current_version}"
    fi

    if [[ -n "${GITHUB_TOKEN:-}" ]]; then
        curl_args+=(-H "Authorization: Bearer ${GITHUB_TOKEN}")
    fi

    if ! response=$(curl "${curl_args[@]}" "${api_url}" 2>/dev/null); then
        status="error"
        version_file_upsert_fields "$VERSION_FILE" "last_check=$checked_at"
        if [[ "$json_mode" == "true" ]]; then
            emit_check_json "false" "$current_version" "" "false" "$status" "$checked_at" "" "Failed to check GitHub releases"
        else
            log_error "Failed to check for updates. Check network or GITHUB_TOKEN."
        fi
        return 1
    fi

    latest_version="$(echo "$response" | jq -r '.tag_name // empty' | sed 's/^v//')"
    changelog_url="$(echo "$response" | jq -r '.html_url // empty')"

    if [[ -z "$latest_version" ]]; then
        status="no_release"
        if [[ "$json_mode" != "true" ]]; then
            log_warn "No releases found on GitHub. You may be on a development version."
        fi
    else
        if [[ "$json_mode" != "true" ]]; then
            log_info "Latest version: ${latest_version}"
        fi

        set +e
        semver_compare "$current_version" "$latest_version"
        cmp_result=$?
        set -e

        case "$cmp_result" in
            0)
                status="up_to_date"
                [[ "$json_mode" != "true" ]] && log_ok "You are on the latest version."
                ;;
            1)
                status="ahead"
                [[ "$json_mode" != "true" ]] && log_warn "You are ahead of the latest release (development version)."
                ;;
            2)
                status="update_available"
                update_available="true"
                if [[ "$json_mode" != "true" ]]; then
                    log_info "Update available: ${current_version} → ${latest_version}"
                    echo ""
                    echo "Run 'dream-update.sh update' to update."
                fi
                ;;
        esac
    fi

    version_file_upsert_fields "$VERSION_FILE" "last_check=$checked_at"

    if [[ "$json_mode" == "true" ]]; then
        emit_check_json "true" "$current_version" "$latest_version" "$update_available" "$status" "$checked_at" "$changelog_url" ""
    fi

    if [[ "$update_available" == "true" ]]; then
        return 2
    fi
    return 0
}

#==============================================================================
# COMMAND: STATUS
#==============================================================================

cmd_status() {
    echo "Dream Server Status"
    echo "==================="
    echo ""
    echo "Version:        $(get_current_version)"
    echo "Install path:   ${INSTALL_DIR}"
    echo "Backup path:    ${BACKUP_DIR}"
    echo "Update channel: ${UPDATE_CHANNEL}"
    echo ""
    
    local last_check
    last_check="$(version_file_get_field "$VERSION_FILE" "last_check" "never")"
    local last_update
    last_update="$(version_file_get_field "$VERSION_FILE" "last_update" "never")"
    echo "Last check:     ${last_check}"
    echo "Last update:    ${last_update}"
    
    echo ""
    
    # Count backups
    if [[ -d "$BACKUP_DIR" ]]; then
        local backup_count
        backup_count=$(find "$BACKUP_DIR" -maxdepth 1 -type d -name "backup-*" 2>/dev/null | wc -l)
        echo "Backups:        ${backup_count} (max: ${MAX_BACKUPS})"
    else
        echo "Backups:        0 (max: ${MAX_BACKUPS})"
    fi
}

#==============================================================================
# COMMAND: BACKUP
#==============================================================================

cmd_backup() {
    local backup_name="${1:-}"
    local timestamp
    timestamp=$(date +%Y%m%d-%H%M%S)
    local backup_id="backup-${timestamp}"
    
    if [[ -n "$backup_name" ]]; then
        backup_id="backup-${backup_name}-${timestamp}"
    fi
    
    local backup_path="${BACKUP_DIR}/${backup_id}"
    
    log_info "Creating backup: ${backup_id}"
    
    mkdir -p "$backup_path"
    
    # Backup compose files
    local files_backed_up=0
    for pattern in "docker-compose*.yml" "docker-compose*.yaml" ".env" ".env.*"; do
        for file in ${INSTALL_DIR}/${pattern}; do
            if [[ -f "$file" ]]; then
                cp "$file" "$backup_path/"
                ((files_backed_up++))
            fi
        done
    done
    
    # Backup version file
    if [[ -f "$VERSION_FILE" ]]; then
        cp "$VERSION_FILE" "$backup_path/.version"
        ((files_backed_up++))
    fi
    
    # Generate metadata (use jq for safe JSON construction)
    jq -n \
        --arg bid "$backup_id" \
        --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --arg ver "$(get_current_version)" \
        --argjson fc "$files_backed_up" \
        --arg dir "$INSTALL_DIR" \
        '{backup_id: $bid, timestamp: $ts, version: $ver, files_count: $fc, install_dir: $dir}' \
        > "$backup_path/metadata.json"
    
    log_ok "Backup created: ${backup_path}"
    log_info "Files backed up: ${files_backed_up}"
    
    # Cleanup old backups
    local backup_dirs
    backup_dirs=$(find "$BACKUP_DIR" -maxdepth 1 -type d -name "backup-*" | sort -r)
    local count=0
    for dir in $backup_dirs; do
        ((count++))
        if ((count > MAX_BACKUPS)); then
            log_info "Removing old backup: $(basename "$dir")"
            rm -rf "$dir"
        fi
    done
}

#==============================================================================
# COMMAND: UPDATE
#==============================================================================

cmd_update() {
    log_info "Starting Dream Server update..."
    
    local current_version
    current_version=$(get_current_version)
    
    # Create pre-update backup
    log_info "Creating pre-update backup..."
    cmd_backup "pre-update-${current_version}"
    
    # Pull latest changes
    log_info "Pulling latest changes..."
    if [[ -d "${INSTALL_DIR}/.git" ]]; then
        cd "$INSTALL_DIR"
        git fetch origin
        git pull origin main || git pull origin master
    else
        log_error "Not a git repository. Manual update required."
        return 1
    fi
    
    # Run migrations if present
    local migrations_dir="${INSTALL_DIR}/migrations"
    if [[ -d "$migrations_dir" ]]; then
        log_info "Running migrations..."
        for migration in "$migrations_dir"/migrate-v*.sh; do
            if [[ -f "$migration" && -x "$migration" ]]; then
                log_info "Running: $(basename "$migration")"
                if ! bash "$migration"; then
                    log_error "Migration failed: $(basename "$migration")"
                    log_warn "Rolling back..."
                    cmd_rollback
                    return 1
                fi
            fi
        done
    fi
    
    # Restart services
    log_info "Restarting services..."
    local compose_flags
    if [[ -x "${INSTALL_DIR}/scripts/resolve-compose-stack.sh" ]]; then
        compose_flags=$(bash "${INSTALL_DIR}/scripts/resolve-compose-stack.sh" --script-dir "$INSTALL_DIR" 2>/dev/null | tail -1)
    fi
    # Validate that every -f target exists before using compose_flags
    if [[ -n "${compose_flags:-}" ]]; then
        local all_exist=true
        for flag_file in $(echo "$compose_flags" | grep -o -- '-f [^ ]*' | cut -d' ' -f2); do
            if [[ ! -f "${INSTALL_DIR}/${flag_file}" ]]; then
                log_warn "Compose file not found: ${flag_file} — falling back to docker-compose.yml"
                all_exist=false
                break
            fi
        done
        if [[ "$all_exist" != "true" ]]; then
            compose_flags=""
        fi
    fi
    if [[ -n "${compose_flags:-}" ]]; then
        cd "$INSTALL_DIR"
        docker compose $compose_flags down --remove-orphans 2>/dev/null || docker-compose $compose_flags down --remove-orphans
        docker compose $compose_flags up -d 2>/dev/null || docker-compose $compose_flags up -d
    elif [[ -f "${INSTALL_DIR}/docker-compose.yml" ]]; then
        cd "$INSTALL_DIR"
        docker compose down --remove-orphans 2>/dev/null || docker-compose down --remove-orphans
        docker compose up -d 2>/dev/null || docker-compose up -d
    else
        log_warn "No compose files found. Skipping container restart."
    fi
    
    # Run health checks
    log_info "Running health checks..."
    if ! cmd_health; then
        log_error "Health checks failed after update!"
        log_warn "Rolling back to previous version..."
        cmd_rollback
        return 1
    fi
    
    # Update version file
    local new_version
    new_version=$(git describe --tags 2>/dev/null || git rev-parse --short HEAD)
    version_file_upsert_fields \
        "$VERSION_FILE" \
        "version=$new_version" \
        "last_update=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    
    log_ok "Update complete! Version: ${new_version}"
}

#==============================================================================
# COMMAND: ROLLBACK
#==============================================================================

cmd_rollback() {
    local backup_id="${1:-}"
    local backup_path=""
    
    if [[ -n "$backup_id" ]]; then
        # Use specified backup
        backup_path="${BACKUP_DIR}/${backup_id}"
        if [[ ! -d "$backup_path" ]]; then
            # Try with backup- prefix
            backup_path="${BACKUP_DIR}/backup-${backup_id}"
        fi
    else
        # Find latest backup
        backup_path=$(find "$BACKUP_DIR" -maxdepth 1 -type d -name "backup-*" | sort -r | head -1)
    fi
    
    if [[ -z "$backup_path" || ! -d "$backup_path" ]]; then
        log_error "No backup found to restore from."
        echo ""
        echo "Available backups:"
        ls -1 "$BACKUP_DIR" 2>/dev/null || echo "  (none)"
        return 1
    fi
    
    log_info "Rolling back from: $(basename "$backup_path")"
    
    # Show backup metadata
    if [[ -f "$backup_path/metadata.json" ]]; then
        local backup_version
        backup_version=$(jq -r '.version // "unknown"' "$backup_path/metadata.json")
        local backup_time
        backup_time=$(jq -r '.timestamp // "unknown"' "$backup_path/metadata.json")
        log_info "Backup version: ${backup_version}"
        log_info "Backup time: ${backup_time}"
    fi
    
    # Stop services
    log_info "Stopping services..."
    cd "$INSTALL_DIR"
    docker compose down 2>/dev/null || docker-compose down 2>/dev/null || true
    
    # Restore files (enable dotglob to include .env, .version, etc.)
    log_info "Restoring configuration files..."
    shopt -s dotglob
    for file in "$backup_path"/*; do
        if [[ -f "$file" && "$(basename "$file")" != "metadata.json" ]]; then
            cp "$file" "$INSTALL_DIR/"
            log_info "  Restored: $(basename "$file")"
        fi
    done
    shopt -u dotglob
    
    # Restart services
    log_info "Restarting services..."
    docker compose up -d 2>/dev/null || docker-compose up -d
    
    # Verify health
    log_info "Verifying health..."
    sleep 10  # Give services time to start
    if cmd_health; then
        log_ok "Rollback complete!"
    else
        log_warn "Rollback complete but health checks failed. Manual intervention may be required."
        return 1
    fi
}

#==============================================================================
# COMMAND: CHANGELOG
#==============================================================================

cmd_changelog() {
    local version="${1:-}"
    
    if [[ -n "$version" ]]; then
        # Fetch specific version from GitHub
        log_info "Fetching changelog for version ${version}..."
        local api_url="https://api.github.com/repos/${GITHUB_REPO}/releases/tags/${version}"
        local response
        if response=$(curl -sf --max-time 15 "${api_url}" 2>/dev/null); then
            echo "$response" | jq -r '.body // "No changelog available."'
        else
            log_error "Could not fetch changelog for ${version}"
            return 1
        fi
    else
        # Show local CHANGELOG.md
        local changelog_file="${INSTALL_DIR}/CHANGELOG.md"
        if [[ -f "$changelog_file" ]]; then
            # Show first 50 lines (most recent entries)
            head -50 "$changelog_file"
        else
            log_warn "No local CHANGELOG.md found."
            log_info "Fetching latest release notes from GitHub..."
            cmd_changelog "$(curl -sf --max-time 15 "https://api.github.com/repos/${GITHUB_REPO}/releases/latest" | jq -r '.tag_name // empty')" || true
        fi
    fi
}

#==============================================================================
# COMMAND: HEALTH
#==============================================================================

cmd_health() {
    log_info "Running health checks..."
    local all_healthy=true
    local timeout_start=$SECONDS
    
    # Check Docker is running
    if ! docker info &>/dev/null; then
        log_error "Docker is not running"
        return 1
    fi
    log_ok "Docker is running"
    
    # Check containers
    cd "$INSTALL_DIR"
    local compose_cmd="docker compose"
    if ! $compose_cmd version &>/dev/null; then
        compose_cmd="docker-compose"
    fi
    
    local services
    services=$($compose_cmd ps --services 2>/dev/null || echo "")
    
    if [[ -z "$services" ]]; then
        log_warn "No services defined in docker-compose"
        return 0
    fi
    
    for service in $services; do
        local status
        status=$($compose_cmd ps --format json "$service" 2>/dev/null | jq -r '.[0].State // .State // "unknown"' 2>/dev/null || echo "unknown")
        
        if [[ "$status" == "running" ]]; then
            log_ok "Service ${service}: running"
        else
            log_error "Service ${service}: ${status}"
            all_healthy=false
        fi
    done
    
    # Check dashboard API health endpoint
    local dashboard_api_port="${DASHBOARD_API_PORT:-3002}"
    if curl -sf "http://localhost:${dashboard_api_port}/health" &>/dev/null; then
        log_ok "Dashboard API: healthy"
    elif curl -sf "http://localhost:${dashboard_api_port}/api/status" &>/dev/null; then
        log_ok "Dashboard API: responding"
    else
        log_warn "Dashboard API: not responding on port ${dashboard_api_port}"
    fi
    
    # Check llama-server health
    local llama_server_port="${OLLAMA_PORT:-${LLAMA_SERVER_PORT:-8080}}"
    if curl -sf "http://localhost:${llama_server_port}/v1/models" &>/dev/null; then
        log_ok "llama-server: healthy"
    else
        log_warn "llama-server: not responding on port ${llama_server_port}"
    fi
    
    if $all_healthy; then
        log_ok "All health checks passed"
        return 0
    else
        log_error "Some health checks failed"
        return 1
    fi
}

#==============================================================================
# USAGE
#==============================================================================

usage() {
    cat << EOF
Dream Server Update Manager

Usage: dream-update.sh <command> [options]

Commands:
  check [--json] Check for available updates (exit: 0=none, 2=available, 1=error)
  status         Show current version and update status
  backup [name]  Create backup of current configuration
  update         Perform update with auto-rollback on failure
  rollback [id]  Restore from backup (default: latest)
  changelog [v]  Show changelog (optional: specific version)
  health         Run health checks on all services

Environment Variables:
  GITHUB_TOKEN        GitHub API token (for higher rate limits)
  UPDATE_CHANNEL      stable|beta|nightly (default: stable)
  MAX_BACKUPS         Number of backups to retain (default: 10)
  HEALTH_TIMEOUT      Seconds to wait for health checks (default: 120)
  DASHBOARD_API_PORT  Dashboard API port (default: 3002)
  OLLAMA_PORT         llama-server port (default: 8080)

Examples:
  dream-update.sh check
  dream-update.sh check --json
  dream-update.sh status
  dream-update.sh backup pre-experiment
  dream-update.sh update
  dream-update.sh rollback
  dream-update.sh changelog v1.1.0
  dream-update.sh health

EOF
}

#==============================================================================
# MAIN
#==============================================================================

main() {
    local command="${1:-help}"
    shift || true

    case "$command" in
        check)
            cmd_check "$@"
            ;;
        status)
            cmd_status "$@"
            ;;
        backup)
            cmd_backup "$@"
            ;;
        update)
            cmd_update "$@"
            ;;
        rollback)
            cmd_rollback "$@"
            ;;
        changelog)
            cmd_changelog "$@"
            ;;
        health)
            cmd_health "$@"
            ;;
        help|--help|-h)
            usage
            ;;
        *)
            log_error "Unknown command: $command"
            echo ""
            usage
            exit 1
            ;;
    esac
}

main "$@"
