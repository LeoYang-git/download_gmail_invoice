#!/usr/bin/env bash
SCRIPT_DIR="/Users/leo/dev/bulk_download_gmail_invoice"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"
LOG_DIR="$SCRIPT_DIR/logs"
LOG="$LOG_DIR/monthly_run.log"
LAST_RUN_FILE="$LOG_DIR/last_run_month.txt"
CURRENT_MONTH=$(date +"%Y-%m")

mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# Skip if already ran this month
if [ -f "$LAST_RUN_FILE" ] && [ "$(cat "$LAST_RUN_FILE")" = "$CURRENT_MONTH" ]; then
    log "Already ran for $CURRENT_MONTH — skipping."
    exit 0
fi

# Skip if no internet (will retry on next Mac startup or scheduled trigger)
if ! curl -s --max-time 10 https://www.google.com > /dev/null 2>&1; then
    log "No internet connection — will retry on next startup."
    exit 1
fi

log "========================================"
log "Monthly run starting for $CURRENT_MONTH"
log "========================================"

log "--- Step 1: Fetch Gmail documents ---"
"$PYTHON" "$SCRIPT_DIR/fetch_gmail_docs.py" >> "$LOG" 2>&1
FETCH_EXIT=$?
log "fetch_gmail_docs.py exited: $FETCH_EXIT"

log "--- Step 2: Classify and file documents ---"
"$PYTHON" "$SCRIPT_DIR/classify_docs.py" --execute >> "$LOG" 2>&1
CLASSIFY_EXIT=$?
log "classify_docs.py exited: $CLASSIFY_EXIT"

if [ $FETCH_EXIT -eq 0 ] && [ $CLASSIFY_EXIT -eq 0 ]; then
    echo "$CURRENT_MONTH" > "$LAST_RUN_FILE"
    log "Run completed successfully. Recorded $CURRENT_MONTH."
else
    log "Run finished with errors (fetch=$FETCH_EXIT, classify=$CLASSIFY_EXIT). Will retry next month."
fi

log "========================================"
