#!/usr/bin/env bash
#
# Nightly audit driver: run the audit, commit the report, push it.
#
# Exit status is deliberately propagated from the audit program, so a night
# with regressions leaves a failed unit that `systemctl --failed` and the
# GitHub Actions UI will both show in red.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2

COHORT="${AUDIT_COHORT:-120}"
CANARIES="${AUDIT_CANARIES:-25}"
WORKERS="${AUDIT_WORKERS:-12}"
TIMEOUT="${AUDIT_TIMEOUT:-120}"
PUSH="${AUDIT_PUSH:-1}"

export PDFDRILL_NO_PREFLIGHT=1   # sanctioned automation path, see SKILL.md

echo "=== pdfdrill nightly audit $(date -Is) ==="
echo "root=$ROOT cohort=$COHORT workers=$WORKERS"

if ! command -v pdfdrill >/dev/null 2>&1; then
    echo "FATAL: pdfdrill not on PATH" >&2
    exit 2
fi

python3 audit/pdfdrill_audit.py \
    --root "$ROOT" \
    --cohort "$COHORT" \
    --canaries "$CANARIES" \
    --workers "$WORKERS" \
    --timeout "$TIMEOUT" \
    "$@"
AUDIT_RC=$?

echo "audit exit=$AUDIT_RC"

if [[ "$PUSH" == "1" ]] && [[ -d .git ]]; then
    DATE="$(date -I)"
    git add reports/latest.md reports/latest.json "reports/history/${DATE}.md" 2>/dev/null

    # The probes write cached facts back into the sidecars, so tracked
    # sidecars drift every night. Stage those too, which both keeps the tree
    # clean and turns `git log` into a record of how the corpus state moved.
    # `-u` only touches already-tracked files, so no PDF, book or scan can
    # enter the repository through this line.
    git add -u 2>/dev/null

    if git diff --cached --quiet; then
        echo "no report changes to commit"
    else
        SUMMARY="$(python3 -c '
import json,sys
try:
    d=json.load(open("reports/latest.json"))
except Exception:
    print("audit"); sys.exit()
bits=[]
if d.get("new_failures"):     bits.append(f"{d[\"new_failures\"]} new failures")
if d.get("fact_regressions"): bits.append(f"{d[\"fact_regressions\"]} fact regressions")
if d.get("corrupted"):        bits.append(f"{d[\"corrupted\"]} corrupted")
if d.get("vanished"):         bits.append(f"{d[\"vanished\"]} vanished")
print(", ".join(bits) if bits else "no regressions")
' 2>/dev/null || echo audit)"

        git commit -q -m "audit ${DATE}: ${SUMMARY}" \
                      -m "$(head -c 900 reports/latest.json)"
        echo "committed: audit ${DATE}: ${SUMMARY}"

        if git remote get-url origin >/dev/null 2>&1; then
            if git push -q origin HEAD 2>&1; then
                echo "pushed"
            else
                echo "WARNING: push failed (report is committed locally)" >&2
            fi
        fi
    fi
fi

exit "$AUDIT_RC"
