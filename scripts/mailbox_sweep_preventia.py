#!/usr/bin/env python3
"""One-time Gmail mailbox sweep for paul.hopcraft@preventia.com.au.

Analysis-only. This script has NO send/draft/watch capability -- it reads
mail (sent + received), writes it to a local JSONL file, and computes
summary stats. It is not a scheduled job and does not register any
cron/loop; run it manually, once, then feed the output into a memory
artifact by hand.

Uses a dedicated Hermes profile (``<hermes_root>/profiles/preventia``) so
this account's OAuth token never collides with a personal Gmail token at
the default ``HERMES_HOME``.

Usage:
  # 1. One-time OAuth (see --auth-help for the full walkthrough)
  python scripts/mailbox_sweep_preventia.py auth-status
  python scripts/mailbox_sweep_preventia.py auth-help

  # 2. Cheap size check before committing to a full sweep
  python scripts/mailbox_sweep_preventia.py count

  # 3. Sweep (resumable -- safe to Ctrl-C and re-run)
  python scripts/mailbox_sweep_preventia.py sweep
  python scripts/mailbox_sweep_preventia.py sweep --limit 500   # test run

  # 4. Aggregate stats over what's been swept so far
  python scripts/mailbox_sweep_preventia.py stats
"""

from __future__ import annotations

import argparse
import base64
import html as html_lib
import importlib.util
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

MAILBOX_ADDRESS = "paul.hopcraft@preventia.com.au"
PROFILE_NAME = "preventia"
DEFAULT_QUERY = "-in:spam -in:trash -in:chats"

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _resolve_hermes_root() -> Path:
    """Resolve the current Hermes home root (not a profile subdir).

    Imports ``hermes_constants`` directly from this repo's root rather than
    the google-workspace skill's ``_hermes_home.py`` fallback shim -- that
    shim's own hardcoded fallback (``Path.home() / ".hermes"``) is stale as
    of the 2026-07-18 Windows home migration (``~/.hermes`` ->
    ``%LOCALAPPDATA%\\hermes``, commit 840fc64ad). This script always runs
    from inside the hermes-agent repo, so hermes_constants (pure stdlib) is
    reliably importable once the repo root is on sys.path.
    """
    from hermes_constants import get_hermes_home

    return get_hermes_home()


HERMES_ROOT = _resolve_hermes_root()
GWS_SCRIPTS_DIR = HERMES_ROOT / "skills" / "productivity" / "google-workspace" / "scripts"
if not (GWS_SCRIPTS_DIR / "setup.py").exists():
    _legacy = Path.home() / ".hermes" / "skills" / "productivity" / "google-workspace" / "scripts"
    if (_legacy / "setup.py").exists():
        GWS_SCRIPTS_DIR = _legacy
    else:
        print(f"ERROR: google-workspace skill scripts not found at {GWS_SCRIPTS_DIR} or {_legacy}", file=sys.stderr)
        print("Is the google-workspace skill installed for this Hermes profile?", file=sys.stderr)
        sys.exit(1)

# Follows the documented <hermes_root>/profiles/<name> convention (see
# hermes_constants.get_default_hermes_root) so this account's token lives
# at <hermes_root>/profiles/preventia/google_token.json and never
# overwrites a personal Gmail token at the default HERMES_HOME.
PROFILE_HOME = HERMES_ROOT / "profiles" / PROFILE_NAME
# Must be set before importing setup.py / google_api.py -- both resolve
# HERMES_HOME once at import time.
os.environ["HERMES_HOME"] = str(PROFILE_HOME)

OUTPUT_JSONL = PROFILE_HOME / "mailbox_sweep_preventia.jsonl"
STATS_JSON = PROFILE_HOME / "mailbox_sweep_preventia_stats.json"


def _load_gws_module(name: str):
    """Load a google-workspace skill script by explicit file path.

    ``import setup`` or ``import google_api`` by bare name is unsafe here:
    ``mailbox_sweep_preventia.py`` needs the hermes-agent repo root on
    sys.path (for ``hermes_constants``), and that repo root has its own
    unrelated top-level ``setup.py`` (the setuptools packaging script,
    which reads ``sys.argv`` itself). A bare ``import setup`` silently
    resolves to whichever one sys.path finds first. Loading by absolute
    path sidesteps the name collision entirely.
    """
    path = GWS_SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_mailbox_sweep_gws_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

