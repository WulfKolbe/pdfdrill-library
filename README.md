# pdfdrill-library

A regression-audit harness for [`pdfdrill`](https://github.com/WulfKolbe), run
nightly against a large corpus of real PDF documents.

The question this answers is narrow and practical: **what stopped working since
yesterday?** Every night a rotating cohort of documents is put through a battery
of `pdfdrill` commands, the whole corpus is swept for data loss, and the result
is diffed against the previous night. The report leads with the only category
that matters for that question — probes that passed yesterday and fail today.

## What is in this repository

| Path | What it is |
|---|---|
| `audit/pdfdrill_audit.py` | The audit program (sweep, probe, diff, report) |
| `audit/select_corpus.py` | Decides which documents may be committed |
| `audit/run_nightly.sh` | Nightly driver: audit → commit → push |
| `audit/systemd/` | Timer + service unit for the nightly run |
| `.github/workflows/` | Equivalent workflow for a self-hosted runner |
| `reports/latest.md` | Most recent audit report |
| `reports/history/` | One report per night — the day-over-day record |
| `audit/corpus.txt` | Manifest of the committed test documents |

### What is *not* in this repository

The local library is 34 GB across 3318 documents. This repo contains a
**1380-document, 0.59 GB subset**. Documents are committed only if the PDF is
at most 1 MB, the folder is not a Z-Library copy of an in-copyright book, and
it is not a `scan_*` folder of personal mail. `.gitignore` denies everything by
default and `audit/select_corpus.py` stages the eligible files explicitly, so
an accidental `git add -A` cannot publish a book or a scan.

The nightly audit still runs against the **full local corpus**, not just the
committed subset. The subset exists so the harness is reproducible by anyone
who clones the repo.

## The two tiers

**Tier A — corpus sweep.** Every sidecar in the library is parsed and its fact
set recorded. Pure file reads; no `pdfdrill`, no mutation. This catches
corrupted sidecars, documents that disappeared, and **fact regressions** — a
document that held `MODEL_BUILT` yesterday and does not today has lost real
work, which is the most expensive failure mode in a library this size.

**Tier B — command probes.** A cohort of ≥100 documents is run through a
battery of `pdfdrill` commands, capturing exit code, stderr and duration per
`(document, command)` pair.

Cohort selection is deliberate:

- **Canaries** (25/night) — the documents with the richest fact sets, always
  included. They are the only ones that exercise the deep code paths.
- **Rotation** (95/night) — least-recently-audited first, so the full corpus is
  covered roughly every 28 days rather than testing the same 100 documents
  forever.

## Safety

The default battery contains only introspection commands (`size`, `pdfinfo`,
`fonts`, `abstract`, `toc`, `links`, `dests`, `bibtex`, `images`, `status`,
`artifacts`). These write cached results into the sidecar, but facts only ever
**grow** — the transitions log shows additions, never removals — so they cannot
clobber an enrichment like `LATEX_INGESTED`.

Commands that rebuild a model (`model`, `mathpix`, `latex`, `tiddlers`,
`semantic`) are **never** run against the live corpus. `--deep` runs them
inside a throwaway copy of the document folder instead.

The runner sets `PDFDRILL_NO_PREFLIGHT=1`, which is the automation path
sanctioned by pdfdrill's own SKILL.

## Reading the report

```
## 🔴 REGRESSIONS FOUND
- New failures since last run: 14
- Documents that lost facts: 0
- pdfdrill version changed: 0.4.0 -> 0.4.1
```

Failures are **grouped by error signature**, not listed per document. Paths,
line numbers and digits are normalised away, so seventeen documents failing on
one bug collapse into a single entry with a reproduce line, instead of a wall
of seventeen. A version change is called out because it is the most likely
explanation for a batch of new failures appearing overnight.

Categories: 🔴 new failures / fact regressions / corruption / vanished ·
🟢 fixed · 🟡 slowdowns (>3× and >2 s) · ⚪ pre-existing failures, which are
listed but do **not** fail the run — they did not break today.

## Running it

```bash
# tonight's audit, 120 documents
./audit/run_nightly.sh

# pick a cohort without probing
python3 audit/pdfdrill_audit.py --dry-run --cohort 120

# include mutating commands, sandboxed in a copy
./audit/run_nightly.sh --deep

# audit only, no commit or push
AUDIT_PUSH=0 ./audit/run_nightly.sh
```

Exit status is `0` when nothing regressed and `1` when it did, so a bad night
leaves a failed unit.

## Scheduling

**Recommended — systemd timer.** Runs on the machine where the corpus, the
toolchain and the API keys already live.

```bash
mkdir -p ~/.config/systemd/user
cp audit/systemd/pdfdrill-audit.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pdfdrill-audit.timer
systemctl --user list-timers pdfdrill-audit    # confirm next run
sudo loginctl enable-linger "$USER"            # run even when logged out
```

`Persistent=true` means a night missed because the machine was off runs at the
next boot, which a plain crontab entry would silently skip.

**Alternative — self-hosted GitHub Actions runner.** Same machine, same speed,
plus the Actions UI and failure emails. See the header of
`.github/workflows/nightly-audit.yml`.

**Not viable — GitHub-hosted runners or Codespaces.** Not a tuning problem: a
hosted runner has 14 GB of disk against a 34 GB corpus that exists only on your
machine, would reinstall ~4 GB of TeX Live and friends every run, and has none
of the API keys. The slowness you saw in Codespaces understates it — the job
structurally cannot run there.

## Cost of a run

On a 16-core machine, 120 documents × 12 commands ≈ 1440 probes in about
6 minutes at 12 workers. The Tier A sweep over all 3318 documents takes a few
seconds. The audit is niced and IO-idle so it stays out of the way.
