#!/usr/bin/env python3
"""
Gmail Financial Document Fetcher
==================================
Downloads financial document attachments from Gmail into OneDrive Inbox.

Rules-based: each biller has its own Gmail query. By default downloads
the previous calendar month. Use --since for a bulk backfill.

Usage:
  python3 fetch_gmail_docs.py                        # previous month
  python3 fetch_gmail_docs.py --since 2024/01/01     # backfill from date
  python3 fetch_gmail_docs.py --rule "Rental"        # one rule only (case-insensitive)
"""

import argparse
import base64
import re
import sys
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import Path

# ── Output ────────────────────────────────────────────────────────────────────

INBOX_DIR = Path.home() / "Library" / "CloudStorage" / "OneDrive-Personal" / "Inbox"

# ── Rules ─────────────────────────────────────────────────────────────────────
# Each rule downloads to its own Inbox subfolder. inbox_dir is the subfolder
# name; the classifier uses it to determine category and destination without
# needing to re-classify well-known sources.

RULES = [
    {
        "name": "Rental Statement",
        "query": "from:@resbymirvac.com has:attachment",
        "inbox_dir": "Rental Statement",  # → Properties/Waterloo
    },
    {
        "name": "Council Payment",
        "query": "from:kuringgai@pml.com.au has:attachment",
        "inbox_dir": "Council Payment",   # → Properties/Lindfield
    },
    {
        "name": "Electricity Bill",
        "query": "from:noreply@amber.com.au has:attachment",
        "inbox_dir": "Electricity Bill",  # → Properties/Lindfield
    },
    {
        "name": "More Telecom",
        "query": "from:@moretelecom.com.au has:attachment",
        "inbox_dir": "More Telecom",      # → Spending/Software
    },
    {
        "name": "Self-Sent",
        "query": (
            "(from:leo.yang.au@gmail.com OR from:leo.yang@cba.com.au)"
            " has:attachment"
        ),
        "inbox_dir": "Self-Sent",         # → full classification; never deleted
        "allowed_extensions": {
            ".pdf", ".xlsx", ".xls", ".docx", ".doc",
            ".jpg", ".jpeg", ".png", ".heic",
        },
    },
    {
        "name": "Generic Invoices & Receipts",
        "query": (
            "(subject:invoice OR subject:receipt OR subject:\"tax invoice\""
            " OR subject:bill OR subject:statement) has:attachment"
            " -from:@resbymirvac.com"
            " -from:kuringgai@pml.com.au"
            " -from:noreply@amber.com.au"
            " -from:@moretelecom.com.au"
            " -from:leo.yang.au@gmail.com"
            " -from:leo.yang@cba.com.au"
        ),
        "inbox_dir": "Generic",           # → full classification
    },
]

# Senders to skip even if matched by a query
EXCLUDE_SENDERS = [
    "no-reply@parknpay.nsw.gov.au",
    "no-reply@toogoodtogo.com",
]

ALLOWED_EXTENSIONS = {".pdf", ".xlsx", ".xls", ".docx", ".doc"}

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# ── Dependency check ──────────────────────────────────────────────────────────

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
except ImportError:
    print(
        "\n[ERROR] Missing dependencies.\n"
        "Run: pip3 install google-api-python-client google-auth-httplib2 google-auth-oauthlib\n"
    )
    sys.exit(1)

# ── Helpers ───────────────────────────────────────────────────────────────────

def sanitise(text: str, max_len: int = 60) -> str:
    text = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len].strip(" ._")


def parse_date(raw: str) -> str:
    try:
        return parsedate_to_datetime(raw).strftime("%Y-%m-%d")
    except Exception:
        return "0000-00-00"


def get_service():
    creds = None
    token_path = Path(__file__).parent / "token.json"
    creds_path = Path(__file__).parent / "credentials.json"

    if not creds_path.exists():
        print(f"\n[ERROR] credentials.json not found at {creds_path}\n")
        sys.exit(1)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def iter_all_threads(service, query: str):
    page_token = None
    while True:
        params = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            params["pageToken"] = page_token
        result = service.users().threads().list(**params).execute()
        for thread in result.get("threads", []):
            yield thread
        page_token = result.get("nextPageToken")
        if not page_token:
            break


def all_parts(payload: dict):
    if "parts" in payload:
        for part in payload["parts"]:
            yield from all_parts(part)
    else:
        yield payload


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    candidate = directory / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


