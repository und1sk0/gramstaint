#!/usr/bin/env python3
"""
gramstaint — enumerate Instagram followers, score them, export to CSV.

Usage:
    uv run gramstaint.py login                                     # authenticate and save token to .creds/
    uv run gramstaint.py scrape [--full] [--limit N] [--batch N]  # list followers (--full adds per-user stats)
    uv run gramstaint.py remove <csv_file>                         # remove followers marked remove=true in csv
    uv run gramstaint.py token                                     # print bearer token + headers for raw API use
"""

import argparse
import csv
import json
import os
import sys
import time
import random
from pathlib import Path

import requests.exceptions
from instagrapi import Client
from instagrapi.exceptions import TwoFactorRequired, ClientError


SESSION_FILE   = Path("session.json")
TOKEN_FILE     = Path(".creds/token.json")
DEFAULT_OUTPUT = Path("followers.csv")

# Seconds to wait between paginated list fetches
PAGE_DELAY   = (0.5, 1.0)
# Seconds to wait between per-user info fetches
INFO_DELAY   = (0.1, 1.0)
# Seconds to wait between follower removals
REMOVE_DELAY = (0.5, 1.0)

CSV_FIELDS = [
    "user_id",
    "username",
    "full_name",
    "follower_count",
    "following_count",
    "media_count",
    "is_private",
    "is_verified",
    "is_mutual",
    "low_id",   # True if numeric ID suggests an older account
    "remove",   # blank by default — fill in True in the CSV to bulk-remove
]

# Accounts created before ~2015 tend to have IDs below this threshold.
OLD_ID_THRESHOLD = 2_000_000_000

# Retryable exception types: Instagram API errors + transient network errors
_RETRYABLE = (ClientError, OSError, requests.exceptions.RequestException)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def jitter(range_: tuple) -> None:
    time.sleep(random.uniform(*range_))


def _stat(user, attr: str):
    return getattr(user, attr, "")


def with_backoff(fn, *args, retries=4, label="request", **kwargs):
    """Call fn(*args, **kwargs) with exponential backoff on API or network errors."""
    delay = 10
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except _RETRYABLE as e:
            if attempt == retries - 1:
                raise
            wait = delay * (2 ** attempt) + random.uniform(0, 5)
            print(f"\n  [{label}] error: {e}. Retrying in {wait:.0f}s...")
            time.sleep(wait)


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

def load_token() -> str:
    if not TOKEN_FILE.exists():
        return ""
    try:
        return json.loads(TOKEN_FILE.read_text()).get("token", "")
    except (json.JSONDecodeError, OSError):
        return ""


def save_token(tok: str) -> None:
    try:
        TOKEN_FILE.parent.mkdir(exist_ok=True)
        TOKEN_FILE.write_text(json.dumps({"token": tok}, indent=2) + "\n")
    except OSError as e:
        print(f"Warning: could not save token to {TOKEN_FILE}: {e}")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _fresh_login(cl: Client, username: str, password: str) -> None:
    """Perform a full login, handling 2FA. u/p never written to disk."""
    try:
        cl.login(username, password)
    except TwoFactorRequired:
        code = input("2FA code: ").strip()
        cl.login(username, password, verification_code=code)
    cl.dump_settings(SESSION_FILE)


def cmd_login() -> None:
    """Prompt for credentials, authenticate, and persist the token."""
    username = os.environ.get("IG_USERNAME") or input("Instagram username: ").strip()
    password = os.environ.get("IG_PASSWORD") or input("Instagram password: ").strip()

    cl = Client()
    cl.delay_range = [1, 3]
    if SESSION_FILE.exists():
        try:
            cl.load_settings(SESSION_FILE)
        except (json.JSONDecodeError, OSError):
            pass  # stale/corrupt session file — proceed with fresh login
    _fresh_login(cl, username, password)
    save_token(cl.authorization)
    print(f"Token saved to {TOKEN_FILE}.")


def get_client() -> Client:
    """Restore session from disk and return an authenticated client, or exit if stale."""
    if not TOKEN_FILE.exists():
        print("Not logged in. Run: uv run gramstaint.py login")
        sys.exit(1)

    cl = Client()
    cl.delay_range = [1, 3]

    try:
        if SESSION_FILE.exists():
            cl.load_settings(SESSION_FILE)
        me = cl.user_info(cl.user_id)  # lightweight probe; result reused by callers
    except Exception:
        print("Session expired. Run: uv run gramstaint.py login")
        sys.exit(1)

    cl._me = me  # stash so scrape() can reuse without a second API call
    return cl


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def fetch_list(cl: Client, fetch_fn, user_id: str, label: str,
               limit: int = 0, batch: int = 0) -> dict:
    """Paginate a follower/following endpoint, returning {str(pk): UserShort}.

    limit: stop after N total results (0 = no limit)
    batch: max_amount hint passed to each API chunk (0 = server default)
    """
    results = {}
    max_id = ""
    page = 1
    while True:
        chunk, max_id = with_backoff(
            fetch_fn, user_id, max_amount=batch, max_id=max_id, label=f"{label} p{page}"
        )
        for user in chunk:
            results[str(user.pk)] = user  # normalize pk to str for consistent keying
            if limit and len(results) >= limit:
                max_id = ""  # signal stop
                break
        print(f"  {label}: {len(results)} fetched...", end="\r", flush=True)
        if not max_id:
            break
        page += 1
        jitter(PAGE_DELAY)
    print(f"  {label}: {len(results)} total.      ")
    return results


