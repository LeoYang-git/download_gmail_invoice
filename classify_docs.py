#!/usr/bin/env python3
"""
Financial Document Classifier and Organiser
Uses the local `claude` CLI (your Claude.ai membership) to read and classify
each file in Inbox/, then offers a dry-run before:
  • Deleting non-financial documents
  • Moving documents into Areas/Finance/ subfolders

Usage:
  python3 classify_docs.py            # dry run — shows table only
  python3 classify_docs.py --execute  # apply changes
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    print("Missing dep. Run: pip3 install pdfplumber")
    sys.exit(1)

INBOX_DIR   = Path.home() / "Library/CloudStorage/OneDrive-Personal/Inbox"
FINANCE_DIR = Path.home() / "Library/CloudStorage/OneDrive-Personal/Areas/Finance"

# ── Inbox subfolder → fixed destination (category + Finance path) ─────────────
# For known sources, category and destination are certain — Claude only names.
INBOX_RULE_CONFIG = {
    "Rental Statement": {
        "category": "properties",
        "dest":     FINANCE_DIR / "Properties" / "Waterloo",
    },
    "Council Payment": {
        "category": "properties",
        "dest":     FINANCE_DIR / "Properties" / "Lindfield",
    },
    "Electricity Bill": {
        "category": "properties",
        "dest":     FINANCE_DIR / "Properties" / "Lindfield",
    },
    "More Telecom": {
        "category": "software",
        "dest":     FINANCE_DIR / "Spending",
    },
    # "Generic" → full classification, dest determined at runtime
}

# Property management docs Claude must never delete — override is_invoice=false for these
PROTECTED_PROPERTY_DOCS = frozenset([
    "lease agreement", "lease renewal", "notice to vacate", "break lease agreement",
    "property inspection report",
    "strata agm notice", "strata agm minutes",
    "strata general meeting notice", "strata general meeting minutes",
    "strata committee meeting notice", "strata committee meeting minutes",
    "landlord income & expense statement", "development application notice",
    "strata levy statement",
])

CATEGORY_MAP = {
    "car":            "Car",
    "properties":     "Properties",
    "medical":        "Medical",
    "insurance":      "Insurance",
    "superannuation": "Superannuation",
    "retail":         "Spending",
    "food":           "Spending",
    "software":       "Spending",
    "tech":           "Spending",
    "travel":         "Spending",
    "professional":   "Spending",
    "other":          "Spending",
    "spending":       "Spending",  # fallback if Claude returns bare "spending"
    "delete":         None,
}

CLASSIFY_INSTRUCTIONS = """\
You are a document classifier. Respond ONLY with valid JSON — no markdown, no explanation.

Format: {"is_invoice": true/false, "category": "<category>", "description": "<2-5 word name>", "reason": "<10 words max>"}

── Naming rules for "description" ──────────────────────────────────────────
1. Name the DOCUMENT, not the email subject line.
2. Strip email noise: "Your", "Monthly", "Here's the receipt for", "Re:", "FW:",
   "Fwd:", "[Amber]", "Here is", "Please find attached", etc.
3. Include the vendor/organisation when it identifies the document:
   e.g. "Amber Energy Bill", "HSBC Bank Statement", "IKEA Invoice".
4. Include a location only when it differentiates similar docs from different places:
   e.g. "McGraths Hill Automotive Invoice" vs "North Shore Mechanic Invoice".
   For a single rental property, "Rental Statement" is enough — no address needed.
5. Keep to 2–5 words — scan-friendly, no abbreviations.

Canonical name examples (use these exact names for consistency):
  Rental Statement | Property Inspection Report | Landlord Income & Expense Statement
  Strata Committee Meeting Minutes | Strata Committee Meeting Notice
  Strata AGM Notice | Strata AGM Minutes
  Strata General Meeting Notice | Strata General Meeting Minutes
  Lease Agreement | Lease Renewal | Notice to Vacate | Break Lease Agreement
  Landlord Insurance Notice | Development Application Notice
  Council Rates Notice | Council Rates Instalment
  Amber Energy Bill | Sydney Water Invoice | HSBC Bank Statement
  McGraths Hill Automotive Invoice | North Shore Mechanic Invoice
  Hertz Car Rental Invoice | Medicare Claim Receipt | GitHub Subscription Receipt
  Anthropic Receipt | IKEA Invoice | Supercheap Auto Receipt

