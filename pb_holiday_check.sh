#!/bin/bash
# Daily holiday checker — runs at 23:59 every night.
# If the date 8 days from now is in pickleball_holidays.json,
# books 17:30-18:30 (2 slots) using Khyati as primary.
#
# Called by: com.ab.pickleball.holidays launchd plist

set -u

WORK_DIR="/Users/amit.b/club"
LOG_DIR="$WORK_DIR/pickleball_logs"
PYTHON_BIN="/opt/homebrew/bin/python3"
HOLIDAYS_FILE="$WORK_DIR/pickleball_holidays.json"

mkdir -p "$LOG_DIR"

# Compute target date (today + 8 days, in IST explicitly so a Mac clock
# in a different timezone cannot shift the date and pick the wrong holiday).
TARGET_DATE=$(TZ=Asia/Kolkata "$PYTHON_BIN" -c "from datetime import date, timedelta; print((date.today() + timedelta(days=8)).isoformat())")
LOG_FILE="$LOG_DIR/launchd_pb_holiday_${TARGET_DATE}.log"

# Check if target date is a holiday
IS_HOLIDAY=$("$PYTHON_BIN" -c "
import json, sys
holidays = json.load(open('$HOLIDAYS_FILE'))['holidays']
target = '$TARGET_DATE'
match = [h for h in holidays if h['date'] == target]
if match:
    print(match[0]['name'])
else:
    print('')
")

if [[ -z "$IS_HOLIDAY" ]]; then
    # Not a holiday — silent exit, no log spam
    exit 0
fi

echo "=== $(date '+%Y-%m-%d %H:%M:%S') HOLIDAY DETECTED: $IS_HOLIDAY on $TARGET_DATE ===" >> "$LOG_FILE"

cd "$WORK_DIR" || {
    echo "FATAL: could not cd $WORK_DIR" >> "$LOG_FILE"
    exit 1
}

# Book 17:30-18:30 (2 slots) with Khyati as primary, Amit and Zaheer as fallbacks.
# Pass the wrapper-computed TARGET_DATE explicitly so the script books the
# exact same date we confirmed is a holiday.
"$PYTHON_BIN" "$WORK_DIR/book_pickleball_api.py" \
    --account khyati \
    --date "$TARGET_DATE" \
    --slots 17:30 18:00 \
    --court 3 \
    --fallback-player amit \
    --fallback-account amit zaheer annika \
    --retries 8 \
    --retry-gap 30 \
    --confirm \
    >> "$LOG_FILE" 2>&1

RC=$?
echo "=== $(date '+%Y-%m-%d %H:%M:%S') holiday booking exited rc=$RC ===" >> "$LOG_FILE"
exit $RC
