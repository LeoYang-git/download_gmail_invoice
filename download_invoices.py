#!/usr/bin/env python3
"""
Gmail Invoice Downloader
========================
Downloads invoice and receipt attachments from Gmail, saving them to a local folder.
Park'nPay and Too Good To Go emails are excluded.

── Setup (one-time) ──────────────────────────────────────────────────────────

1. Enable the Gmail API & create credentials:
   a. Go to https://console.cloud.google.com/
   b. Create a new project (e.g. "Invoice Downloader")
   c. Go to "APIs & Services" → "Enable APIs & Services"
      Search for "Gmail API" and click Enable
   d. Go to "APIs & Services" → "Credentials" → "Create Credentials" → "OAuth client ID"
      - Application type: Desktop app
      - Name: anything you like
   e. Click "Download JSON", rename the file to  credentials.json
      and put it in the SAME folder as this script

2. Install Python dependencies (run once in Terminal):
   pip3 install google-api-python-client google-auth-httplib2 google-auth-oauthlib

3. Run this script:
   python3 download_invoices.py

   On first run a browser window will open asking you to sign in to Google
   and grant read-only Gmail access. After that a token.json file is saved
   so you won't need to sign in again.

── Output ────────────────────────────────────────────────────────────────────

Files are saved to the OUTPUT_DIR folder defined below, named:
  YYYY-MM-DD_Sender_Subject.pdf  (etc.)

Duplicate filenames get a numeric suffix (_2, _3 …).
"""

import base64
import os
import re
import sys
from email.utils import parsedate_to_datetime
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

# Where to save downloaded invoices
OUTPUT_DIR = Path.home() / "Library" / "CloudStorage" / "OneDrive-Personal" / "Inbox" / "Invoices"

# Gmail search queries  (senders to skip are baked in)
EXCLUDE_SENDERS = [
    "no-reply@parknpay.nsw.gov.au",
    "no-reply@toogoodtogo.com",
]

SEARCH_QUERIES = [
    (
        "subject:invoice after:2023/07/01 has:attachment"
        " -from:no-reply@parknpay.nsw.gov.au"
        " -from:no-reply@toogoodtogo.com"
    ),
    (
        "(subject:receipt OR subject:\"tax invoice\") after:2023/07/01 has:attachment"
        " -from:no-reply@parknpay.nsw.gov.au"
        " -from:no-reply@toogoodtogo.com"
    ),
]

# Only download files with these extensions (empty list = download everything)
ALLOWED_EXTENSIONS = {".pdf", ".xlsx", ".xls", ".docx", ".doc", ".png", ".jpg", ".jpeg"}

# Gmail API scope — read-only is enough
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
        "Run this in Terminal and then try again:\n\n"
        "  pip3 install google-api-python-client google-auth-httplib2 google-auth-oauthlib\n"
    )
    sys.exit(1)

# ── Helpers ───────────────────────────────────────────────────────────────────

def sanitise(text: str, max_len: int = 60) -> str:
    """Make text safe to use as part of a filename."""
    text = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len].strip(" ._")


def parse_date(raw: str) -> str:
    try:
        return parsedate_to_datetime(raw).strftime("%Y-%m-%d")
    except Exception:
        return "0000-00-00"


def get_service():
    """Authenticate with Gmail and return a service object."""
    creds = None
    token_path = Path(__file__).parent / "token.json"
    creds_path = Path(__file__).parent / "credentials.json"

    if not creds_path.exists():
        print(
            "\n[ERROR] credentials.json not found.\n"
            f"Expected location: {creds_path}\n"
            "Follow the Setup steps in the header of this script.\n"
        )
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
    """Yield every thread matching a Gmail search query, handling pagination."""
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
    """Recursively yield every part of a MIME message payload."""
    if "parts" in payload:
        for part in payload["parts"]:
            yield from all_parts(part)
    else:
        yield payload


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """Return a Path that doesn't already exist, appending _2, _3 … if needed."""
    candidate = directory / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


def download_attachments(service, message: dict, output_dir: Path) -> int:
    """Download all allowed attachments from one Gmail message. Returns file count."""
    msg_id = message["id"]
    headers = {
        h["name"]: h["value"]
        for h in message.get("payload", {}).get("headers", [])
    }

    date_str = parse_date(headers.get("Date", ""))
    raw_from  = headers.get("From", "unknown")
    # Extract just the sender name or domain for the filename
    sender_name = re.sub(r"<.*?>", "", raw_from).strip() or raw_from.split("@")[-1].split(">")[0]
    sender = sanitise(sender_name, 30) or "unknown"
    subject = sanitise(headers.get("Subject", "no-subject"), 60)

    saved = 0
    for part in all_parts(message.get("payload", {})):
        filename = part.get("filename", "").strip()
        if not filename:
            continue

        suffix = Path(filename).suffix.lower()
        if ALLOWED_EXTENSIONS and suffix not in ALLOWED_EXTENSIONS:
            continue

        body = part.get("body", {})
        attachment_id = body.get("attachmentId")
        data = body.get("data")

        # Fetch attachment data if not already inline
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
        stem = f"{date_str}_{sender}_{subject}"
        out_path = unique_path(output_dir, stem, suffix)
        out_path.write_bytes(file_bytes)
        print(f"    ✓  {out_path.name}")
        saved += 1

    return saved


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\nInvoice downloader starting …")
    print(f"Output folder: {OUTPUT_DIR}\n")

    service = get_service()
    seen_thread_ids: set = set()
    total_threads = 0
    total_files = 0

    for query in SEARCH_QUERIES:
        print(f"Searching Gmail: {query[:90]} …")
        thread_count = 0

        for thread in iter_all_threads(service, query):
            tid = thread["id"]
            if tid in seen_thread_ids:
                continue
            seen_thread_ids.add(tid)
            thread_count += 1
            total_threads += 1

            # Fetch the full thread so we get attachment data
            thread_data = service.users().threads().get(
                userId="me", id=tid, format="full"
            ).execute()

            for msg in thread_data.get("messages", []):
                # Double-check we're not downloading excluded senders
                hdrs = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                from_addr = hdrs.get("From", "")
                if any(ex in from_addr for ex in EXCLUDE_SENDERS):
                    continue

                subj = hdrs.get("Subject", "(no subject)")
                frm  = hdrs.get("From", "unknown")
                print(f"  [{hdrs.get('Date','')[:16]}]  {frm[:40]}  |  {subj[:55]}")
                count = download_attachments(service, msg, OUTPUT_DIR)
                if count == 0:
                    print(f"    –  no downloadable attachments")
                total_files += count

        print(f"  → {thread_count} threads processed\n")

    print("=" * 65)
    print(f"Done!  {total_threads} email threads scanned.")
    print(f"       {total_files} invoice files downloaded to:")
    print(f"       {OUTPUT_DIR}")
    print("=" * 65)


if __name__ == "__main__":
    main()