For water bills and utility invoices under "properties", include the suburb in the
description so the file can be routed to the correct property folder:
  e.g. "Lindfield Water Bill" or "Waterloo Water Bill"

── Categories ───────────────────────────────────────────────────────────────
- "car"            — vehicle purchase/service, fuel, car rental, auto parts, car insurance,
                     registration, pink slip, green slip, toll payments (e.g. Linkt, E-toll)
- "properties"     — real estate, rent, utilities (electricity, gas, water), home maintenance,
                     building reports, lease agreements/renewals, rental applications, property
                     inspection reports, strata general meeting notices/minutes, break-lease
                     notices, landlord/rental insurance, house/building insurance, council rates
                     notices, rate instalments, penalty notices from local council
- "medical"        — health, dental, optical, pharmacy, physio, allied health, hospital,
                     psychology/counselling, pathology, radiology (MRI, X-ray, ultrasound),
                     chiropractic, podiatry, occupational therapy, speech therapy,
                     Medicare statements, health fund rebate statements
- "insurance"      — life insurance, health insurance, income protection, trauma/TPD policies
- "superannuation" — super fund statements, contribution receipts, rollover confirmations,
                     annual statements, insurance-within-super notices
- For all other spending, pick the most specific sub-category:
    - "retail"       — general retail, clothing, homewares, gifts
    - "food"         — restaurants, cafes, groceries, takeaway, alcohol
    - "software"     — SaaS subscriptions, app purchases, online tools,
                       streaming services (Netflix, Spotify), digital memberships
    - "tech"         — hardware, electronics, gadgets, accessories
    - "travel"       — flights, hotels, car hire, transport, rideshare
    - "professional" — accounting, legal, consulting, financial advice
    - "other"        — anything not covered above

NEVER delete these document types — always classify as "properties" even with no dollar amount:
  Lease Agreement, Lease Renewal, Notice to Vacate, Break Lease Agreement,
  Property Inspection Report, Strata AGM Notice, Strata AGM Minutes,
  Strata General Meeting Notice, Strata General Meeting Minutes,
  Strata Committee Meeting Notice, Strata Committee Meeting Minutes,
  Landlord Income & Expense Statement, Development Application Notice

Set is_invoice to false (category "delete") ONLY when the file is clearly NOT a financial or
property-management document: product photos, run sheets, personal photos, email body
screenshots, wedding event documents, or government rebate forms with no payment amount.

The user's only rental property is in Waterloo — any Rental Statement or Landlord Income &
Expense Statement should include "Waterloo" in the description so it routes correctly.

"Document" alone is not a valid description — always name the specific document type."""


NAME_ONLY_INSTRUCTIONS = """\
You are a document namer. Respond ONLY with valid JSON — no markdown, no explanation.

Format: {"description": "<2-5 word name>", "reason": "<10 words max>"}

── Naming rules ────────────────────────────────────────────────────────────────
1. Name the DOCUMENT based on its content, not the email subject line.
2. Strip email noise: "Your", "Monthly", "Here's the receipt for", "Re:", "FW:",
   "Fwd:", "[Amber]", "Here is", "Please find attached", etc.
3. Include the vendor/organisation when it identifies the document:
   e.g. "Amber Energy Bill", "HSBC Bank Statement".
4. Include a location only when it differentiates similar docs:
   "Lindfield Water Bill" vs "Waterloo Water Bill". For a single rental property,
   "Rental Statement" is enough — no address needed.
5. Keep to 2–5 words — scan-friendly, no abbreviations.

