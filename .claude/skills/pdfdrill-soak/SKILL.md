---
name: pdfdrill-soak
description: |
  Install pdfdrill on a fresh machine and run the long-running soak test
  against the document corpus in this repository, then write up and commit
  the results. Use when asked to run the soak test, the 12-hour test, the
  audit, or to check pdfdrill for regressions on a cloud session (claude.ai,
  Codespaces, CI) where nothing is installed yet.
allowed-tools: [Bash, Read, Write, Edit]
---

# pdfdrill soak test

Install everything, run a time-budgeted soak against the corpus, document
the result. The whole procedure is five steps and the only genuinely
tricky part is step 3: a 12-hour run cannot happen inside one tool call,
so it must be backgrounded and polled.

## What this test actually does

It is not a pass/fail test suite. It hammers `pdfdrill` against ~1380 real
PDFs and checks five **invariants** — properties pdfdrill is supposed to
guarantee. An invariant violation is a much stronger finding than a
non-zero exit code:

| Oracle | Broken means |
|---|---|
| `MONOTONIC` | a command **removed** facts from a sidecar — enrichment destroyed |
| `PDF_FROZEN` | the source PDF changed on disk — it is read-only input |
| `JSON_VALID` | the sidecar stopped parsing — this is how corrupt sidecars are born |
| `IDEMPOTENT` | running a command twice gave a different fact set |
| `NO_CRASH` | Python traceback — an unhandled exception |

A non-zero exit **without** a traceback is usually a legitimate refusal
(asking for the TOC of a document that has none). Do not report refusals
as bugs.

## Step 1 — check where you are

```bash
pwd && ls audit/ && find . -mindepth 2 -maxdepth 2 -name '*.pdf' | wc -l
```

You need `audit/soak_test.py` and a non-zero PDF count. If the count is 0
the corpus is not checked out — `git lfs` is *not* used here, so a plain
`git clone` should already have it.

## Step 2 — install everything

```bash
./audit/cloud_bootstrap.sh
```

This installs poppler + ghostscript, the five Python dependencies, clones
and pip-installs `pdfdrill` from https://github.com/WulfKolbe/pdfdrill,
runs `pdfdrill doctor`, and smoke-tests one document.

Add `WITH_TEX=1` only if the LaTeX/SVG routes matter — it pulls ~4 GB of
TeX Live and is rarely worth it for a soak run. Without it, those routes
degrade to a one-line hint and are recorded as refusals, not crashes.

If `pdfdrill` is still not found afterwards:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

**Always export `PDFDRILL_NO_PREFLIGHT=1`** in any shell where you call
`pdfdrill` by hand. pdfdrill hard-blocks its build commands behind an
attestation gate; this variable is the automation path its own SKILL
sanctions. The soak runner already sets it for its children.

## Step 3 — run it in the background

A 12-hour run will outlive any single tool call, so never run it in the
foreground. Background it and poll:

```bash
mkdir -p reports
nohup ./audit/soak_test.py --hours 12 > reports/soak-run.log 2>&1 &
echo "started pid $!"
```

Then poll every few minutes — do **not** block:

```bash
tail -20 reports/soak-run.log
```

Useful variants:

```bash
./audit/soak_test.py --hours 12 --resume     # continue an interrupted run
./audit/soak_test.py --hours 0.1 --limit 5   # 6-minute sanity check first
./audit/soak_test.py --hours 12 --no-deep    # skip model rebuilds (faster)
./audit/soak_test.py --hours 12 --workers 4  # small machine
```

**Run the sanity check first.** `--hours 0.1 --limit 5` takes six minutes
and catches a broken install before you commit to twelve hours.

If the session is likely to end before 12 hours, prefer a budget that fits
it (`--hours 3`) and use `--resume` next session. The checkpoint at
`reports/soak.jsonl` is written after every document, so nothing is lost
to an interruption.

### Choosing the budget

The scheduler **fills** the budget: it runs a light pass, an idempotency
pass, a medium pass, a sandboxed deep pass, and then repeats reshuffled
passes until the time is spent. So `--hours 12` really does take about
twelve hours, and a shorter budget is not a partial run — it is a complete
run with fewer repeats.

## Step 4 — monitor

Violations print live, prefixed `!!`:

```bash
grep '!!' reports/soak-run.log        # invariant violations so far
tail -5 reports/soak-run.log          # progress + budget remaining
wc -l reports/soak.jsonl              # probes recorded
```

Progress lines report percent-of-budget and time left. If violations
appear early, let the run continue — a longer run gives better evidence
of how widespread the problem is.

## Step 5 — document the results

When it finishes, `reports/soak-latest.md` holds the report and
`reports/soak-latest.json` the summary. Read the report and write up:

```bash
cat reports/soak-latest.md
```

Then commit. `git add -u` is safe here (tracked files only); never
`git add -A`, because the repository denies by default precisely so that
copyrighted books and personal scans cannot be committed by accident.

```bash
git add reports/soak-latest.md reports/soak-latest.json reports/history/
git add -u
git commit -m "soak $(date -I): <N> probes, <M> violations"
git push
```

`reports/soak.jsonl` and `reports/soak-run.log` are gitignored — they are
large and machine-local. Do not force-add them.

### What to tell the user

Lead with invariant violations and crashes; those are real bugs. Give,
for each:

- the oracle that broke and what that means in plain terms
- the document and command, so it is reproducible:
  `pdfdrill <cmd> "<document>"`
- how many documents share the signature — failures are grouped by a
  normalised error signature, so one bug across 40 documents is one
  finding, not forty

Report refusal and timeout counts as background context, not as defects.
State the actual elapsed time and probe count rather than the budget you
asked for; a run can stop early if interrupted.

Exit status is `1` when violations or crashes were found, `0` otherwise.

## Gotchas

- **Do not run the soak against a corpus you care about without reading
  this line.** The light and medium batteries are additive — they cache
  facts into sidecars and never remove them. The deep battery rebuilds
  models and therefore runs **only** inside a throwaway copy of each
  document folder. This is enforced in the code, not by convention.
- Never `curl`/`wget`/`tar` a PDF to "help" pdfdrill. Pass it the path.
- `--workers` defaults to CPU count minus 4. On a 2-core cloud box pass
  `--workers 2`, otherwise the machine will thrash.
- Commands run per document sequentially by design; the oracles compare
  sidecar state before and after each command and would race otherwise.
- A document whose sidecar is *already* unparseable will fail every
  command in the battery. That is one broken document, not twelve bugs —
  the report groups it accordingly.