def fetch_user_stats(cl: Client, users: dict) -> dict:
    """Enrich each UserShort with full profile stats via user_info(). Returns {str(pk): User}."""
    enriched = {}
    total = len(users)
    for i, (pk, user) in enumerate(users.items(), 1):
        print(f"  fetching stats: {i}/{total} (@{user.username})      ", end="\r", flush=True)
        try:
            info = with_backoff(cl.user_info, pk, label=f"info:{user.username}")
            enriched[pk] = info
        except Exception as e:
            print(f"\n  skipping @{user.username}: {e}")
            enriched[pk] = user  # fall back to UserShort
        jitter(INFO_DELAY)
    print(f"  fetching stats: {total}/{total} done.              ")
    return enriched


def scrape(cl: Client, output: Path = DEFAULT_OUTPUT, full: bool = False,
           limit: int = 0, batch: int = 0):
    me = getattr(cl, "_me", None) or cl.user_info(cl.user_id)
    my_id = str(cl.user_id)
    print(f"Logged in as @{me.username} (id={my_id})\n")

    print("Fetching followers...")
    followers_short = fetch_list(cl, cl.user_followers_v1_chunk, my_id, "followers",
                                 limit=limit, batch=batch)

    # following is always fetched without a limit so mutuals detection is complete
    print("\nFetching following...")
    following_short = fetch_list(cl, cl.user_following_v1_chunk, my_id, "following",
                                 batch=batch)

    mutual_pks = set(followers_short.keys()) & set(following_short.keys())
    print(f"\nFollowers: {len(followers_short)}  |  Following: {len(following_short)}  |  Mutuals: {len(mutual_pks)}")

    if full:
        print("\nFetching per-account stats (this takes a while)...")
        followers = fetch_user_stats(cl, followers_short)
    else:
        followers = followers_short

    rows = []
    for pk, user in followers.items():
        rows.append({
            "user_id":         pk,
            "username":        user.username,
            "full_name":       user.full_name,
            "follower_count":  _stat(user, "follower_count"),
            "following_count": _stat(user, "following_count"),
            "media_count":     _stat(user, "media_count"),
            "is_private":      user.is_private,
            "is_verified":     user.is_verified,
            "is_mutual":       pk in mutual_pks,
            "low_id":          int(pk) < OLD_ID_THRESHOLD,
            "remove":          "",
        })

    rows.sort(key=lambda r: (not r["is_mutual"], r["username"].lower()))

    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWrote {len(rows)} rows -> {output}")
    print(f"Open the CSV, set remove=true on unwanted accounts, then run:")
    print(f"  uv run gramstaint.py remove {output}")


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------

def remove(cl: Client, csv_file: str):
    targets = []
    with open(csv_file, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("remove", "").strip().lower() in ("true", "yes", "1"):
                targets.append((row["user_id"], row["username"]))

    if not targets:
        print("No rows marked for removal (set remove=true in the CSV).")
        return

    print(f"Removing {len(targets)} followers...")
    for i, (uid, username) in enumerate(targets, 1):
        print(f"  [{i}/{len(targets)}] removing @{username}...")
        try:
            with_backoff(cl.user_remove_follower, int(uid), label=f"remove:{username}")
        except Exception as e:
            print(f"    ERROR: {e}")
        jitter(REMOVE_DELAY)

    print("Done.")


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

def show_token(cl: Client):
    print("\n# Bearer token (valid until session expires)\n")
    print(f"Authorization: {cl.authorization}")
    print(f"User-Agent: {cl.user_agent}")
    print(f"X-IG-App-ID: {cl.app_id}")
    print(f"\n# curl example:")
    print(f'curl -s "https://i.instagram.com/api/v1/accounts/current_user/?edit=true" \\')
    print(f'  -H "Authorization: {cl.authorization}" \\')
    print(f'  -H "User-Agent: {cl.user_agent}" \\')
    print(f'  -H "X-IG-App-ID: {cl.app_id}"')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(prog="gramstaint", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="authenticate and save token to .creds/")
    sub.add_parser("token", help="print bearer token + headers for raw API use")

    p_scrape = sub.add_parser("scrape", help="list followers [--full adds per-user stats]")
    p_scrape.add_argument("--output", "-o", type=Path, default=DEFAULT_OUTPUT, metavar="FILE",
                          help=f"output CSV path (default: {DEFAULT_OUTPUT})")
    p_scrape.add_argument("--full", action="store_true",
                          help="fetch per-user stats (follower/following/post counts)")
    p_scrape.add_argument("--limit", type=int, default=0, metavar="N",
                          help="stop after N followers (default: all)")
    p_scrape.add_argument("--batch", type=int, default=0, metavar="N",
                          help="API chunk size per page request (default: server default)")

    p_remove = sub.add_parser("remove", help="remove followers marked remove=true in csv")
    p_remove.add_argument("csv_file")

    args = parser.parse_args()

    if args.cmd == "login":
        cmd_login()
        return

    cl = get_client()

    if args.cmd == "scrape":
        scrape(cl, output=args.output, full=args.full, limit=args.limit, batch=args.batch)
    elif args.cmd == "remove":
        remove(cl, args.csv_file)
    elif args.cmd == "token":
        show_token(cl)


if __name__ == "__main__":
    main()
