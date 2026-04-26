# The Club Mumbai — Court Booking (Pickleball + Padel)

Automated booking system for The Club Mumbai. Supports both:
- Pickleball courts (https://theclubmumbai.com/the-club-pickleball-game-booking/)
- Padel courts (https://theclubmumbai.com/the-club-padel-game-booking/)

Pure-HTTP — no browser, no Selenium. Reverse-engineered WordPress `admin-ajax.php` + SAP login API. Runs from three independent layers for redundancy.

The two games share the same script, accounts, and infrastructure but have **independent weekly hour limits** (5 hours each, tracked separately by the club's API).

## Architecture

```
                        ┌──────────────────────────┐
                        │  Local Mac (launchd)     │
                        │  23:59 IST, sub-second   │
                        │  fires if Mac is awake   │
                        └────────────┬─────────────┘
                                     │ books directly
                                     ▼
┌─────────────────────┐   ┌────────────────────────┐
│ Swiss EC2 (cron)    │   │                        │
│ 23:58 IST precisely │──▶│  The Club Mumbai API   │
│ triggers GH Actions │   │  (theclubmumbai.com    │
└──────────┬──────────┘   │   + octosystems.com)   │
           │ POST dispatch │                        │
           ▼               └────────────────────────┘
┌─────────────────────┐                ▲
│ GitHub Actions      │────────────────┘
│ workflow_dispatch   │   books directly
│ runs ~1-2s after    │   from US/EU runners
│ Swiss trigger fires │
└─────────────────────┘
```

Both paths end up calling `book_pickleball_api.py` with the same parameters. Double-booking is prevented by a pre-flight `club_get_my_bookings` check (if an overlapping confirmed booking already exists, the script exits 0 without probing).

## Quick Start

```bash
# Dry run
python3 book_pickleball_api.py \
    --account amit --date 2026-04-24 \
    --slots 18:00 18:30 19:00 \
    --court 3 --fallback-player khyati

# Live booking with fallback accounts + retry loop for midnight slot opening
python3 book_pickleball_api.py \
    --account amit --date auto \
    --slots 18:00 18:30 19:00 \
    --court 3 --fallback-player khyati \
    --fallback-account khyati zaheer annika \
    --confirm --retries 8 --retry-gap 30
```

**Key flags:**
- `--game` — `pickleball` (default) or `padel`
- `--account` — primary account (amit/khyati/zaheer/annika)
- `--date` — `YYYY-MM-DD` or `auto` (IST today + 8 days)
- `--slots` — space-separated 30-min slot times (e.g. `17:30 18:00 18:30`)
- `--court` — preferred court (1-3 for pickleball, 1-4 for padel); falls back in game-specific order
- `--fallback-player` — Player 2 identity
- `--fallback-account` — accounts to try if primary hits weekly limit (per-game)
- `--confirm` — actually submit (without this = dry run)
- `--retries` / `--retry-gap` — retry loop across the midnight slot-opening window
- `--allow-partial` (default) / `--no-allow-partial` — book longest contiguous available subset if full slot unavailable

## Credentials

`pickleball_accounts.json` (gitignored, local only):
```json
{
  "accounts": {
    "amit":    { "membership_no": "FM11532",    "password": "..." },
    "khyati":  { "membership_no": "FM11532 02", "password": "..." },
    "zaheer":  { "membership_no": "FM1211",     "password": "..." },
    "annika":  { "membership_no": "FM11532 03", "password": "..." }
  }
}
```

In GitHub Actions the same JSON is stored as the `PICKLEBALL_ACCOUNTS` secret and re-materialized at runtime.

## Accounts

| Account | Member No | Phone | Player 2 fallback |
|---|---|---|---|
| amit    | FM11532     | 9821098042 | khyati or zaheer |
| khyati  | FM11532 02  | 9821925606 | amit or zaheer |
| zaheer  | FM1211      | 9820527997 | amit or khyati |
| annika  | FM11532 03  | 9324959103 | last-resort fallback only |

## Schedules

Slots open at 00:00 IST exactly 7 days ahead. All three layers fire 1-2 minutes before midnight IST.

### Pickleball

| Day booked | Local launchd fires (IST) | Swiss cron fires (UTC) | Account | Slots | Duration |
|---|---|---|---|---|---|
| **Friday**   | Thu 23:59 | Thu 18:28 | amit   | 18:00, 18:30, 19:00        | 6:00–7:30 PM |
| **Saturday** | Fri 23:59 | Fri 18:28 | khyati | 17:00, 17:30, 18:00        | 5:00–6:30 PM |
| **Sunday**   | Sat 23:59 | Sat 18:28 | amit   | 17:00, 17:30, 18:00        | 5:00–6:30 PM |
| **Holidays** | Nightly 23:59 | Nightly 18:28 | khyati | 17:30, 18:00               | 5:30–6:30 PM (weekday holidays only, from `pickleball_holidays.json`) |

Pickleball account fallback chains:
- Friday: amit → khyati → zaheer → annika
- Saturday: khyati → amit → zaheer → annika
- Sunday: amit → khyati → zaheer → annika
- Holidays: khyati → amit → zaheer → annika

### Padel

| Day booked | Local launchd fires (IST) | Swiss cron fires (UTC) | Account | Time preferences |
|---|---|---|---|---|
| **Saturday** | Fri 23:59 | Fri 18:28 | khyati | priority list (see below) |
| **Sunday**   | Sat 23:59 | Sat 18:28 | khyati | priority list (see below) |

**Padel slot priority** (tried in order — first that books wins):

**Saturday** padel preferences:
1. `17:00, 17:30` — **5:00–6:00 PM** (1 hour, preferred)
2. `17:30, 18:00` — 5:30–6:30 PM (1 hour, fallback)
3. `17:00` / `17:30` / `18:00` — any 30-min slot (last resort)

**Sunday** padel preferences:
1. `17:30, 18:00` — **5:30–6:30 PM** (1 hour, preferred)
2. `17:00, 17:30` — 5:00–6:00 PM (1 hour, fallback)
3. `17:00` / `17:30` / `18:00` — any 30-min slot (last resort)

This is wired via the `--slot-pref` CLI flag (each comma-separated set is one preference; the launchd plist passes them as separate `<string>` array entries, the GitHub workflow as space-separated quoted args).

Padel court preference: **1 → 2 only** (courts 3 and 4 are intentionally excluded; the script fails rather than booking on them).
Padel fallback chain (both Sat + Sun): khyati → annika → amit → zaheer.
Player 2 default: annika.

Padel weekly hour limit (5 hr/week) is **separate** from pickleball — booking padel does not consume pickleball quota and vice versa.

## Repository Layout

```
.
├── book_pickleball_api.py         # main booking script (pure HTTP)
├── pickleball_holidays.json       # 2026 weekday public holidays
├── pickleball_accounts.json       # credentials (gitignored)
├── pb_weekly_book.sh              # launchd wrapper for Fri/Sat/Sun
├── pb_holiday_check.sh            # launchd wrapper for holiday check
├── .github/workflows/book.yml     # GitHub Actions workflow
├── pickleball_logs/               # runtime logs (gitignored)
└── requirements.txt               # httpx
```

## Scheduling Details

### Layer 1 — Local Mac (launchd)

Four plists at `~/Library/LaunchAgents/com.ab.pickleball.{friday,saturday,sunday,holidays}.plist`. They call `pb_weekly_book.sh` or `pb_holiday_check.sh` with the right parameters. The plists reference `/Users/amit.b/club` absolute paths.

```bash
# Load / unload
for d in friday saturday sunday holidays; do
  launchctl load   ~/Library/LaunchAgents/com.ab.pickleball.$d.plist
  # or: launchctl unload ~/Library/LaunchAgents/com.ab.pickleball.$d.plist
done

# Status
launchctl list | grep pickleball
```

**Sleep caveat:** launchd fires missed jobs on wake, but if the Mac is asleep at 23:59 IST the job runs late (potentially past midnight IST). The script uses IST explicitly for date calculation so a late fire still books the correct date.

### Layer 2 — Swiss EC2 (cron → GitHub workflow_dispatch)

AWS EC2 in eu-central-2 (Zurich) runs a UTC cron:

```
28 18 * * 4 /home/ubuntu/clubpickle/trigger_github_action.sh friday
28 18 * * 5 /home/ubuntu/clubpickle/trigger_github_action.sh saturday
28 18 * * 5 /home/ubuntu/clubpickle/trigger_github_action.sh padel-sat
28 18 * * 6 /home/ubuntu/clubpickle/trigger_github_action.sh sunday
28 18 * * 6 /home/ubuntu/clubpickle/trigger_github_action.sh padel-sun
28 18 * * * /home/ubuntu/clubpickle/trigger_github_action.sh holiday
```

Pickleball and padel are dispatched as **separate workflow runs** for the same time slot so they execute in parallel on independent GitHub runners. Combining them into one workflow run (as we did initially) caused padel to start ~3 minutes after midnight IST because the pickleball step's retry loop ran first sequentially — by then, popular padel slots were already grabbed by other members.

The `trigger_github_action.sh` script POSTs to GitHub's workflow_dispatch API. A GitHub PAT is stored at `~/clubpickle/secrets/github_pat` (mode 0600).

**Why Swiss is trigger-only:** octosystems.com:89 (the SAP login endpoint) blocks the Swiss server's IP, so direct booking from Switzerland fails. But GitHub API works, so Swiss acts as a precision trigger — GitHub Actions does the actual booking from its US/EU runners.

### Layer 3 — GitHub Actions

`.github/workflows/book.yml` — `workflow_dispatch` only (the delay-prone `schedule` trigger has been removed). Runs Python 3.12 on ubuntu-latest, installs httpx, writes the accounts JSON from the `PICKLEBALL_ACCOUNTS` secret, then calls `book_pickleball_api.py`. An `always()` cleanup step removes the accounts file at end of job.

Manual trigger:
```bash
gh workflow run book.yml --repo amitbhawnani1/clubpickle -f day_override=friday
```

## Booking Rules / Constraints

- **NEVER cancel existing bookings** — only create new ones
- Weekly limit: 5 hours per member per week (Mon–Sun)
- Slots open exactly 7 days in advance at 00:00:00 IST
- Court preference: 3 → 2 → 1
- Player 2: use "marker" checkbox; if it fails, fill Player 2 fields with another family member
- Account fallback: if one account hits weekly limit, try the next

## Partial-slot fallback

When some requested slots are unavailable, the script does a **two-pass search per account**:

1. **Pass 1 (full)**: probe courts 3 → 2 → 1 and book only if ALL requested slots are available.
2. **Pass 2 (partial)**: if no court has full availability, book the longest contiguous available subset.

This guarantees "full > partial" preference across courts — never settles for partial on court 3 when court 1 or 2 could have booked the full slot.

Contiguity = consecutive 30-min steps within the requested list. If there's a gap in the middle (e.g. `17:00, [17:30 booked], 18:00, 18:30`), the script books the longer contiguous side (`18:00, 18:30`).

Disable with `--no-allow-partial`.

## Double-booking prevention

Before attempting to book, the script calls `club_get_my_bookings` per account. If the account already has a **confirmed** booking on the target date with any overlapping time slots, the script logs `ALREADY BOOKED` and exits 0 — no probes, no booking attempts.

## Timeout-after-success recovery

If a `create_booking` POST succeeds on the server but the client hits a ReadTimeout, the internal HTTP retry POSTs again and sees the slots as "already booked." Previously this looked like a failure and fallback accounts/courts would be tried — sometimes creating a duplicate. The script now re-queries `club_get_my_bookings` after any "already booked" error (except P2 phone collisions) and, if a matching confirmed booking exists, treats the original POST as a success.

## Timezone handling

`--date auto` uses IST explicitly via `zoneinfo.ZoneInfo("Asia/Kolkata")` regardless of the host timezone. This prevents a late-firing cron from computing the wrong target date (bug fixed Apr 17 2026 — a 71-min-delayed GitHub scheduled run on Thursday evening UTC was Friday morning IST, which would have picked Friday's plan with a Thursday's-date + 8 target).

## Logs

All run logs go to `pickleball_logs/`:
- `pickleball_<account>_<date>.log` — per-run Python log
- `launchd_pb_<account>_<date>.log` — launchd wrapper log
- `launchd_stdout_<day>.log` / `launchd_stderr_<day>.log` — raw launchd output

On the Swiss server, trigger logs are at `~/clubpickle/trigger_logs/`.

## Observability

See current bookings across all accounts:
```bash
python3 -c "
import json, sys, logging
from datetime import date
from pathlib import Path
sys.path.insert(0, '/Users/amit.b/club')
from book_pickleball_api import Creds, BookingClient, load_accounts

accounts = load_accounts(Path('/Users/amit.b/club/pickleball_accounts.json'))
logger = logging.getLogger('show'); logging.basicConfig(level=logging.WARNING)
today = date.today().isoformat()

for name in ['amit', 'khyati', 'zaheer', 'annika']:
    cfg = accounts[name]
    creds = Creds.login(name, cfg['membership_no'], cfg['password'], logger)
    client = BookingClient(creds, logger)
    for b in client.get_my_bookings():
        if b.get('booking_status')=='confirmed' and b.get('booking_date','')>=today:
            print(f\"{b['booking_date']} {b['time_slot']:30s} court={b['court_id'][-1]} via={name} #{b['id']}\")
    client.close()
"
```