Canonical name examples (use these exact names for consistency):
  Rental Statement | Property Inspection Report | Landlord Income & Expense Statement
  Strata Committee Meeting Minutes | Strata Committee Meeting Notice
  Strata AGM Notice | Strata AGM Minutes
  Strata General Meeting Notice | Strata General Meeting Minutes
  Lease Agreement | Lease Renewal | Notice to Vacate | Break Lease Agreement
  Landlord Insurance Notice | Development Application Notice
  Council Rates Notice | Council Rates Instalment | Amber Energy Bill
  Sydney Water Invoice | Lindfield Water Bill | Waterloo Water Bill"""


def extract_pdf_text(path: Path) -> str:
    try:
        with pdfplumber.open(path) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages[:2])
            return text[:3000].strip()
    except Exception as e:
        return f"[PDF read error: {e}]"


def run_claude(prompt: str, allow_read: bool = False, timeout: int = 60) -> str:
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "text",
        "--no-session-persistence",
        "--permission-mode", "bypassPermissions",
    ]
    if allow_read:
        cmd += ["--allowedTools", "Read"]
    for attempt in range(3):
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.stdout.strip():
            return result.stdout.strip()
        # Empty response — back off and retry
        wait = 10 * (attempt + 1)
        print(f"\n    [rate limit? retrying in {wait}s]", end="", flush=True)
        time.sleep(wait)
    return ""


def parse_json(raw: str) -> dict:
    raw = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {}  # empty = classification failed, leave file in Inbox


def classify_file(path: Path, name_only: bool = False) -> dict:
    instructions = NAME_ONLY_INSTRUCTIONS if name_only else CLASSIFY_INSTRUCTIONS
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text = extract_pdf_text(path)
        if len(text) < 20:
            user_msg = (
                f"{instructions}\n\n"
                f"Filename: {path.name}\n[PDF has no extractable text — name by filename only]"
            )
        else:
            user_msg = (
                f"{instructions}\n\n"
                f"Filename: {path.name}\n\nDocument text:\n{text}"
            )
        raw = run_claude(user_msg, allow_read=False)
    else:
        user_msg = (
            f"{instructions}\n\n"
            f"Read the file at this exact path and name it:\n{path}"
        )
        raw = run_claude(user_msg, allow_read=True, timeout=90)
    return parse_json(raw)


def generic_dest_folder(category: str, description: str = "") -> Path | None:
    """Determine destination for Generic inbox files based on Claude's category."""
    folder_name = CATEGORY_MAP.get(category.lower())
    if not folder_name:
        return None
    if folder_name == "Properties":
        desc_lower = description.lower()
        if "lindfield" in desc_lower:
            return FINANCE_DIR / "Properties" / "Lindfield"
        if "waterloo" in desc_lower or "rental statement" in desc_lower:
            return FINANCE_DIR / "Properties" / "Waterloo"
        return FINANCE_DIR / "Properties"
    return FINANCE_DIR / folder_name


def build_target_name(path: Path, description: str, category: str) -> str:
    """Build final filename: {description}_{date} or {sub-category}_{description}_{date}."""
    stem = path.stem
    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', stem)
    date_part = date_match.group(1) if date_match else "0000-00-00"
    clean_desc = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", description).strip()
    if CATEGORY_MAP.get(category.lower()) == "Spending":
        return f"{category.capitalize()}_{clean_desc}_{date_part}"
    return f"{clean_desc}_{date_part}"


def unique_target(directory: Path, stem: str, suffix: str) -> Path:
    candidate = directory / f"{stem}{suffix}"
    n = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{n}{suffix}"
        n += 1
    return candidate


ALLOWED_SUFFIXES = {".pdf", ".xlsx", ".xls", ".docx", ".doc"}


