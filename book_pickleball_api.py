#!/usr/bin/env python3
"""
Pickleball Court Booking - The Club Mumbai (HTTP API approach)

Pure-HTTP booking script. No browser, no Selenium, no Claude MCP at runtime.
Logs into the club's member API fresh each run (no stored tokens needed), then
POSTs bookings directly to WordPress admin-ajax.php.

Usage:
    # Dry run (no booking submitted):
    python3 book_pickleball_api.py \\
        --account amit --date 2026-04-19 \\
        --slots 17:30 18:00 18:30 \\
        --court 3 --fallback-player khyati

    # Live booking:
    python3 book_pickleball_api.py \\
        --account amit --date 2026-04-19 \\
        --slots 17:30 18:00 18:30 \\
        --court 3 --fallback-player khyati \\
        --confirm --retries 8 --retry-gap 30

    # Auto-date (books 8 days from today — for midnight cron jobs):
    python3 book_pickleball_api.py \\
        --account amit --date auto \\
        --slots 17:30 18:00 18:30 \\
        --court 3 --fallback-player khyati \\
        --fallback-account khyati \\
        --confirm

Flags:
    --account           Primary account (amit/khyati/zaheer)
    --date              Booking date YYYY-MM-DD, or "auto" = today + 8 days
    --slots             Space-separated slot times HH:MM
    --court             Court preference (3/2/1, default 3; falls back to others)
    --fallback-player   Player 2 identity when marker checkbox can't be used
    --fallback-account  Account to try if primary's weekly limit is reached
                        (can specify multiple, e.g. --fallback-account khyati zaheer)
    --confirm           Actually submit the booking (without this = dry run)
    --retries           Max retry attempts (default 8)
    --retry-gap         Seconds between retries (default 30)
    --accounts-file     Path to pickleball_accounts.json (default: auto-detect)

Exit codes:
    0 = booked successfully (or dry run passed)
    1 = failed after retries
    2 = config / usage error

This script NEVER cancels existing bookings.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import httpx

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

BASE = "https://theclubmumbai.com"
BOOKING_PAGE = f"{BASE}/the-club-pickleball-game-booking/"
AJAX = f"{BASE}/wp-admin/admin-ajax.php"
LOGIN_URL = "https://theclubsap.octosystems.com:89/MembersSvc.asmx/Login"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# Family-member contact details for Player 2 field.
FAMILY_CONTACTS: dict[str, dict[str, str]] = {
    "amit": {
        "name": "AMIT BHAWNANI",
        "email": "AMITBHAWNANI@GMAIL.COM",
        "phone": "9821098042",
    },
    "khyati": {
        "name": "KHYATI BHAWNANI",
        "email": "KHYATI@GMAIL.COM",
        "phone": "9821925606",
    },
    "zaheer": {
        "name": "ZAHEER",
        "email": "ZAHEER@KETTO.ORG",
        "phone": "9820527997",
    },
    "annika": {
        "name": "ANNIKA BHAWNANI",
        "email": "annikabhawnani@gmail.com",
        "phone": "9324959103",
    },
}

COURT_IDS = {
    1: "pickleball_court_1",
    2: "pickleball_court_2",
    3: "pickleball_court_3",
}


# --------------------------------------------------------------------------- #
# Logging                                                                      #
# --------------------------------------------------------------------------- #

def setup_logger(log_path: Optional[Path]) -> logging.Logger:
    logger = logging.getLogger("pickleball")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


# --------------------------------------------------------------------------- #
# Login & Credentials                                                          #
# --------------------------------------------------------------------------- #

@dataclass
class Creds:
    account_name: str
    member_info_raw: str  # JSON string exactly as stored in localStorage
    parsed: dict

    @classmethod
    def login(
        cls,
        account_name: str,
        membership_no: str,
        password: str,
        logger: logging.Logger,
    ) -> "Creds":
        """Login via the club's member API and return fresh credentials."""
        logger.info(f"logging in as {account_name} ({membership_no})...")
        client = httpx.Client(
            timeout=15,
            verify=False,
            headers={"User-Agent": UA},
        )
        try:
            r = client.post(LOGIN_URL, data={
                "membercode": membership_no,
                "password": password,
            })
            r.raise_for_status()
            data = r.json()
            if not (isinstance(data, list) and len(data) > 0 and data[0].get("member_no")):
                raise RuntimeError(f"login failed: {str(data)[:300]}")
            mi = data[0]
            mi_raw = json.dumps(mi)
            logger.info(
                f"login OK: {mi.get('member_first_name')} {mi.get('member_last_name')} "
                f"phone={mi.get('member_mobile')}"
            )
            return cls(account_name=account_name, member_info_raw=mi_raw, parsed=mi)
        finally:
            client.close()