def download_attachments(service, message: dict, output_dir: Path,
                         allowed_extensions: set | None = None) -> int:
    msg_id = message["id"]
    headers = {
        h["name"]: h["value"]
        for h in message.get("payload", {}).get("headers", [])
    }

    date_str = parse_date(headers.get("Date", ""))
    subject = sanitise(headers.get("Subject", "no-subject"), 60)

    saved = 0
    for part in all_parts(message.get("payload", {})):
        filename = part.get("filename", "").strip()
        if not filename:
            continue

        suffix = Path(filename).suffix.lower()
        exts = allowed_extensions if allowed_extensions is not None else ALLOWED_EXTENSIONS
        if exts and suffix not in exts:
            continue

        body = part.get("body", {})
        attachment_id = body.get("attachmentId")
        data = body.get("data")

        if attachment_id:
            att = (
                service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=msg_id, id=attachment_id)
                .execute()
            )
            data = att.get("data", "")

        if not data:
            continue

        file_bytes = base64.urlsafe_b64decode(data)
        stem = f"{subject}_{date_str}"
        out_path = unique_path(output_dir, stem, suffix)
        out_path.write_bytes(file_bytes)
        print(f"    ✓  {out_path.name}")
        saved += 1

    return saved


def previous_month_range() -> tuple[str, str]:
    today = date.today()
    if today.month == 1:
        start = date(today.year - 1, 12, 1)
    else:
        start = date(today.year, today.month - 1, 1)
    end = date(today.year, today.month, 1)
    return start.strftime("%Y/%m/%d"), end.strftime("%Y/%m/%d")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Download financial document attachments from Gmail.")
    parser.add_argument(
        "--since",
        metavar="YYYY/MM/DD",
        default=None,
        help="Download emails since this date (use with --until for a range)",
    )
    parser.add_argument(
        "--until",
        metavar="YYYY/MM/DD",
        default=None,
        help="Download emails before this date (default: 1st of current month)",
    )
    parser.add_argument(
        "--rule",
        metavar="NAME",
        default=None,
        help="Run only the rule whose name contains this string (case-insensitive)",
    )
    args = parser.parse_args()

    default_since, default_until = previous_month_range()
    since = args.since or default_since
    until = args.until or (default_until if not args.since else None)
    date_filter = f"after:{since}" + (f" before:{until}" if until else "")

    active_rules = RULES
    if args.rule:
        active_rules = [r for r in RULES if args.rule.lower() in r["name"].lower()]
        if not active_rules:
            names = ", ".join(f'"{r["name"]}"' for r in RULES)
            print(f'[ERROR] No rule matching "{args.rule}". Available: {names}')
            sys.exit(1)

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\nDownloader starting …")
    print(f"Date filter : {date_filter}")
    print(f"Inbox       : {INBOX_DIR}")
    print(f"Rules       : {', '.join(r['name'] for r in active_rules)}\n")

    service = get_service()
    seen_thread_ids: set = set()
    total_threads = 0
    total_files = 0

    for rule in active_rules:
        query = f"{rule['query']} {date_filter}"
        print(f"── {rule['name']} ──────────────────────────────────────")
        print(f"   Query: {query[:120]}")
        rule_threads = 0
        rule_files = 0

        for thread in iter_all_threads(service, query):
            tid = thread["id"]
            if tid in seen_thread_ids:
                continue
            seen_thread_ids.add(tid)

            thread_data = service.users().threads().get(
                userId="me", id=tid, format="full"
            ).execute()

            for msg in thread_data.get("messages", []):
                hdrs = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                from_addr = hdrs.get("From", "")
                if any(ex in from_addr for ex in EXCLUDE_SENDERS):
                    continue

                subj = hdrs.get("Subject", "(no subject)")
                frm = hdrs.get("From", "unknown")
                print(f"  [{hdrs.get('Date','')[:16]}]  {frm[:40]}  |  {subj[:55]}")
                rule_inbox = INBOX_DIR / rule["inbox_dir"]
                rule_inbox.mkdir(parents=True, exist_ok=True)
                count = download_attachments(service, msg, rule_inbox,
                                             rule.get("allowed_extensions"))
                if count == 0:
                    print(f"    –  no downloadable attachments")
                rule_files += count

            rule_threads += 1
            total_threads += 1

        total_files += rule_files
        print(f"  → {rule_threads} threads, {rule_files} files\n")

    print("=" * 65)
    print(f"Done!  {total_threads} threads scanned, {total_files} files downloaded.")
    print(f"       {INBOX_DIR}")
    print("=" * 65)


if __name__ == "__main__":
    main()
