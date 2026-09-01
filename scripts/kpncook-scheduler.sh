#!/usr/bin/env bash
#
# kptncook-scheduler.sh  (v2)
#
# Long-running loop:
#   - every day  at DAILY_TIME              -> `kptncook sync` + favorites backup
#   - every week on WEEKLY_DAY WEEKLY_TIME  -> full sweep via SYNC_SCRIPT
#
# Resolves the kptncook CLI from PATH, a `nix develop` shell, a local Nix flake,
# or uv, so it works the same under Nix and Docker.
#
# Env:
#   MEALIE_URL, MEALIE_API_TOKEN   (required)
#   KPTNCOOK_ENV_FILE  env file to source (default: ./.env or ~/.kptncook/.env)
#   SYNC_SCRIPT      full sweep script (default: <repo>/scripts/kptncook-sync.sh)
#   KPTNCOOK_BIN     explicit path to the kptncook executable (optional)
#   KPTNCOOK_FLAKE   Nix flake ref to build kptncook from (default: repo root)
#   KPTNCOOK_ACCESS_TOKEN  enables favorites backup
#   BACKUP_FAVORITES 1/0 to force on/off (default: on when token present)
#   RUN_MAINTENANCE  1/0 Mealie housekeeping after the weekly sweep (default 1):
#                    repair-mealie, deduplicate-mealie, create-mealie-cookbooks,
#                    categorize-mealie
#   MAINTENANCE_ON_DAILY 1/0 also run housekeeping after the daily job (default 0)
#   DAILY_TIME     default "06:00"
#   WEEKLY_DAY     default "Mon"   (Mon Tue Wed Thu Fri Sat Sun)
#   WEEKLY_TIME    default "06:30"
#   TZ             e.g. "Europe/Berlin"
#   RUN_ON_START   "1" = run both jobs immediately on boot
#   KPTNCOOK_API_KEY, KPTNCOOK_HOME, KPTNCOOK_LOCALES, KPTNCOOK_TAGS
#     (forwarded to the sync script)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*"; }

# ---------- Load .env (so a bare run sees the same vars as the CLI) ----------
# The file is sourced as trusted shell (like the CLI, it may hold tokens and
# password-manager commands). Override the location with KPTNCOOK_ENV_FILE.
load_env_file() {
    local f="${KPTNCOOK_ENV_FILE:-}"
    if [[ -z "$f" ]]; then
        if [[ -n "${KPTNCOOK_HOME:-}" && -f "$KPTNCOOK_HOME/.env" ]]; then
            f="$KPTNCOOK_HOME/.env"
        elif [[ -f "$REPO_ROOT/.env" ]]; then
            f="$REPO_ROOT/.env"
        elif [[ -f "$HOME/.kptncook/.env" ]]; then
            f="$HOME/.kptncook/.env"
        fi
    fi
    [[ -n "$f" && -f "$f" ]] || return 0
    log "Loading env from $f"
    set -a
    # shellcheck disable=SC1090
    source "$f"
    set +a
}
load_env_file

: "${MEALIE_URL:?Set MEALIE_URL}"
: "${MEALIE_API_TOKEN:?Set MEALIE_API_TOKEN}"
: "${SYNC_SCRIPT:=$SCRIPT_DIR/kptncook-sync.sh}"

# Sanitize secrets (guards against the earlier 'Illegal header value' bug)
MEALIE_API_TOKEN="$(printf '%s' "$MEALIE_API_TOKEN" | tr -d '[:space:]')"
MEALIE_URL="$(printf '%s' "$MEALIE_URL" | tr -d '[:space:]')"
export MEALIE_URL MEALIE_API_TOKEN
[[ -n "${KPTNCOOK_ACCESS_TOKEN:-}" ]] && export KPTNCOOK_ACCESS_TOKEN

DAILY_TIME="${DAILY_TIME:-06:00}"
WEEKLY_DAY="${WEEKLY_DAY:-Mon}"
WEEKLY_TIME="${WEEKLY_TIME:-06:30}"
RUN_ON_START="${RUN_ON_START:-0}"
BACKUP_FAVORITES="${BACKUP_FAVORITES:-}"
RUN_MAINTENANCE="${RUN_MAINTENANCE:-1}"
MAINTENANCE_ON_DAILY="${MAINTENANCE_ON_DAILY:-0}"

