# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

This is **not a source repository** — it is the `pdfdrill` **library root**: a 46 GB
corpus of ~3320 drilled PDF documents. It is registered as such in
`~/.config/pdfdrill/config.json` (`library_root: /home/wkolbe/pdfdrill-library`),
which is what makes `pdfdrill relocate` move drills here. There is no `.git` here,
no build, no test suite. The "code" that operates on this tree is the external
`pdfdrill` CLI (`/home/wkolbe/.local/bin/pdfdrill`), installed outside this directory.

Work here means **running `pdfdrill` against documents**, not editing files.

## MANDATORY: preflight before any pdfdrill build/extract command

pdfdrill hard-blocks build/extract commands (`model`, `mathpix`, `latex`,
`tiddlers`, `semantic`, `relocate`, `make`, …) until you attest you read its SKILL.
Read-only commands (`size`, `pdfinfo`, `status`, `doctor`, `config`, `steps`,
`plan`, `artifacts`, `ls`) stay open.

```bash
pdfdrill skill --emit /tmp/pdfdrill-skill   # emit SKILL.md + commands.yaml
# read SKILL.md to its LAST line, which carries the token
pdfdrill preflight --ack DRILL-xxxxxxxx     # token is a checksum of that file — re-read it, don't reuse this one
```

The token changes whenever the SKILL changes; always re-derive it from a fresh
`--emit`. Trusted automation may set `PDFDRILL_NO_PREFLIGHT=1`.

## Library layout — one self-contained folder per document

```
<library-root>/<stem>/
    <stem>.pdf                    # the document itself
    <stem>.drill.json             # THE SIDECAR — state machine + facts + evidence
    <stem>.lines.json             # MathPix/OCR line geometry (input to `model`)
    <stem>.tiddlers.json          # TiddlyWiki projection
    <stem>.inspect.html           # docmodel inspector
    <stem>.llm.txt / .md / .tex   # LLM/markdown/LaTeX projections
    model.docmodel.json           # the unified model (can be 50 MB+)
    rasterize/ inspect/ visionocr/ images_extracted/   # blob subfolders
```

The folder name is the PDF stem; the `bibkey` (tiddler prefix) normally equals it.
This flattened layout is what `pdfdrill relocate` produces from legacy scattered
drills (`X.pdf.drill.json` → `X.drill.json`, `X.pdf.drill/` blobs hoisted).

### The sidecar is the state

`<stem>.drill.json` holds `facts` (a set of capability flags), `evidence`, per-layer
payloads, and a `transitions` log. Everything pdfdrill knows accumulates there, so
repeated calls never redo work. Typical fact progression:

`SIZE_KNOWN` → `MATHPIX_KNOWN` / `OCR_BUILT` / `LATEX_INGESTED` → `MODEL_BUILT`
→ `BIBLIOGRAPHY_BUILT` → `TIDDLERS_BUILT` → `SEMANTIC_BUILT` / `REPORT_BUILT`

**Current corpus state (3310 sidecars): 2863 hold only `SIZE_KNOWN`** — the library is
overwhelmingly triaged-but-not-drilled. 420 have `MODEL_BUILT`, 243 `TIDDLERS_BUILT`,
34 `SEMANTIC_BUILT`; a handful are fully enriched (e.g.
`The_C___Programming_Language__Special_Edition__Bjarne_Stroustrup__Addison-Wesley__2000_/`).
17 sidecars fail to parse as JSON and 1 has an empty fact set. Assume a document is
*not* drilled until `pdfdrill status` says otherwise.

`MODEL_BUILT` alone is not enough — a model can be a different *species* (geometry-only
vs. real math). Trust `pdfdrill status`, and treat `NEEDS_VISION_OCR` as "the equations
are garbled; run `mathpix` or `visionocr`".

## Common commands

```bash
pdfdrill doctor                     # system tools / Python deps / API keys check
pdfdrill config --json              # confirm library_root + download_dir
pdfdrill status <stem>/<stem>.pdf   # what is already known about one document
pdfdrill artifacts <pdf>            # openable outputs in this doc's folder
pdfdrill steps <cmd> <pdf>          # prerequisite chain for a command
pdfdrill plan <pdf> "question"      # what would need to run to answer this

pdfdrill ls <dir>                   # shallow triage over a folder (pdfinfo → sidecars)
pdfdrill folder <dir>               # build full structure for every PDF in a folder
pdfdrill selftest <pdf-or-dir>      # diagnostic grid of commands → selftest.log

pdfdrill model <pdf>                # build the unified docmodel
pdfdrill make <pdf> <goal>          # plan + execute to reach a capability (clobber-checked)
pdfdrill retrieve <pdf> "question"  # grounded top-k context — the practical Q&A path
```

`pdfdrill ask` is the *strict verified-only* mode and usually withholds on a fresh
model; use `retrieve` + synthesis for normal question answering.

## Hard rules when operating on this library

- **Never `curl`/`wget`/`tar`/`unzip` a PDF or arXiv e-print, and never hand-parse
  `.tex`/`.tgz`.** Pass the path, https URL, or bare arXiv id as `<pdf>`; pdfdrill does
  the acquisition, caching, macro expansion, and decides *if* the LaTeX source is used.
  Downloads land in `~/Downloads` (`download_dir`), drills land here.
- **One command per step.** Let pdfdrill resolve prerequisites (`steps`, `--ensure`,
  `make`); do not hand-chain a pipeline.
- **Start shallow, escalate only when the question demands it** — `size`, `pdfinfo`,
  `links`, `abstract`, `toc` before any model build.
- **Read what pdfdrill writes** (`llmtext`, `report`, `tables`, `md`) from the doc's
  folder; do not re-extract by hand.
- **Never cat/Read the big model JSON.** `model.docmodel.json` runs to tens of MB and
  one folder holds a 686 MB `chars.json`. Use `pdfdrill artifacts` / `fetch` /
  `retrieve` instead. Same for `find`/`ls` at the root: 3320 entries, 46 GB.
- **`make`/`plan` refuse a plan that would rebuild a model and destroy a held
  enrichment** (e.g. `LATEX_INGESTED`). If a command is refused, that is the reason —
  do not force it.

## Loose ends in this tree

- `Zwiebeln.{pdf,md,lines.json,tex.zip}` sit un-relocated at the root — a legacy
  scattered drill. `pdfdrill relocate Zwiebeln.pdf` (dry-run) then `--apply` folds it
  into `Zwiebeln/`. It is collision-safe and idempotent.
- `pdfdrill-downloads.json` at the root is the URL → `{filename, sha256, bytes,
  downloaded_at}` download ledger, keyed by source URL. Use it to check whether a
  paper was already fetched before downloading again.
- `resume.sh` is a one-liner: `claude --dangerously-skip-permissions`.
- 9 folders hold a PDF with no `.drill.json` yet (mostly Z-Library books with
  non-ASCII names) — these have never been triaged.

## CodeGraph

A CodeGraph MCP index exists at `.codegraph/`, and `.cursor/rules/codegraph.mdc`
carries the standard usage guide. Note that this tree holds almost no source code, so
codegraph has very little to index here — its value is limited in this directory.
