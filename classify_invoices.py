#!/usr/bin/env python3
"""
Invoice Classifier and Organiser
Uses the local `claude` CLI (your Claude.ai membership) to read and classify
each file in Inbox/Invoices, then offers a dry-run before:
  • Deleting non-invoices from Inbox/Invoices
  • Moving genuine invoices into Areas/Finance/ subfolders

Usage:
  python3 classify_invoices.py            # dry run — shows table only
  python3 classify_invoices.py --execute  # apply changes
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

INVOICES_DIR = Path.home() / "Library/CloudStorage/OneDrive-Personal/Inbox/Invoices"
FINANCE_DIR  = Path.home() / "Library/CloudStorage/OneDrive-Personal/Areas/Finance"

CATEGORY_MAP = {
    "car":            "Car",
    "properties":     "Properties",
    "medical":        "Medical",
    "insurance":      "Insurance",
    "superannuation": "Superannuation",
    "spending":       "Spending",
    "professional":   "Spending",
    "technology":     "Spending",
    "travel":         "Spending",
    "other":          "Spending",
    "delete":         None,
}

CLASSIFY_INSTRUCTIONS = """\
You are a document classifier. Respond ONLY with valid JSON — no markdown, no explanation.

Format: {"is_invoice": true/false, "category": "<category>", "reason": "<10 words max>"}

Categories (only when is_invoice is true):
- "car"            — vehicle purchase/service, fuel, car rental, auto parts
- "properties"     — real estate, rent, utilities, home maintenance, building reports
- "medical"        — health, dental, optical, pharmacy, physio, allied health, hospital
- "insurance"      — insurance premiums or claim receipts
- "superannuation" — super fund statements or contribution receipts
- "spending"       — general retail, food, software, tech, professional services, travel

Set is_invoice to false (category "delete") when the file is NOT a financial document:
product photos, run sheets, personal photos, email body screenshots, wedding event
documents that are not invoices/receipts, or rebate application forms without a
payment amount."""


def extract_pdf_text(path: Path) -> str:
    try:
        with pdfplumber.open(path) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages[:2])
            return text[:3000].strip()
    except Exception as e:
        return f"[PDF read error: {e}]"


def run_claude(prompt: str, allow_read: bool = False, timeout: int = 60) -> str:
    """Call `claude -p` and return stdout."""
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "text",
        "--no-session-persistence",
        "--permission-mode", "bypassPermissions",
    ]
    if allow_read:
        cmd += ["--allowedTools", "Read"]
    else:
        cmd += ["--tools", ""]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip()


def parse_json(raw: str) -> dict:
    # Strip markdown fences if present
    raw = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
    # Extract first JSON object
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {"is_invoice": True, "category": "spending", "reason": "parse error"}


def classify_file(path: Path) -> dict:
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        text = extract_pdf_text(path)
        if len(text) < 20:
            user_msg = (
                f"{CLASSIFY_INSTRUCTIONS}\n\n"
                f"Filename: {path.name}\n[PDF has no extractable text — classify by filename only]"
            )
        else:
            user_msg = (
                f"{CLASSIFY_INSTRUCTIONS}\n\n"
                f"Filename: {path.name}\n\nDocument text:\n{text}"
            )
        raw = run_claude(user_msg, allow_read=False)
    else:
        # Image — ask claude to Read it, then classify
        user_msg = (
            f"{CLASSIFY_INSTRUCTIONS}\n\n"
            f"Read the image file at this exact path and classify it:\n{path}"
        )
        raw = run_claude(user_msg, allow_read=True, timeout=90)

    return parse_json(raw)


def dest_folder(category: str) -> Path | None:
    folder_name = CATEGORY_MAP.get(category.lower())
    return FINANCE_DIR / folder_name if folder_name else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Apply changes (default is dry run)")
    args = parser.parse_args()

    # Verify claude CLI is available
    if not shutil.which("claude"):
        print("[ERROR] `claude` CLI not found in PATH.")
        sys.exit(1)

    files = sorted(
        f for f in INVOICES_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg"}
    )
    if not files:
        print("No files found in", INVOICES_DIR)
        sys.exit(0)

    print(f"\nClassifying {len(files)} files using Claude …\n")

    results = []
    for i, f in enumerate(files, 1):
        print(f"  [{i:3}/{len(files)}] {f.name[:70]}", end=" … ", flush=True)
        try:
            info = classify_file(f)
        except subprocess.TimeoutExpired:
            info = {"is_invoice": True, "category": "spending", "reason": "timeout"}
        except Exception as e:
            info = {"is_invoice": True, "category": "spending", "reason": f"error: {e}"}
        results.append((f, info))
        if info.get("is_invoice"):
            label = CATEGORY_MAP.get(info.get("category", "").lower(), "Spending") or "Spending"
        else:
            label = "DELETE"
        print(label)
        time.sleep(0.1)

    # Summary table
    to_delete, to_move = [], []
    print("\n" + "=" * 95)
    print(f"{'FILE':<65} {'ACTION':<14} REASON")
    print("=" * 95)
    for f, info in results:
        folder = dest_folder(info.get("category", "spending"))
        if info.get("is_invoice") and folder:
            action = f"→ {folder.name}"
            to_move.append((f, folder))
        else:
            action = "DELETE"
            to_delete.append(f)
        reason = info.get("reason", "")[:30]
        print(f"{f.name[:65]:<65} {action:<14} {reason}")
    print("=" * 95)
    print(f"\n  {len(to_move)} to move   |   {len(to_delete)} to delete\n")

    if not args.execute:
        print("Dry run only. Run with --execute to apply.\n")
        return

    # Execute
    print("Applying changes …\n")
    (FINANCE_DIR / "Medical").mkdir(exist_ok=True)

    for f in to_delete:
        f.unlink()
        print(f"  DELETED  {f.name}")

    for f, dest in to_move:
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / f.name
        n = 2
        while target.exists():
            target = dest / f"{f.stem}_{n}{f.suffix}"
            n += 1
        shutil.move(str(f), str(target))
        print(f"  MOVED → {dest.name}/  {f.name}")

    print(f"\nDone!  {len(to_move)} moved, {len(to_delete)} deleted.\n")


if __name__ == "__main__":
    main()