def collect_inbox_files() -> list[tuple[Path, dict | None]]:
    """Yield (file, rule_config_or_None) for every file in Inbox subfolders."""
    entries = []
    for subfolder in sorted(INBOX_DIR.iterdir()):
        if not subfolder.is_dir():
            continue
        rule_cfg = INBOX_RULE_CONFIG.get(subfolder.name)  # None = Generic or unknown
        for f in sorted(subfolder.iterdir()):
            if f.is_file() and f.suffix.lower() in ALLOWED_SUFFIXES:
                entries.append((f, rule_cfg))
    return entries


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Apply changes (default is dry run)")
    args = parser.parse_args()

    if not shutil.which("claude"):
        print("[ERROR] `claude` CLI not found in PATH.")
        sys.exit(1)

    entries = collect_inbox_files()
    if not entries:
        print("No files found in Inbox subfolders.")
        sys.exit(0)

    print(f"\nClassifying {len(entries)} files using Claude …\n")

    results = []
    for i, (f, rule_cfg) in enumerate(entries, 1):
        subfolder = f.parent.name
        name_only = rule_cfg is not None  # known source → naming only
        print(f"  [{i:3}/{len(entries)}] [{subfolder}] {f.name[:55]}", end=" … ", flush=True)
        try:
            info = classify_file(f, name_only=name_only)
        except subprocess.TimeoutExpired:
            info = {}
        except Exception:
            info = {}

        if name_only and info:
            # Merge fixed category/dest with Claude's description
            info["category"] = rule_cfg["category"]
            info["is_invoice"] = True
            info["_dest"] = rule_cfg["dest"]

        results.append((f, info, rule_cfg))

        if not info:
            label = "SKIP"
        elif info.get("is_invoice"):
            dest = info.get("_dest") or generic_dest_folder(
                info.get("category", ""), info.get("description", "")
            )
            label = str(dest.relative_to(FINANCE_DIR)) if dest else "DELETE"
        else:
            label = "DELETE"
        print(f"{label}  →  {info.get('description', '')}")
        time.sleep(2)

    # Summary table
    to_delete, to_move, to_skip = [], [], []
    print("\n" + "=" * 115)
    print(f"{'SUBFOLDER/FILE':<60} {'→ DEST':<28} {'NEW NAME':<30} REASON")
    print("=" * 115)
    for f, info, rule_cfg in results:
        rel_src = f"{f.parent.name}/{f.name[:45]}"
        if not info:
            to_skip.append(f)
            print(f"{rel_src:<60} {'SKIP (stays in Inbox)':<28} {'':30} failed")
            continue
        category = info.get("category", "other")
        description = info.get("description", "")
        dest = info.get("_dest") or generic_dest_folder(category, description)
        if info.get("is_invoice") and dest:
            new_stem = build_target_name(f, description, category)
            rel_dest = str(dest.relative_to(FINANCE_DIR))
            to_move.append((f, dest, category, description))
        elif any(kw in description.lower() for kw in PROTECTED_PROPERTY_DOCS):
            dest = FINANCE_DIR / "Properties"
            new_stem = build_target_name(f, description, "properties")
            rel_dest = str(dest.relative_to(FINANCE_DIR))
            to_move.append((f, dest, "properties", description))
        else:
            new_stem = ""
            rel_dest = "DELETE"
            to_delete.append(f)
        reason = info.get("reason", "")[:20]
        print(f"{rel_src:<60} {rel_dest:<28} {(new_stem + f.suffix)[:30]:<30} {reason}")
    print("=" * 115)
    print(f"\n  {len(to_move)} to move  |  {len(to_delete)} to delete  |  {len(to_skip)} skipped\n")

    if not args.execute:
        print("Dry run only. Run with --execute to apply.\n")
        return

    print("Applying changes …\n")
    for f in to_delete:
        f.unlink()
        print(f"  DELETED  {f.parent.name}/{f.name}")

    for f, dest, category, description in to_move:
        dest.mkdir(parents=True, exist_ok=True)
        new_stem = build_target_name(f, description, category)
        target = unique_target(dest, new_stem, f.suffix)
        shutil.move(str(f), str(target))
        print(f"  MOVED → {dest.relative_to(FINANCE_DIR)}/{target.name}")

    print(f"\nDone!  {len(to_move)} moved, {len(to_delete)} deleted, {len(to_skip)} left in Inbox.\n")


if __name__ == "__main__":
    main()