def load_accounts(path: Path) -> dict:
    """Load pickleball_accounts.json → {name: {membership_no, password}}."""
    raw = json.loads(path.read_text())
    return raw.get("accounts", raw)


# --------------------------------------------------------------------------- #
# Booking client                                                               #
# --------------------------------------------------------------------------- #

class BookingClient:
    def __init__(self, creds: Creds, logger: logging.Logger):
        self.creds = creds
        self.log = logger
        self.client = httpx.Client(
            timeout=15.0,
            follow_redirects=True,
            headers={
                "User-Agent": UA,
                "Accept": "*/*",
                "Accept-Language": "en-GB,en;q=0.9",
                "Origin": BASE,
                "Referer": BOOKING_PAGE,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        self.nonce: Optional[str] = None

    def close(self) -> None:
        self.client.close()

    def switch_account(self, creds: Creds) -> None:
        """Switch to a different account (for account fallback)."""
        self.creds = creds
        self.nonce = None  # force nonce refresh

    # -- Nonce ---------------------------------------------------------------

    def refresh_nonce(self) -> str:
        """GET the booking page and scrape the nonce + session cookies."""
        r = self.client.get(BOOKING_PAGE)
        r.raise_for_status()
        m = re.search(
            r'<input[^>]+id="nonce"[^>]+value="([^"]+)"', r.text
        )
        if not m:
            raise RuntimeError("Could not find nonce in booking page HTML")
        self.nonce = m.group(1)
        self.log.info(f"nonce refreshed: len={len(self.nonce)}")
        return self.nonce

    # -- Generic POST --------------------------------------------------------

    def _post_with_retry(self, payload: dict, max_tries: int = 3) -> httpx.Response:
        """POST with small retry loop for transient network errors."""
        last_exc: Optional[Exception] = None
        for i in range(max_tries):
            try:
                r = self.client.post(AJAX, data=payload)
                r.raise_for_status()
                return r
            except (
                httpx.RemoteProtocolError,
                httpx.ReadError,
                httpx.ConnectError,
                httpx.ReadTimeout,
            ) as e:
                last_exc = e
                self.log.info(f"post retry {i + 1}/{max_tries}: {type(e).__name__}")
                time.sleep(1.0 + i)
        raise RuntimeError(f"post failed after {max_tries} tries: {last_exc}")

    def ajax(self, action: str, **extra) -> dict:
        if not self.nonce:
            self.refresh_nonce()
        payload = {
            "action": action,
            "nonce": self.nonce,
            "member_id": self.creds.parsed["member_no"],
            "member_info": self.creds.member_info_raw,
            "user_phone": self.creds.parsed["member_mobile"],
        }
        payload.update(extra)
        r = self._post_with_retry(payload)
        try:
            return r.json()
        except Exception:
            return {"success": False, "raw": r.text}

    # -- API calls -----------------------------------------------------------

    def get_time_slots(self, date: str, court: int) -> dict:
        return self.ajax(
            "club_get_time_slots",
            date=date,
            game_type="pickleball",
            court_id=COURT_IDS[court],
            is_event_booking="no",
        )

    def get_booked_hours(self, date: str) -> dict:
        return self.ajax(
            "club_get_booked_hours",
            booking_date=date,
            game_type="pickleball",
        )

    def get_my_bookings(self) -> list[dict]:
        """Return list of this member's bookings (all dates)."""
        res = self.ajax("club_get_my_bookings")
        return (res.get("data") or {}).get("bookings", [])

    def create_booking(
        self,
        *,
        date: str,
        court: int,
        slots: list[str],
        player2: dict[str, str],
        remarks: str = "",
    ) -> dict:
        p = self.creds.parsed
        payload = {
            "action": "club_create_booking",
            "nonce": self.nonce,
            "member_id": p["member_no"],
            "member_info": self.creds.member_info_raw,
            "game_type": "pickleball",
            "court_id": COURT_IDS[court],
            "booking_date": date,
            "time_slots": json.dumps(slots),
            "booking_type": "member",
            "player_count": "2",
            "player1_name": f"{p['member_first_name']} {p['member_last_name']}",
            "player1_email": p["member_email"],
            "player1_phone": p["member_mobile"],
            "player2_name": player2["name"],
            "player2_email": player2["email"],
            "player2_phone": player2["phone"],
            "booking_remarks": remarks,
        }
        r = self._post_with_retry(payload)
        try:
            return r.json()
        except Exception:
            return {"success": False, "raw": r.text, "status": r.status_code}


# --------------------------------------------------------------------------- #
# Single booking attempt                                                       #
# --------------------------------------------------------------------------- #

def longest_contiguous_available(
    requested: list[str], available: set[str]
) -> list[str]:
    """Return the longest contiguous run of slots from `requested` that are all
    in `available`. Contiguity = consecutive 30-min steps.
    Preserves the order of `requested`."""
    def to_min(s: str) -> int:
        h, m = s.split(":")
        return int(h) * 60 + int(m)

    best: list[str] = []
    current: list[str] = []
    prev: Optional[int] = None
    for s in requested:
        if s not in available:
            if len(current) > len(best):
                best = current
            current = []
            prev = None
            continue
        cur = to_min(s)
        if prev is None or cur == prev + 30:
            current.append(s)
        else:
            if len(current) > len(best):
                best = current
            current = [s]
        prev = cur
    if len(current) > len(best):
        best = current
    return best


def try_single_attempt(
    client: BookingClient,
    *,
    date_str: str,
    court: int,
    slots: list[str],
    player2: dict[str, str],
    dry_run: bool,
    logger: logging.Logger,
    allow_partial: bool = False,
) -> tuple[bool, str]:
    """Returns (ok, message). ok=True means booking confirmed / dry run passed.

    If `allow_partial` is False and some requested slots are unavailable, this
    returns ("already-booked"). If True, it falls back to booking the longest
    contiguous available subset of the requested slots.
    """

    # Fresh nonce per attempt (safer after long waits between retries)
    try:
        client.refresh_nonce()
    except Exception as e:
        return False, f"nonce-refresh-failed: {e}"

    # Probe available slots for this court + date.
    probe = client.get_time_slots(date=date_str, court=court)
    logger.info(f"probe court={court} -> {truncate(probe)}")
    if not probe.get("success"):
        msg = (probe.get("data") or {}).get("message") if isinstance(
            probe.get("data"), dict
        ) else str(probe.get("data"))
        return False, f"slots-not-available: {msg}"

    # Parse which slots are available / booked.
    slot_data = probe.get("data") or {}
    available: set[str] = set()
    unavailable: set[str] = set()

    entries = slot_data.get("slots") if isinstance(slot_data, dict) else None
    if not isinstance(entries, list) or not entries:
        logger.info(f"court={court}: no slot list in probe response, trying booking anyway")
    if isinstance(entries, list):
        for e in entries:
            t = e.get("time") or e.get("slot") or ""
            if not t:
                continue
            is_available = bool(e.get("available"))
            is_booked = bool(e.get("is_booked"))
            is_past = bool(e.get("is_past"))
            is_disabled = (
                bool(e.get("is_user_disabled"))
                or bool(e.get("is_event_blocked"))
                or bool(e.get("is_court_restricted"))
            )
            if is_booked or is_past or is_disabled or not is_available:
                unavailable.add(t)
            else:
                available.add(t)

    # Partial-slot handling. Only kicks in if caller opts in via allow_partial;
    # orchestrator prefers full booking on ALL courts before falling back.
    missing = [s for s in slots if s in unavailable]
    if missing:
        if not allow_partial:
            return False, f"slots-already-booked: {missing}"
        if not available:
            return False, f"slots-already-booked: {missing}"
        partial = longest_contiguous_available(slots, available)
        if not partial:
            return False, f"slots-already-booked: {missing}"
        if partial != slots:
            logger.info(
                f"PARTIAL SLOT FALLBACK: requested={slots} unavailable={missing} "
                f"booking={partial} (longest contiguous available run)"
            )
            slots = partial
    elif available and not all(s in available for s in slots):
        # No slots explicitly in `unavailable` set but some aren't in
        # `available` either — try as-is and let the API decide.
        logger.info(f"note: some slots not in 'available' set, trying anyway: {slots}")

    if dry_run:
        logger.info(
            f"DRY RUN: would POST club_create_booking "
            f"court={court} date={date_str} slots={slots} player2={player2}"
        )
        return True, "dry-run-ok"

    res = client.create_booking(
        date=date_str, court=court, slots=slots, player2=player2
    )
    logger.info(f"create_booking -> {truncate(res)}")

    if res.get("success"):
        return True, f"booked: {res.get('data')}"

    data = res.get("data") or {}
    msg = data.get("message") if isinstance(data, dict) else str(data)
    return False, f"create-failed: {msg}"


# --------------------------------------------------------------------------- #
# Orchestration with account fallback                                          #
# --------------------------------------------------------------------------- #

def resolve_date(date_arg: str) -> str:
    """Resolve --date value. 'auto' = today + 8 days (for midnight cron jobs)."""
    if date_arg == "auto":
        target = date.today() + timedelta(days=8)
        return target.isoformat()
    return date_arg


def pick_player2(booking_account: str, fallback_player: str) -> dict[str, str]:
    """Return Player 2 contact info. If fallback_player is the same as the
    booking account, pick a different family member automatically."""
    if fallback_player != booking_account:
        return FAMILY_CONTACTS[fallback_player]
    # Pick the first family member that isn't the booking account
    for name, info in FAMILY_CONTACTS.items():
        if name != booking_account:
            return info
    return FAMILY_CONTACTS[fallback_player]  # shouldn't happen


def truncate(obj, n=400) -> str:
    s = json.dumps(obj, default=str)
    return s if len(s) <= n else s[:n] + "..."


def _has_existing_booking(
    client: "BookingClient",
    date_str: str,
    requested_slots: list[str],
    logger: logging.Logger,
) -> bool:
    """Check if the logged-in member already has a confirmed booking
    overlapping with the requested slots on this date.

    Uses the club_get_my_bookings API (the "My Bookings" section).
    Returns True if an overlapping booking exists → caller should skip.
    """
    try:
        bookings = client.get_my_bookings()
    except Exception as e:
        logger.warning(f"could not fetch my bookings: {e} — proceeding anyway")
        return False

    requested = set(requested_slots)
    for b in bookings:
        if b.get("booking_date") != date_str:
            continue
        if b.get("booking_status") != "confirmed":
            continue
        existing = {s.strip() for s in (b.get("time_slot") or "").split(",")}
        overlap = requested & existing
        if overlap:
            logger.info(
                f"ALREADY BOOKED: {b.get('court_id')} on {date_str} "
                f"slots={b.get('time_slot')} (overlap with requested: {overlap}) "
                f"— skipping to avoid double-booking"
            )
            return True
    return False


def run(args) -> int:
    date_str = resolve_date(args.date)
    log_dir = Path(args.log_dir).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"pickleball_{args.account}_{date_str}.log"
    logger = setup_logger(log_path)

    logger.info(f"target date: {date_str} (from --date {args.date})")

    # Load account credentials from pickleball_accounts.json
    accounts_path = Path(args.accounts_file).expanduser()
    if not accounts_path.exists():
        logger.error(f"accounts file not found: {accounts_path}")
        return 2
    accounts = load_accounts(accounts_path)

    # Build the account chain: primary + fallback accounts
    account_chain = [args.account]
    if args.fallback_account:
        for fa in args.fallback_account:
            if fa not in account_chain:
                account_chain.append(fa)
    logger.info(f"account chain: {account_chain}")

    # Court preference: try requested court first, then fall back.
    court_order = [args.court] + [c for c in (3, 2, 1) if c != args.court]

    client: Optional[BookingClient] = None
    try:
        for attempt in range(1, args.retries + 1):
            logger.info(f"=== attempt {attempt}/{args.retries} ===")

            for acct_name in account_chain:
                acct_cfg = accounts.get(acct_name)
                if not acct_cfg:
                    logger.warning(f"account '{acct_name}' not found in accounts file")
                    continue

                # Login fresh for this account
                try:
                    creds = Creds.login(
                        acct_name,
                        acct_cfg["membership_no"],
                        acct_cfg["password"],
                        logger,
                    )
                except Exception as e:
                    logger.error(f"login failed for {acct_name}: {e}")
                    continue

                # Create or switch client
                if client is None:
                    client = BookingClient(creds, logger)
                else:
                    client.switch_account(creds)

                # Check "My Bookings" — if this account (or any family
                # member sharing the same membership) already has a confirmed
                # booking overlapping our requested slots, we're done.
                if _has_existing_booking(client, date_str, args.slots, logger):
                    logger.info(
                        f"account {acct_name} already has overlapping booking "
                        f"for {date_str} — exiting successfully"
                    )
                    return 0

                player2 = pick_player2(acct_name, args.fallback_player)
                logger.info(
                    f"trying account={acct_name} player2={player2['name']} "
                    f"({player2['phone']})"
                )

                # Two-pass court search: first try to get the FULL requested
                # slot set on any court (3 → 2 → 1). Only if no court has full
                # availability do we fall back to partial booking (if
                # --allow-partial, which defaults to True).
                passes = [False]
                if args.allow_partial:
                    passes.append(True)

                acct_action = None  # "next-account" | "next-attempt" | None
                for allow_partial in passes:
                    if allow_partial:
                        logger.info(
                            f"no full slot available on any court for "
                            f"{acct_name} — falling back to partial booking"
                        )
                    for court in court_order:
                        ok, msg = try_single_attempt(
                            client,
                            date_str=date_str,
                            court=court,
                            slots=args.slots,
                            player2=player2,
                            dry_run=not args.confirm,
                            logger=logger,
                            allow_partial=allow_partial,
                        )
                        logger.info(
                            f"account={acct_name} court={court} "
                            f"allow_partial={allow_partial} -> ok={ok} {msg}"
                        )

                        if ok:
                            logger.info(
                                f"SUCCESS account={acct_name} attempt={attempt} "
                                f"court={court} partial={allow_partial}: {msg}"
                            )
                            return 0

                        if "already-booked" in msg:
                            continue  # try next court
                        if "slots-not-available" in msg and "7 days" in msg:
                            acct_action = "next-attempt"
                            break  # date not open yet, retry later
                        if "weekly booking limit" in msg or "remaining" in msg:
                            logger.info(
                                f"account {acct_name} weekly limit hit, "
                                f"trying next account..."
                            )
                            acct_action = "next-account"
                            break  # switch to next account in chain
                        # other failure -> try next court

                    if acct_action is not None:
                        break  # don't try partial pass if blocked

                if acct_action == "next-attempt":
                    break  # stop trying accounts, wait for retry gap
                # Otherwise (next-account or exhausted courts) try next account
                continue

            if attempt < args.retries:
                logger.info(f"sleeping {args.retry_gap}s before next attempt")
                time.sleep(args.retry_gap)

        logger.error("all retries exhausted")
        return 1
    finally:
        if client:
            client.close()


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def parse_args():
    p = argparse.ArgumentParser(
        description="Book pickleball courts at The Club Mumbai via HTTP API."
    )
    p.add_argument("--account", required=True, choices=["amit", "khyati", "zaheer", "annika"])
    p.add_argument(
        "--date",
        required=True,
        help='YYYY-MM-DD, or "auto" = today + 8 days',
    )
    p.add_argument(
        "--slots",
        required=True,
        nargs="+",
        help="slot start times, e.g. 17:30 18:00 18:30",
    )
    p.add_argument("--court", type=int, default=3, choices=[1, 2, 3])
    p.add_argument(
        "--fallback-player",
        required=True,
        choices=list(FAMILY_CONTACTS.keys()),
        help="Player 2 identity (family member)",
    )
    p.add_argument(
        "--fallback-account",
        nargs="+",
        choices=["amit", "khyati", "zaheer", "annika"],
        help="Accounts to try if primary's weekly limit is hit",
    )
    p.add_argument("--confirm", action="store_true", help="actually submit")
    p.add_argument("--retries", type=int, default=8)
    p.add_argument("--retry-gap", type=int, default=30)
    p.add_argument(
        "--allow-partial",
        dest="allow_partial",
        action="store_true",
        default=True,
        help="(default) book longest contiguous available subset if full slot unavailable",
    )
    p.add_argument(
        "--no-allow-partial",
        dest="allow_partial",
        action="store_false",
        help="disable partial-slot fallback; require all requested slots",
    )
    p.add_argument(
        "--accounts-file",
        default=str(Path(__file__).parent / "pickleball_accounts.json"),
    )
    p.add_argument(
        "--log-dir",
        default=str(Path(__file__).parent / "pickleball_logs"),
    )
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(run(parse_args()))