AUTH_HELP = f"""
One-time OAuth setup for {MAILBOX_ADDRESS} (isolated profile: {PROFILE_NAME})
================================================================================

This profile is empty -- nothing is cached yet. Steps (interactive, ~5 min):

1. Google Cloud OAuth client (skip if you already have one you're willing
   to reuse -- any "Desktop app" OAuth client works, it doesn't have to be
   new):
   - https://console.cloud.google.com/projectselector2/home/dashboard
   - Enable the Gmail API: https://console.cloud.google.com/apis/library
   - Credentials -> Create Credentials -> OAuth 2.0 Client ID -> Desktop app
   - If the app is in Testing mode, add {MAILBOX_ADDRESS} as a test user:
     https://console.cloud.google.com/auth/audience
   - Download the client_secret JSON.

2. Point setup.py at the ISOLATED profile for every step below -- set
   HERMES_HOME first so nothing touches your personal token:

   PowerShell:
     $env:HERMES_HOME = "{PROFILE_HOME}"
   Bash:
     export HERMES_HOME="{PROFILE_HOME}"

3. Store the client secret, get the auth URL, sign in as
   {MAILBOX_ADDRESS} in the browser, then exchange the code:

   python "{GWS_SCRIPTS_DIR / 'setup.py'}" --client-secret /path/to/client_secret.json
   python "{GWS_SCRIPTS_DIR / 'setup.py'}" --auth-url
   # open the printed URL, sign in as {MAILBOX_ADDRESS}, approve,
   # then copy the FULL redirected URL (it will look like it failed to
   # load at localhost:1 -- that's expected) and paste it back:
   python "{GWS_SCRIPTS_DIR / 'setup.py'}" --auth-code "PASTE_THE_REDIRECTED_URL_HERE"

4. Verify, then come back here:
   python scripts/mailbox_sweep_preventia.py auth-status
"""


def _ensure_auth():
    gws_setup = _load_gws_module("setup")

    if not gws_setup.check_auth(quiet=True):
        print("NOT_AUTHENTICATED for this profile.", file=sys.stderr)
        print(f"Run: python {Path(__file__).name} auth-help", file=sys.stderr)
        sys.exit(1)


def cmd_auth_status(_args) -> None:
    gws_setup = _load_gws_module("setup")

    print(f"profile home: {PROFILE_HOME}")
    ok = gws_setup.check_auth()
    sys.exit(0 if ok else 1)


def cmd_auth_help(_args) -> None:
    print(AUTH_HELP)


def _b64_decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")


def _walk_parts(payload: dict):
    parts = payload.get("parts")
    if not parts:
        yield payload
        return
    for part in parts:
        yield from _walk_parts(part)


def _strip_html(fragment: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", fragment, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html_lib.unescape(text)


def extract_body_text(payload: dict) -> str:
    parts = list(_walk_parts(payload))
    for part in parts:
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return _b64_decode(part["body"]["data"])
    for part in parts:
        if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
            return _strip_html(_b64_decode(part["body"]["data"]))
    if payload.get("body", {}).get("data"):
        return _b64_decode(payload["body"]["data"])
    return ""


_QUOTE_HEADER_RE = re.compile(r"^On .{5,90} wrote:\s*$")
_ORIGINAL_MSG_RE = re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.I)


def strip_quoted_reply(text: str) -> str:
    """Best-effort cut at the start of a quoted reply chain."""
    lines = text.splitlines()
    cut_at = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if _QUOTE_HEADER_RE.match(stripped) or _ORIGINAL_MSG_RE.match(stripped):
            cut_at = i
            break
        if stripped.startswith(">") and i + 1 < len(lines) and lines[i + 1].strip().startswith(">"):
            cut_at = i
            break
    return "\n".join(lines[:cut_at]).strip()


def _headers_dict(msg: dict) -> dict[str, str]:
    return {
        h["name"].lower(): h["value"]
        for h in msg.get("payload", {}).get("headers", [])
        if h.get("name")
    }


def determine_direction(headers: dict[str, str], label_ids: list[str]) -> str:
    if MAILBOX_ADDRESS.lower() in headers.get("from", "").lower():
        return "sent"
    if "SENT" in (label_ids or []):
        return "sent"
    return "received"


def _load_swept_ids() -> set[str]:
    if not OUTPUT_JSONL.exists():
        return set()
    ids = set()
    with OUTPUT_JSONL.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ids.add(json.loads(line)["id"])
            except (json.JSONDecodeError, KeyError):
                continue
    return ids


def cmd_count(args) -> None:
    _ensure_auth()
    google_api = _load_gws_module("google_api")

    service = google_api.build_service("gmail", "v1")
    result = service.users().messages().list(userId="me", q=args.query, maxResults=1).execute()
    estimate = result.get("resultSizeEstimate", 0)
    print(f"query: {args.query!r}")
    print(f"resultSizeEstimate: {estimate} (Gmail's approximation, not exact)")


def _get_message_with_retry(service, message_id: str, max_retries: int = 5):
    from googleapiclient.errors import HttpError

    delay = 1.0
    for attempt in range(max_retries):
        try:
            return service.users().messages().get(userId="me", id=message_id, format="full").execute()
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            if status in (429, 500, 503) and attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise


def cmd_sweep(args) -> None:
    _ensure_auth()
    google_api = _load_gws_module("google_api")

    PROFILE_HOME.mkdir(parents=True, exist_ok=True)
    already_swept = _load_swept_ids()
    print(f"output: {OUTPUT_JSONL}")
    print(f"already swept: {len(already_swept)} messages (resuming)")

    service = google_api.build_service("gmail", "v1")

    fetched_this_run = 0
    page_token = None
    with OUTPUT_JSONL.open("a", encoding="utf-8") as out:
        while True:
            list_result = service.users().messages().list(
                userId="me", q=args.query, maxResults=500, pageToken=page_token
            ).execute()
            message_refs = list_result.get("messages", [])

            for ref in message_refs:
                if args.limit and fetched_this_run >= args.limit:
                    print(f"reached --limit {args.limit}, stopping (resumable)")
                    return
                if ref["id"] in already_swept:
                    continue

                msg = _get_message_with_retry(service, ref["id"])
                headers = _headers_dict(msg)
                body = strip_quoted_reply(extract_body_text(msg.get("payload", {})))
                record = {
                    "id": msg["id"],
                    "threadId": msg.get("threadId", ""),
                    "date": headers.get("date", ""),
                    "from": headers.get("from", ""),
                    "to": headers.get("to", ""),
                    "cc": headers.get("cc", ""),
                    "subject": headers.get("subject", ""),
                    "direction": determine_direction(headers, msg.get("labelIds", [])),
                    "labelIds": msg.get("labelIds", []),
                    "body": body,
                }
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                out.flush()
                already_swept.add(ref["id"])
                fetched_this_run += 1
                if fetched_this_run % 100 == 0:
                    print(f"  swept {fetched_this_run} new messages this run "
                          f"({len(already_swept)} total)...")

            page_token = list_result.get("nextPageToken")
            if not page_token:
                break

    print(f"done. swept {fetched_this_run} new messages this run "
          f"({len(already_swept)} total in {OUTPUT_JSONL})")


_STOPWORDS = {
    "the", "a", "an", "to", "and", "of", "in", "for", "on", "is", "it", "this",
    "that", "with", "you", "your", "i", "we", "re", "fwd", "our", "at", "be",
    "as", "are", "was", "have", "has", "will", "from", "or", "by", "not",
}


def _word_freq(texts: list[str], top_n: int) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for text in texts:
        for word in re.findall(r"[a-zA-Z']{3,}", text.lower()):
            if word not in _STOPWORDS:
                counter[word] += 1
    return counter.most_common(top_n)


def _extract_email(addr_field: str) -> str:
    match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", addr_field or "")
    return match.group(0).lower() if match else (addr_field or "").strip().lower()


def cmd_stats(_args) -> None:
    if not OUTPUT_JSONL.exists():
        print(f"no sweep data at {OUTPUT_JSONL} yet. Run `sweep` first.")
        sys.exit(1)

    records = []
    with OUTPUT_JSONL.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    total = len(records)
    directions = Counter(r["direction"] for r in records)

    correspondents: Counter[str] = Counter()
    for r in records:
        other_field = r["to"] if r["direction"] == "sent" else r["from"]
        for part in re.split(r",(?![^<]*>)", other_field or ""):
            email = _extract_email(part)
            if email and email != MAILBOX_ADDRESS.lower():
                correspondents[email] += 1

    word_counts = [len(r["body"].split()) for r in records if r["body"]]
    avg_words = sum(word_counts) / len(word_counts) if word_counts else 0
    word_counts_sorted = sorted(word_counts)
    median_words = word_counts_sorted[len(word_counts_sorted) // 2] if word_counts_sorted else 0

    closings = []
    openings = []
    for r in records:
        lines = [l.strip() for l in r["body"].splitlines() if l.strip()]
        if lines:
            openings.append(lines[0])
            closings.append(lines[-1])
            if len(lines) > 1:
                closings.append(lines[-2])

    dates = sorted(r["date"] for r in records if r["date"])

    summary = {
        "total_messages": total,
        "direction_counts": dict(directions),
        "date_range": {"earliest": dates[0] if dates else None, "latest": dates[-1] if dates else None},
        "top_correspondents": correspondents.most_common(20),
        "body_word_count": {"avg": round(avg_words, 1), "median": median_words},
        "top_closing_lines": Counter(closings).most_common(20),
        "top_opening_lines": Counter(openings).most_common(20),
        "top_subject_words": _word_freq([r["subject"] for r in records], 30),
        "top_body_words": _word_freq([r["body"] for r in records], 40),
    }

    STATS_JSON.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nwritten to {STATS_JSON}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("auth-status", help="Check OAuth status for the isolated preventia profile")
    p_status.set_defaults(func=cmd_auth_status)

    p_help = sub.add_parser("auth-help", help="Print the one-time OAuth setup walkthrough")
    p_help.set_defaults(func=cmd_auth_help)

    p_count = sub.add_parser("count", help="Cheap approximate count of matching messages, no fetch")
    p_count.add_argument("--query", default=DEFAULT_QUERY)
    p_count.set_defaults(func=cmd_count)

    p_sweep = sub.add_parser("sweep", help="Sweep sent+received mail into a local JSONL (resumable)")
    p_sweep.add_argument("--query", default=DEFAULT_QUERY)
    p_sweep.add_argument("--limit", type=int, default=0, help="Max NEW messages to fetch this run (0 = no limit)")
    p_sweep.set_defaults(func=cmd_sweep)

    p_stats = sub.add_parser("stats", help="Compute aggregate stats over what's been swept so far")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
