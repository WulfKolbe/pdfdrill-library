#!/usr/bin/env python3
"""
Select the documents that may be committed to the public repository.

Eligibility (ALL must hold):
  * the folder holds a PDF of at most --max-bytes (default 1 MB)
  * the folder is not a Z-Library copy of an in-copyright book
  * the folder is not a scan_* folder (personal scanned mail)

Rationale: GitHub strongly recommends repositories stay under 1 GB, and the
full library is 34 GB. Because books are large and scans are few, the 1 MB cut
removes almost all copyrighted and personal material on its own -- the explicit
name filters exist so the guarantee does not depend on a size coincidence.

Writes audit/corpus.txt (the manifest) and, with --add, stages exactly those
paths with `git add -f`, bypassing the deny-by-default .gitignore for them and
nothing else.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ZLIB_RE = re.compile(r"z-?lib", re.I)

# Sidecar-adjacent files worth committing with each document. Anything not
# listed here stays local, which keeps the repo to PDFs + small JSON.
KEEP_SUFFIXES = (".pdf", ".drill.json")


def eligible(folder: Path, max_bytes: int) -> tuple[bool, str]:
    name = folder.name
    if name.startswith("."):
        return False, "hidden"
    if name in {"audit", "reports", "node_modules"}:
        return False, "infrastructure"
    if ZLIB_RE.search(name):
        return False, "z-library (in-copyright book)"
    if name.startswith("scan_"):
        return False, "personal scan"

    pdfs = sorted(folder.glob("*.pdf"))
    if not pdfs:
        return False, "no pdf"
    biggest = max(p.stat().st_size for p in pdfs)
    if biggest > max_bytes:
        return False, "pdf over size limit"
    return True, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--max-bytes", type=int, default=1024 * 1024)
    ap.add_argument("--add", action="store_true", help="git add -f the selection")
    ap.add_argument("--limit", type=int, default=0, help="cap document count")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    chosen: list[Path] = []
    reasons: dict[str, int] = {}
    total = 0

    for entry in sorted(os.scandir(root), key=lambda e: e.name):
        if not entry.is_dir():
            continue
        folder = Path(entry.path)
        ok, why = eligible(folder, args.max_bytes)
        if not ok:
            reasons[why] = reasons.get(why, 0) + 1
            continue
        for f in sorted(folder.iterdir()):
            if f.is_file() and f.name.endswith(KEEP_SUFFIXES):
                chosen.append(f.relative_to(root))
                total += f.stat().st_size
        if args.limit and len({c.parts[0] for c in chosen}) >= args.limit:
            break

    docs = sorted({c.parts[0] for c in chosen})
    manifest = root / "audit" / "corpus.txt"
    manifest.write_text("\n".join(docs) + "\n", encoding="utf-8")

    print(f"eligible documents : {len(docs)}")
    print(f"files to commit    : {len(chosen)}")
    print(f"total size         : {total/1024**3:.2f} GB")
    print(f"manifest           : {manifest}")
    print("\nexcluded:")
    for why, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>5}  {why}")

    if total > 1024**3:
        print("\nWARNING: selection exceeds 1 GB, above GitHub's recommendation.",
              file=sys.stderr)

    if args.add:
        print(f"\nstaging {len(chosen)} files …")
        BATCH = 500
        for i in range(0, len(chosen), BATCH):
            batch = [str(p) for p in chosen[i:i + BATCH]]
            subprocess.run(["git", "add", "-f", "--", *batch],
                           cwd=root, check=True)
            print(f"  staged {min(i+BATCH, len(chosen))}/{len(chosen)}")
        subprocess.run(["git", "add", "audit/corpus.txt"], cwd=root, check=True)
        print("done")

    return 0


if __name__ == "__main__":
    sys.exit(main())
