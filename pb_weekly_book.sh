#!/bin/bash
# Recurring pickleball booking wrapper.
# Called by launchd plists at 23:59 on Thu/Fri/Sat to book slots
# that open at midnight (7 days in advance).
#
# Usage: pb_weekly_book.sh <day_label> <account> <slots...> -- <fallback_player> <fallback_accounts...>
#   pb_weekly_book.sh friday amit 18:00 18:30 19:00 -- khyati khyati zaheer
#   pb_weekly_book.sh saturday khyati 17:00 17:30 18:00 18:30 -- amit amit zaheer
#   pb_weekly_book.sh sunday zaheer 17:30 18:00 18:30 -- amit amit khyati

set -u

# Optional leading flags (must come before DAY_LABEL):
#   --game GAME       Defaults to pickleball.
#   --court N         Restrict booking to ONLY court N (no fallback). Forwarded
#                     as both --court N and --court-pref N to the Python script.
GAME="pickleball"
COURT_OVERRIDE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --game)  GAME="${2:?--game requires a value}"; shift 2 ;;
        --court) COURT_OVERRIDE="${2:?--court requires a value}"; shift 2 ;;
        *) break ;;
    esac
done

DAY_LABEL="${1:?day_label required}"
ACCOUNT="${2:?account required}"
shift 2

# Parse slots (before --) and fallback info (after --)
SLOTS=()
FALLBACK_PLAYER=""
FALLBACK_ACCOUNTS=()
PAST_SEP=false

for arg in "$@"; do
    if [[ "$arg" == "--" ]]; then
        PAST_SEP=true
        continue
    fi
    if $PAST_SEP; then
        if [[ -z "$FALLBACK_PLAYER" ]]; then
            FALLBACK_PLAYER="$arg"
        else
            FALLBACK_ACCOUNTS+=("$arg")
        fi
    else
        SLOTS+=("$arg")
    fi
done

WORK_DIR="/Users/amit.b/club"
LOG_DIR="$WORK_DIR/pickleball_logs"
PYTHON_BIN="/opt/homebrew/bin/python3"

# Compute the target date (today + 8 days, evaluated in IST explicitly
# so a Mac clock in a different timezone cannot shift the date).
TARGET_DATE=$(TZ=Asia/Kolkata "$PYTHON_BIN" -c "from datetime import date, timedelta; print((date.today() + timedelta(days=8)).isoformat())")
LOG_FILE="$LOG_DIR/launchd_${GAME}_${ACCOUNT}_${TARGET_DATE}.log"

mkdir -p "$LOG_DIR"
echo "=== $(date '+%Y-%m-%d %H:%M:%S') $GAME $DAY_LABEL wrapper starting ===" >> "$LOG_FILE"
echo "game=$GAME account=$ACCOUNT target=$TARGET_DATE slots=${SLOTS[*]} fallback_player=$FALLBACK_PLAYER fallback_accounts=${FALLBACK_ACCOUNTS[*]:-none}" >> "$LOG_FILE"

cd "$WORK_DIR" || {
    echo "FATAL: could not cd $WORK_DIR" >> "$LOG_FILE"
    exit 1
}

# Build fallback-account args
FA_ARGS=()
if [[ ${#FALLBACK_ACCOUNTS[@]} -gt 0 ]]; then
    FA_ARGS+=("--fallback-account" "${FALLBACK_ACCOUNTS[@]}")
fi

# Court selection. If --court was passed, lock to that single court (no
# fallback). Otherwise use the game-default preferred court with full fallback.
COURT_ARGS=()
if [[ -n "$COURT_OVERRIDE" ]]; then
    COURT_ARGS=("--court" "$COURT_OVERRIDE" "--court-pref" "$COURT_OVERRIDE")
else
    DEFAULT_COURT=3
    [[ "$GAME" == "padel" ]] && DEFAULT_COURT=1
    COURT_ARGS=("--court" "$DEFAULT_COURT")
fi

# Detect slot-preference mode: if any positional slot arg contains a comma,
# the slots are treated as a priority list of preferences, each comma-
# separated. Pass them via --slot-pref. Otherwise pass via --slots.
SLOT_FLAG="--slots"
for s in "${SLOTS[@]}"; do
    if [[ "$s" == *","* ]]; then
        SLOT_FLAG="--slot-pref"
        break
    fi
done

# 8 attempts × 30s = covers 23:59 to ~00:03, crossing the midnight gate.
# Pass the wrapper-computed TARGET_DATE explicitly so it cannot drift if the
# Python process happens to cross midnight IST during execution.
"$PYTHON_BIN" "$WORK_DIR/book_pickleball_api.py" \
    --game "$GAME" \
    --account "$ACCOUNT" \
    --date "$TARGET_DATE" \
    "$SLOT_FLAG" "${SLOTS[@]}" \
    "${COURT_ARGS[@]}" \
    --fallback-player "$FALLBACK_PLAYER" \
    "${FA_ARGS[@]}" \
    --retries 8 \
    --retry-gap 30 \
    --confirm \
    >> "$LOG_FILE" 2>&1

RC=$?
echo "=== $(date '+%Y-%m-%d %H:%M:%S') booking script exited rc=$RC ===" >> "$LOG_FILE"
exit $RC