# ---------- Resolve the kptncook CLI (PATH / nix develop / nix flake / uv) ----------
KC_CMD=()
resolve_cli() {
    if [[ -n "${KPTNCOOK_BIN:-}" ]]; then
        KC_CMD=("$KPTNCOOK_BIN"); return
    fi
    if command -v kptncook >/dev/null 2>&1; then
        KC_CMD=(kptncook); return
    fi
    local flake="${KPTNCOOK_FLAKE:-$REPO_ROOT}"
    if command -v nix >/dev/null 2>&1 && [[ -f "$flake/flake.nix" ]]; then
        local bin
        if bin="$(nix build --no-link --print-out-paths "${flake}#kptncook" 2>/dev/null)/bin/kptncook" \
           && [[ -x "$bin" ]]; then
            KC_CMD=("$bin"); return
        fi
    fi
    if command -v uv >/dev/null 2>&1; then
        log "kptncook CLI not found; installing via uv..."
        uv tool install kptncook >/dev/null 2>&1 || true
        export PATH="$HOME/.local/bin:$PATH"
        if command -v kptncook >/dev/null 2>&1; then KC_CMD=(kptncook); return; fi
    fi
    log "ERROR: could not find or install the kptncook CLI"
    exit 1
}
kc() { "${KC_CMD[@]}" "$@"; }

favorites_enabled() {
    case "${BACKUP_FAVORITES:-}" in
        1|true|yes) return 0 ;;
        0|false|no) return 1 ;;
        *) [[ -n "${KPTNCOOK_ACCESS_TOKEN:-}" ]] ;;
    esac
}

daily_job() {
    log "Daily job: kptncook sync (dailies + Mealie push)"
    if ! kc sync; then
        log "WARN: daily sync failed"
    fi
    if favorites_enabled; then
        log "Daily job: backing up favorites + Mealie push"
        if ! kc backup-favorites; then
            log "WARN: favorites backup failed"
        elif ! kc sync-with-mealie; then
            log "WARN: favorites Mealie push failed"
        fi
    fi
    if [[ "$MAINTENANCE_ON_DAILY" == "1" ]]; then
        maintenance_job
    fi
}

weekly_job() {
    log "Weekly job: full sweep via $SYNC_SCRIPT"
    if ! bash "$SYNC_SCRIPT"; then
        log "WARN: weekly job failed"
    fi
    maintenance_job
}

# Mealie housekeeping: repair failed imports, remove duplicates, ensure the
# cookbooks exist, then apply categories/tools and repoint cookbooks to their
# category filter. Each step is best-effort so one failure doesn't stop the rest.
maintenance_job() {
    if [[ "$RUN_MAINTENANCE" != "1" ]]; then
        log "Maintenance disabled (RUN_MAINTENANCE=0)"
        return 0
    fi
    log "Maintenance: repair failed imports"
    kc repair-mealie --force || log "WARN: repair-mealie failed"
    log "Maintenance: remove duplicate recipes"
    kc deduplicate-mealie --force || log "WARN: deduplicate-mealie failed"
    log "Maintenance: ensure cookbooks exist"
    kc create-mealie-cookbooks || log "WARN: create-mealie-cookbooks failed"
    log "Maintenance: apply categories + tools, repoint cookbooks"
    kc categorize-mealie || log "WARN: categorize-mealie failed"
}

# Graceful shutdown for container/systemd usage
trap 'log "Received termination signal, exiting."; exit 0' TERM INT

resolve_cli

log "Scheduler started."
log "  cli    : ${KC_CMD[*]}"
log "  daily  : ${DAILY_TIME} (favorites: $(favorites_enabled && echo on || echo off))"
log "  weekly : ${WEEKLY_DAY} ${WEEKLY_TIME}"
log "  maint  : $([[ "$RUN_MAINTENANCE" == "1" ]] && echo on || echo off) (on daily: $([[ "$MAINTENANCE_ON_DAILY" == "1" ]] && echo on || echo off))"
log "  TZ     : ${TZ:-system default}"

if [[ "$RUN_ON_START" == "1" ]]; then
    log "RUN_ON_START=1 -> firing both jobs now."
    daily_job
    weekly_job
fi

# Track the last minute each job fired to avoid double-runs within the same minute.
last_daily=""
last_weekly=""

while true; do
    now_day="$(date '+%a')"                 # Mon / Tue / ...
    now_hm="$(date '+%H:%M')"
    now_stamp="$(date '+%Y-%m-%d %H:%M')"

    if [[ "$now_hm" == "$DAILY_TIME" && "$last_daily" != "$now_stamp" ]]; then
        daily_job
        last_daily="$now_stamp"
    fi

    if [[ "$now_day" == "$WEEKLY_DAY" \
       && "$now_hm" == "$WEEKLY_TIME" \
       && "$last_weekly" != "$now_stamp" ]]; then
        weekly_job
        last_weekly="$now_stamp"
    fi

    # Sleep until the top of the next minute (drift-resistant).
    sleep "$(( 60 - $(date '+%S') ))"
done