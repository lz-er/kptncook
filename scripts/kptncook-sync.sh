#!/usr/bin/env bash
#
# kptncook-sync.sh  (v4)
#
# Fetch KptnCook recipes from every public source the CLI exposes and sync them
# to Mealie. Resolves the `kptncook` CLI from PATH, a `nix develop` shell, a
# local Nix flake, or uv, so it runs the same under Nix and Docker.
#
# Per locale it sweeps:
#   - today's dailies
#   - "latest" and "recommended" discovery lists
#   - every curated & automated list from `discovery-screen`
#   - an onboarding tag sweep (diets, cuisines, ...)
#   - a popular-ingredient sweep (needs KPTNCOOK_ACCESS_TOKEN)
# Then optionally backs up account favorites and pushes everything to Mealie.
#
# Required (unless SKIP_MEALIE=1):
#   MEALIE_URL           e.g. https://mealie.example.com/api
#   MEALIE_API_TOKEN     Mealie long-lived API token
#
# Optional:
#   KPTNCOOK_ENV_FILE    env file to source (default: ./.env or ~/.kptncook/.env)
#   KPTNCOOK_BIN         explicit path to the kptncook executable
#   KPTNCOOK_FLAKE       Nix flake ref to build kptncook from (default: repo root)
#   KPTNCOOK_API_KEY     defaults to the public app key
#   KPTNCOOK_ACCESS_TOKEN  enables favorites backup + ingredient sweep
#   KPTNCOOK_HOME        defaults to ~/.kptncook
#   KPTNCOOK_LOCALES     space-separated "lang:store" pairs (default "de:de en:en")
#   KPTNCOOK_TAGS        onboarding tag list (default: broad diet/cuisine set)
#   MAX_INGREDIENTS      popular ingredients to sweep per locale (default 0/off;
#                        KptnCook's recipes-by-ingredient endpoint currently 404s)
#   BACKUP_FAVORITES     1/0 to force on/off (default: on when token present)
#   SKIP_MEALIE=1        fetch only, no Mealie push
#   SLEEP_BETWEEN=1      seconds to sleep between API calls (default 0)

set -euo pipefail

# ---------- 0. Pretty logging ----------
if [[ -t 1 ]]; then
    C_G=$'\e[32m'; C_Y=$'\e[33m'; C_B=$'\e[36m'; C_0=$'\e[0m'
else
    C_G=''; C_Y=''; C_B=''; C_0=''
fi
log()  { printf '%s==>%s %s\n' "$C_B" "$C_0" "$*"; }
step() { printf '    %s->%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '    %sWARN:%s %s\n' "$C_Y" "$C_0" "$*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---------- 0b. Load .env (so a bare run sees the same vars as the CLI) ----------
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

# ---------- 1. Validate inputs ----------
if [[ -z "${SKIP_MEALIE:-}" ]]; then
    : "${MEALIE_URL:?Set MEALIE_URL, e.g. https://mealie.example.com/api}"
    : "${MEALIE_API_TOKEN:?Set MEALIE_API_TOKEN to your Mealie API token}"
    # Sanitize secrets (strip whitespace/newlines)
    MEALIE_API_TOKEN="$(printf '%s' "$MEALIE_API_TOKEN" | tr -d '[:space:]')"
    MEALIE_URL="$(printf '%s' "$MEALIE_URL" | tr -d '[:space:]')"
    export MEALIE_URL MEALIE_API_TOKEN
fi

# ---------- 2. Defaults ----------
export KPTNCOOK_API_KEY="${KPTNCOOK_API_KEY:-6q7QNKy-oIgk-IMuWisJ-jfN7s6}"
export KPTNCOOK_HOME="${KPTNCOOK_HOME:-$HOME/.kptncook}"
[[ -n "${KPTNCOOK_ACCESS_TOKEN:-}" ]] && export KPTNCOOK_ACCESS_TOKEN

KPTNCOOK_LOCALES="${KPTNCOOK_LOCALES:-de:de}"
KPTNCOOK_TAGS="${KPTNCOOK_TAGS:-\
rt:diet_vegetarian rt:diet_vegan rt:diet_pescatarian rt:diet_flexitarian \
rt:diet_low_carb rt:diet_high_protein rt:diet_low_fat rt:diet_low_calorie \
rt:diet_gluten_free rt:diet_lactose_free rt:diet_dairy_free \
rt:diet_keto rt:diet_paleo \
rt:cuisine_italian rt:cuisine_asian rt:cuisine_mexican rt:cuisine_mediterranean \
rt:cuisine_indian rt:cuisine_thai rt:cuisine_french rt:cuisine_greek \
rt:cuisine_spanish rt:cuisine_american rt:cuisine_oriental rt:cuisine_german}"

MAX_INGREDIENTS="${MAX_INGREDIENTS:-0}"
BACKUP_FAVORITES="${BACKUP_FAVORITES:-}"
SLEEP_BETWEEN="${SLEEP_BETWEEN:-0}"

mkdir -p "$KPTNCOOK_HOME"

# ---------- 3. Resolve the kptncook CLI (PATH / nix develop / nix flake / uv) ----------
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
        log "kptncook CLI not found; installing with uv..."
        uv tool install kptncook >/dev/null 2>&1 || true
        export PATH="$HOME/.local/bin:$PATH"
        if command -v kptncook >/dev/null 2>&1; then KC_CMD=(kptncook); return; fi
    fi
    warn "could not find or install the kptncook CLI"
    warn "set KPTNCOOK_BIN, add kptncook to PATH, provide KPTNCOOK_FLAKE, or install uv"
    exit 1
}
resolve_cli
kc() { "${KC_CMD[@]}" "$@"; }

