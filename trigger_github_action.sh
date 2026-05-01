#!/bin/bash
# Trigger the Pickleball Court Booking workflow on GitHub.
# Called by cron at precise times. GitHub Actions picks up the
# workflow_dispatch within ~15 seconds, far more reliable than GitHub's
# own scheduled cron which can be delayed 15–60 minutes.
#
# Usage:  trigger_github_action.sh <day_override>
#         day_override: friday | saturday | sunday | holiday |
#                       padel-sat-c1 | padel-sat-c2 |
#                       padel-sun-c1 | padel-sun-c2
#                       (comma-separated for multiple)
#
# Requires:
#   $HOME/clubpickle/secrets/github_pat — GitHub PAT with `repo` or
#       fine-grained "Actions: Write" scope on amitbhawnani1/clubpickle.

set -u

DAY_OVERRIDE="${1:?day_override required (e.g. friday, padel-sat-c1)}"
LOG_DIR="$HOME/clubpickle/trigger_logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/trigger_$(date -u +%Y%m%d_%H%M%S)_${DAY_OVERRIDE}.log"

PAT=$(cat "$HOME/clubpickle/secrets/github_pat")
REPO="amitbhawnani1/clubpickle"
WORKFLOW="book.yml"

{
echo "=== $(date -u +"%Y-%m-%d %H:%M:%S UTC") trigger: $DAY_OVERRIDE ==="

RESPONSE=$(curl -s -w "\nHTTP_STATUS:%{http_code}\nTOTAL_TIME:%{time_total}" \
    -X POST \
    -H "Authorization: Bearer $PAT" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/repos/$REPO/actions/workflows/$WORKFLOW/dispatches" \
    -d "{\"ref\":\"main\",\"inputs\":{\"day_override\":\"$DAY_OVERRIDE\"}}")

echo "$RESPONSE"

STATUS=$(echo "$RESPONSE" | grep HTTP_STATUS | cut -d: -f2)
if [ "$STATUS" = "204" ]; then
    echo "SUCCESS: workflow dispatched"
    exit 0
else
    echo "FAILED: HTTP $STATUS"
    exit 1
fi
} >> "$LOG_FILE" 2>&1
