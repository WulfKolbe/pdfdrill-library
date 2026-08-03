#!/usr/bin/env bash
#
# Bootstrap a fresh machine (Claude.ai cloud session, CI container, new laptop)
# so audit/soak_test.py can run against the corpus committed in this repo.
#
#   git clone https://github.com/WulfKolbe/pdfdrill-library
#   cd pdfdrill-library
#   ./audit/cloud_bootstrap.sh
#   ./audit/soak_test.py --hours 12
#
# What this can and cannot give you:
#
#   works    the introspection battery (size, pdfinfo, fonts, abstract, toc,
#            links, dests, bibtex, tables, identifiers) plus the invariant
#            oracles. That is the entire point of the soak test.
#
#   absent   MathPix/OpenAI routes (no API keys) and the LaTeX/SVG routes
#            unless you accept a ~4 GB TeX Live install. pdfdrill degrades
#            to a one-line hint for these rather than failing, and the soak
#            records them as refusals, not crashes.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PDFDRILL_SRC="${PDFDRILL_SRC:-$HOME/pdfdrill}"
WITH_TEX="${WITH_TEX:-0}"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "1/5  system packages"
if command -v apt-get >/dev/null 2>&1; then
    SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
    $SUDO apt-get update -qq || true
    # poppler-utils is the only hard requirement: pdfinfo/pdftotext/pdffonts
    # back size, pdfinfo, fonts, links and dests.
    $SUDO apt-get install -y -qq poppler-utils ghostscript >/dev/null || {
        echo "WARNING: apt install failed; continuing"; }
    if [ "$WITH_TEX" = "1" ]; then
        echo "installing TeX Live (large, several minutes) …"
        $SUDO apt-get install -y -qq texlive-latex-extra dvisvgm tesseract-ocr \
            >/dev/null || echo "WARNING: TeX/tesseract install failed"
    else
        echo "skipping TeX Live and tesseract (set WITH_TEX=1 to include)"
    fi
else
    echo "no apt-get; ensure poppler-utils is installed by other means"
fi

say "2/5  python dependencies"
python3 -m pip install --quiet --upgrade pip
python3 -m pip install --quiet \
    "pdfminer.six>=20221105" "pdfplumber>=0.11" "pydantic>=2.0" \
    "pypdf>=4.0" "PyYAML>=6.0"

say "3/5  pdfdrill itself"
if command -v pdfdrill >/dev/null 2>&1; then
    echo "pdfdrill already on PATH: $(command -v pdfdrill)"
else
    if [ ! -d "$PDFDRILL_SRC" ]; then
        git clone --depth 1 https://github.com/WulfKolbe/pdfdrill "$PDFDRILL_SRC"
    fi
    python3 -m pip install --quiet -e "$PDFDRILL_SRC"
fi

export PATH="$HOME/.local/bin:$PATH"
command -v pdfdrill >/dev/null 2>&1 || {
    echo "FATAL: pdfdrill still not on PATH."
    echo "Try: export PATH=\"\$HOME/.local/bin:\$PATH\""
    exit 2
}

say "4/5  environment check"
export PDFDRILL_NO_PREFLIGHT=1     # sanctioned automation path
pdfdrill doctor 2>&1 | tail -25 || true

say "5/5  smoke test"
FIRST="$(python3 - <<'PY'
import os,glob,sys
for d in sorted(os.listdir(".")):
    if os.path.isdir(d) and not d.startswith(".") and d not in {"audit","reports"}:
        p=sorted(glob.glob(os.path.join(d,"*.pdf")))
        if p: print(p[0]); sys.exit()
PY
)"
if [ -n "$FIRST" ]; then
    echo "probing: $FIRST"
    pdfdrill size "$FIRST" || echo "WARNING: smoke test failed"
else
    echo "WARNING: no PDFs found — is the corpus checked out?"
fi

DOCS=$(find . -mindepth 2 -maxdepth 2 -name '*.pdf' | wc -l)
cat <<EOF

------------------------------------------------------------------
ready — $DOCS documents available

  ./audit/soak_test.py --hours 12            # the 12 hour run
  ./audit/soak_test.py --hours 0.1 --limit 5 # quick sanity check
  ./audit/soak_test.py --hours 12 --resume   # continue an interrupted run

Remember to set PDFDRILL_NO_PREFLIGHT=1 in any shell you drive
pdfdrill from directly; the soak runner sets it for its children.
------------------------------------------------------------------
EOF