log "kptncook CLI : ${KC_CMD[*]}"
log "KPTNCOOK_HOME: $KPTNCOOK_HOME"
log "Locales      : $KPTNCOOK_LOCALES"
[[ -z "${SKIP_MEALIE:-}" ]] && log "Target Mealie: $MEALIE_URL"

# ---------- 4. Helpers ----------
TOTAL_OK=0
TOTAL_FAIL=0

throttle() { [[ "$SLEEP_BETWEEN" != "0" ]] && sleep "$SLEEP_BETWEEN" || true; }

run_step() {
    local desc="$1"; shift
    step "$desc"
    if kc "$@" --save >/dev/null 2>&1; then
        TOTAL_OK=$((TOTAL_OK + 1))
    else
        TOTAL_FAIL=$((TOTAL_FAIL + 1))
        warn "'$desc' failed, continuing."
    fi
    throttle
}

favorites_enabled() {
    case "${BACKUP_FAVORITES:-}" in
        1|true|yes) return 0 ;;
        0|false|no) return 1 ;;
        *) [[ -n "${KPTNCOOK_ACCESS_TOKEN:-}" ]] ;;
    esac
}

backup_favorites_step() {
    favorites_enabled || return 0
    log "Backing up account favorites"
    if kc backup-favorites >/dev/null 2>&1; then
        step "favorites backed up"
    else
        warn "favorites backup failed"
    fi
}

# Sweep recipes built from the most popular ingredients (needs an access token).
# Off by default: KptnCook's recipes/withIngredients endpoint currently returns
# 404, so recipes-with-ingredients cannot resolve anything. Set MAX_INGREDIENTS>0
# to re-enable if the API starts serving it again.
ingredient_sweep() {
    [[ "$MAX_INGREDIENTS" == "0" ]] && return 0
    if [[ -z "${KPTNCOOK_ACCESS_TOKEN:-}" ]]; then
        warn "skipping ingredient sweep (KPTNCOOK_ACCESS_TOKEN not set)"
        return 0
    fi
    step "enumerating popular ingredients"
    local ids
    ids="$(kc ingredients-popular 2>/dev/null \
        | sed 's/[│┃]/|/g' \
        | awk -F'|' '{ gsub(/[^0-9a-fA-F]/, "", $1); if (length($1) >= 12) print $1 }' \
        | head -n "$MAX_INGREDIENTS")" || true
    if [[ -z "$ids" ]]; then
        warn "no popular ingredients parsed"
        return 0
    fi
    local id
    while read -r id; do
        [[ -z "$id" ]] && continue
        run_step "ingredient $id" recipes-with-ingredients --ingredient-id "$id"
    done <<< "$ids"
}

sweep_locale() {
    local lang="$1" store="$2"
    export KPTNCOOK_LANG="$lang"
    export KPTNCOOK_STORE="$store"

    echo
    echo "==> Locale $lang / store $store"

    # 4a. dailies (cheap, always current)
    run_step "dailies" dailies

    # 4b. static discovery lists
    run_step "discovery: latest"      discovery-list --list-type latest
    run_step "discovery: recommended" discovery-list --list-type recommended

    # 4c. curated + automated lists from discovery-screen
    step "enumerating discovery-screen"
    local screen
    screen="$(kc discovery-screen --no-quick-search 2>/dev/null || true)"

    if [[ -n "$screen" ]]; then
        while IFS='|' read -r raw_id raw_title raw_type; do
            local id title type
            id="$(echo "${raw_id:-}"    | xargs)"
            title="$(echo "${raw_title:-}" | xargs)"
            type="$(echo "${raw_type:-}"  | xargs | tr '[:upper:]' '[:lower:]')"

            [[ -z "$id" || -z "$type" ]] && continue
            [[ ! "$id" =~ ^[A-Za-z0-9_-]+$ ]] && continue

            case "$type" in
                curated|automated)
                    run_step "$type '$title' (id=$id)" \
                        discovery-list --list-type "$type" --list-id "$id"
                    ;;
            esac
        done <<< "$screen"
    else
        warn "discovery-screen empty for locale $lang/$store"
    fi

    # 4d. onboarding tag sweep
    for tag in $KPTNCOOK_TAGS; do
        run_step "onboarding tag: $tag" onboarding --tag "$tag"
    done

    # 4e. popular-ingredient sweep
    ingredient_sweep
}

# ---------- 5. Iterate locales ----------
for pair in $KPTNCOOK_LOCALES; do
    lang="${pair%%:*}"
    store="${pair##*:}"
    [[ -z "$lang" || -z "$store" ]] && continue
    sweep_locale "$lang" "$store"
done

# ---------- 6. Favorites ----------
backup_favorites_step

# ---------- 7. Report ----------
log "Fetch summary: $TOTAL_OK ok, $TOTAL_FAIL failed"
recipe_count="$(kc list-recipes 2>/dev/null | wc -l | tr -d '[:space:]')" || true
log "Local recipes: ${recipe_count:-0} lines from list-recipes"

# ---------- 8. Push to Mealie ----------
if [[ -z "${SKIP_MEALIE:-}" ]]; then
    log "Syncing local repository to Mealie ..."
    kc sync-with-mealie
fi

log "Done."