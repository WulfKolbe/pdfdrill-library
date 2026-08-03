#!/usr/bin/env python3
"""
pdfdrill nightly audit — detect functionality lost since the previous run.

Two tiers run every night:

  Tier A  corpus sweep    every sidecar in the library is parsed and its fact
                          set recorded. Pure file reads, no pdfdrill, no
                          mutation. Catches corruption, vanished documents and
                          FACT REGRESSIONS (a document that held MODEL_BUILT
                          yesterday and does not today = data loss).

  Tier B  command probes  a rotating cohort of >=100 documents is put through a
                          battery of pdfdrill commands. Exit code, stderr and
                          duration are captured per (document, command).

The run is then diffed against the previous run's state. The report leads with
NEW failures -- probes that passed yesterday and fail today -- because that is
the only category that means "something broke in the last day".

SAFETY: the default battery contains only introspection commands. Those write
cached results back into the sidecar (facts only ever GROW, see the transitions
log), so they can never clobber an enrichment like LATEX_INGESTED. Commands
that rebuild a model (model/mathpix/latex/tiddlers/semantic) are NEVER run
against the live corpus; --deep runs them inside a throwaway copy of the
document folder instead.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

# Introspection only. Every one of these is additive to the sidecar.
DEFAULT_BATTERY = [
    "size",
    "pdfinfo",
    "status",
    "fonts",
    "fonts_layer",
    "abstract",
    "toc",
    "links",
    "dests",
    "bibtex",
    "images",
    "artifacts",
]

# Heavier commands, only ever executed against a COPY of the document folder.
DEEP_BATTERY = [
    "tables",
    "identifiers",
    "booktoc",
    "eqblobs",
    "model",
    "llmtext",
]

# Documents excluded from the committed corpus; still audited locally.
EXCLUDE_DIR_RE = re.compile(r"z-?lib", re.I)


def is_sensitive(name: str) -> bool:
    return bool(EXCLUDE_DIR_RE.search(name)) or name.startswith("scan_")


# --------------------------------------------------------------------------
# result model
# --------------------------------------------------------------------------


@dataclass
class Probe:
    doc: str
    cmd: str
    status: str          # ok | fail | timeout | crash
    exit_code: int
    duration: float
    signature: str = ""  # normalised error identity, "" when ok
    detail: str = ""     # first useful line of the error


@dataclass
class DocState:
    doc: str
    pdf: str | None
    sidecar_ok: bool
    facts: list[str] = field(default_factory=list)
    sidecar_bytes: int = 0
    error: str = ""


@dataclass
class RunState:
    started: str
    finished: str = ""
    pdfdrill_version: str = ""
    host: str = ""
    cohort: list[str] = field(default_factory=list)
    docs: dict = field(default_factory=dict)     # doc -> DocState
    probes: dict = field(default_factory=dict)   # "doc\x00cmd" -> Probe
    last_audited: dict = field(default_factory=dict)  # doc -> iso date
    corpus_total: int = 0


# --------------------------------------------------------------------------
# error signatures
#
# Grouping failures by a normalised signature is what turns a wall of 200
# failing probes into "one bug in bibtex.py". Paths, line numbers, addresses
# and digits are stripped so the same defect collapses to one key.
# --------------------------------------------------------------------------

_NOISE = [
    (re.compile(r'File "[^"]+"'), 'File "..."'),
    (re.compile(r"0x[0-9a-fA-F]+"), "0xADDR"),
    (re.compile(r"/[\w./+-]{4,}"), "/PATH"),
    (re.compile(r"\b\d+\b"), "N"),
    (re.compile(r"\s+"), " "),
]


def normalise(text: str) -> str:
    for pat, rep in _NOISE:
        text = pat.sub(rep, text)
    return text.strip()[:200]


def extract_error(stderr: str, stdout: str) -> tuple[str, str]:
    """Return (signature, human-readable detail)."""
    blob = (stderr or "").strip() or (stdout or "").strip()
    if not blob:
        return "", ""
    lines = [l.rstrip() for l in blob.splitlines() if l.strip()]
    if not lines:
        return "", ""

    # A Python traceback: the final "SomeError: msg" line is the identity.
    for line in reversed(lines):
        if re.match(r"^[A-Za-z_][\w.]*(Error|Exception|Warning)\b", line.strip()):
            detail = line.strip()
            return hashlib.sha1(normalise(detail).encode()).hexdigest()[:12], detail[:300]

    detail = lines[-1].strip()
    return hashlib.sha1(normalise(detail).encode()).hexdigest()[:12], detail[:300]


# --------------------------------------------------------------------------
# corpus discovery (tier A)
# --------------------------------------------------------------------------


def sweep_corpus(root: Path) -> dict[str, DocState]:
    docs: dict[str, DocState] = {}
    for entry in sorted(os.scandir(root), key=lambda e: e.name):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if entry.name in {"audit", "reports", "node_modules"}:
            continue
        d = Path(entry.path)
        pdfs = sorted(d.glob("*.pdf"))
        sidecars = sorted(d.glob("*.drill.json"))
        pdf = None
        if pdfs:
            # prefer the one whose stem matches the folder
            match = [p for p in pdfs if p.stem == entry.name]
            pdf = str((match or pdfs)[0].relative_to(root))

        if not sidecars:
            docs[entry.name] = DocState(entry.name, pdf, False, error="no sidecar")
            continue

        sc = sidecars[0]
        try:
            size = sc.stat().st_size
            with open(sc, "rb") as fh:
                data = json.load(fh)
            facts = sorted(data.get("facts") or [])
            docs[entry.name] = DocState(entry.name, pdf, True, facts, size)
        except Exception as exc:  # unparseable / truncated sidecar
            docs[entry.name] = DocState(
                entry.name, pdf, False, [], sc.stat().st_size if sc.exists() else 0,
                f"{type(exc).__name__}: {exc}"[:200],
            )
    return docs


# --------------------------------------------------------------------------
# cohort selection (tier B)
#
# canaries  : the richest documents, always audited, because they are the only
#             ones that exercise the deep code paths (semantic, tiddlers, ...)
# rotation  : least-recently-audited first, so the whole corpus is covered over
#             time rather than the same 100 documents every night
# --------------------------------------------------------------------------


def choose_cohort(docs: dict[str, DocState], last: dict[str, str],
                  size: int, canary_n: int) -> list[str]:
    usable = [d for d, s in docs.items() if s.pdf]

    canary_n = min(canary_n, size)   # a small --cohort must not be overrun
    ranked = sorted(usable, key=lambda d: (-len(docs[d].facts), d))
    canaries = [d for d in ranked if len(docs[d].facts) > 1][:canary_n]

    rest = [d for d in usable if d not in set(canaries)]
    # never audited sorts first (empty string), then oldest date
    rest.sort(key=lambda d: (last.get(d, ""), d))

    cohort = canaries + rest[: max(0, size - len(canaries))]
    return cohort


# --------------------------------------------------------------------------
# probe execution
# --------------------------------------------------------------------------


def run_probe(root: Path, doc: str, pdf: str, cmd: str, timeout: int,
              binary: str) -> Probe:
    env = dict(os.environ)
    env["PDFDRILL_NO_PREFLIGHT"] = "1"   # sanctioned automation path
    env.setdefault("PYTHONUNBUFFERED", "1")

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [binary, cmd, pdf],
            cwd=root, env=env, timeout=timeout,
            capture_output=True, text=True, errors="replace",
        )
    except subprocess.TimeoutExpired:
        return Probe(doc, cmd, "timeout", -9, float(timeout),
                     f"timeout-{cmd}", f"exceeded {timeout}s")
    except Exception as exc:
        return Probe(doc, cmd, "crash", -1, time.monotonic() - t0,
                     "spawn", f"{type(exc).__name__}: {exc}"[:200])

    dur = time.monotonic() - t0
    if proc.returncode == 0:
        return Probe(doc, cmd, "ok", 0, dur)

    sig, detail = extract_error(proc.stderr, proc.stdout)
    # A traceback means the tool broke; a clean non-zero exit is a refusal.
    status = "crash" if "Traceback" in (proc.stderr or "") else "fail"
    return Probe(doc, cmd, status, proc.returncode, dur, sig or cmd, detail)


def run_deep_probe(root: Path, doc: str, cmd: str, timeout: int,
                   binary: str) -> Probe:
    """Run a mutating command inside a throwaway copy of the document folder."""
    src = root / doc
    with tempfile.TemporaryDirectory(prefix="pdfdrill-audit-") as tmp:
        dst = Path(tmp) / doc
        try:
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
                "*.docmodel.json", "*.docpack.json", "chars.json",
                "rasterize", "inspect", "visionocr", "viewer", "images_extracted"))
        except Exception as exc:
            return Probe(doc, cmd, "crash", -1, 0.0, "copy",
                         f"copy failed: {exc}"[:200])
        pdfs = sorted(dst.glob("*.pdf"))
        if not pdfs:
            return Probe(doc, cmd, "ok", 0, 0.0)  # nothing to probe
        return run_probe(Path(tmp), doc, str(pdfs[0].relative_to(tmp)),
                         cmd, timeout, binary)


# --------------------------------------------------------------------------
# diffing
# --------------------------------------------------------------------------


def diff_runs(prev: RunState | None, cur: RunState) -> dict:
    out = {
        "new_failures": [], "fixed": [], "still_failing": [],
        "fact_regressions": [], "corrupted": [], "repaired": [],
        "vanished": [], "added": [], "slowdowns": [],
        "version_changed": None,
    }

    cur_bad = {k: p for k, p in cur.probes.items() if p["status"] != "ok"}

    if prev is None:
        out["still_failing"] = list(cur_bad.values())
        return out

    if prev.pdfdrill_version and prev.pdfdrill_version != cur.pdfdrill_version:
        out["version_changed"] = f"{prev.pdfdrill_version} -> {cur.pdfdrill_version}"

    prev_probes = prev.probes
    for key, p in cur.probes.items():
        old = prev_probes.get(key)
        if p["status"] != "ok":
            if old is None:
                out["still_failing"].append(p)      # not seen before, no claim
            elif old["status"] == "ok":
                out["new_failures"].append(p)       # <-- regression
            else:
                out["still_failing"].append(p)
        else:
            if old is not None and old["status"] != "ok":
                out["fixed"].append(p)
            elif old is not None and old["duration"] > 1.0 \
                    and p["duration"] > 3 * old["duration"] and p["duration"] > 2.0:
                out["slowdowns"].append(
                    {**p, "was": round(old["duration"], 2)})

    # corpus-level drift over ALL documents, not just the cohort
    for doc, s in cur.docs.items():
        old = prev.docs.get(doc)
        if old is None:
            out["added"].append(doc)
            continue
        lost = set(old["facts"]) - set(s["facts"])
        if lost:
            out["fact_regressions"].append(
                {"doc": doc, "lost": sorted(lost), "kept": s["facts"]})
        if old["sidecar_ok"] and not s["sidecar_ok"]:
            out["corrupted"].append({"doc": doc, "error": s["error"]})
        if not old["sidecar_ok"] and s["sidecar_ok"]:
            out["repaired"].append(doc)

    for doc in prev.docs:
        if doc not in cur.docs:
            out["vanished"].append(doc)

    return out


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def group_by_signature(probes: list[dict]) -> list[tuple[str, str, list[dict]]]:
    buckets: dict[str, list[dict]] = {}
    for p in probes:
        buckets.setdefault(p["signature"] or p["cmd"], []).append(p)
    ranked = sorted(buckets.items(), key=lambda kv: -len(kv[1]))
    return [(sig, items[0].get("detail", ""), items) for sig, items in ranked]


def write_report(cur: RunState, diff: dict, path: Path, elapsed: float) -> str:
    docs = cur.docs
    probes = list(cur.probes.values())
    ok = sum(1 for p in probes if p["status"] == "ok")
    bad = len(probes) - ok
    parse_fail = sum(1 for s in docs.values() if not s["sidecar_ok"])

    new_f = diff["new_failures"]
    regressions = diff["fact_regressions"]
    corrupted = diff["corrupted"]

    verdict_bad = bool(new_f or regressions or corrupted or diff["vanished"])
    verdict = "REGRESSIONS FOUND" if verdict_bad else "no new failures"

    L: list[str] = []
    A = L.append
    A(f"# pdfdrill nightly audit — {cur.started[:10]}")
    A("")
    A(f"## {'🔴' if verdict_bad else '🟢'} {verdict}")
    A("")
    A(f"- **New failures since last run:** {len(new_f)}")
    A(f"- **Documents that lost facts:** {len(regressions)}")
    A(f"- **Sidecars newly corrupted:** {len(corrupted)}")
    A(f"- **Documents vanished:** {len(diff['vanished'])}")
    if diff["version_changed"]:
        A(f"- **pdfdrill version changed:** `{diff['version_changed']}` "
          f"— regressions below are most likely explained by this")
    A("")
    A(f"Ran {len(probes)} probes over {len(cur.cohort)} documents in "
      f"{elapsed/60:.1f} min. Corpus: {len(docs)} documents "
      f"({parse_fail} unreadable sidecars). pdfdrill {cur.pdfdrill_version}.")
    A("")

    if new_f:
        A("## 🔴 New failures — these broke in the last day")
        A("")
        for sig, detail, items in group_by_signature(new_f):
            cmds = sorted({i["cmd"] for i in items})
            # One broken document fails every command in the battery, so report
            # distinct documents rather than raw probe count.
            by_doc: dict[str, list[dict]] = {}
            for i in items:
                by_doc.setdefault(i["doc"], []).append(i)
            A(f"### `{', '.join(cmds)}` — {len(by_doc)} document(s), "
              f"{len(items)} probe(s) — `{sig}`")
            A("")
            if detail:
                A(f"> {detail}")
                A("")
            for doc, ps in list(by_doc.items())[:10]:
                verb = ps[0]["status"]
                extra = f" ×{len(ps)} commands" if len(ps) > 1 else ""
                A(f"- `{doc}` (exit {ps[0]['exit_code']}, {verb}{extra})")
            if len(by_doc) > 10:
                A(f"- …and {len(by_doc) - 10} more documents")
            A("")
            first_doc, first_ps = next(iter(by_doc.items()))
            A(f"Reproduce: `pdfdrill {first_ps[0]['cmd']} \"{first_doc}\"`")
            A("")

    if regressions:
        A("## 🔴 Fact regressions — enrichment was destroyed")
        A("")
        A("A document that held a fact yesterday and does not today has lost "
          "real work. This is the most expensive failure mode in the library.")
        A("")
        A("| Document | Lost |")
        A("|---|---|")
        for r in regressions[:40]:
            A(f"| `{r['doc']}` | {', '.join(r['lost'])} |")
        if len(regressions) > 40:
            A(f"| …{len(regressions)-40} more | |")
        A("")

    if corrupted:
        A("## 🔴 Newly corrupted sidecars")
        A("")
        for c in corrupted[:30]:
            A(f"- `{c['doc']}` — {c['error']}")
        A("")

    if diff["vanished"]:
        A("## 🔴 Documents that disappeared")
        A("")
        for d in diff["vanished"][:30]:
            A(f"- `{d}`")
        A("")

    if diff["fixed"]:
        A(f"## 🟢 Fixed since last run ({len(diff['fixed'])})")
        A("")
        for sig, detail, items in group_by_signature(diff["fixed"])[:10]:
            cmds = sorted({i["cmd"] for i in items})
            A(f"- `{', '.join(cmds)}` now passes on {len(items)} document(s)")
        A("")

    if diff["slowdowns"]:
        A(f"## 🟡 Slowdowns ({len(diff['slowdowns'])})")
        A("")
        A("| Document | Command | Was | Now |")
        A("|---|---|---|---|")
        for s in sorted(diff["slowdowns"], key=lambda x: -x["duration"])[:15]:
            A(f"| `{s['doc']}` | `{s['cmd']}` | {s['was']}s | {s['duration']:.1f}s |")
        A("")

    still = diff["still_failing"]
    if still:
        A(f"## ⚪ Pre-existing failures ({len(still)}) — not new today")
        A("")
        A("| Command | Docs | Signature | Example |")
        A("|---|---|---|---|")
        for sig, detail, items in group_by_signature(still)[:25]:
            cmds = ", ".join(sorted({i["cmd"] for i in items}))
            A(f"| `{cmds}` | {len(items)} | `{sig}` | {(detail or '')[:80]} |")
        A("")

    A("## Coverage")
    A("")
    audited = len(cur.last_audited)
    A(f"- {audited}/{len(docs)} documents have been probed at least once "
      f"({100*audited/max(1,len(docs)):.0f}%)")
    A(f"- At {len(cur.cohort)} documents/night the full corpus is covered every "
      f"{max(1, round(len(docs)/max(1,len(cur.cohort))))} days")
    A(f"- Probe pass rate tonight: {ok}/{len(probes)} "
      f"({100*ok/max(1,len(probes)):.1f}%)")
    A("")

    text = "\n".join(L)
    path.write_text(text, encoding="utf-8")
    return text


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def detect_version(root: Path, docs: dict[str, DocState]) -> str:
    """Read pdfdrill_version off any healthy sidecar.

    A version change between runs is the single most likely explanation for a
    batch of new failures, so it is worth reporting even though it costs a
    couple of file reads.
    """
    for state in docs.values():
        if not state.sidecar_ok:
            continue
        for sc in (root / state.doc).glob("*.drill.json"):
            try:
                v = json.loads(sc.read_text()).get("pdfdrill_version")
            except Exception:
                break
            if v:
                return str(v)
            break
    return ""


def load_prev(p: Path) -> RunState | None:
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        return RunState(**d)
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent),
                    help="library root (default: repo root)")
    ap.add_argument("--cohort", type=int, default=120,
                    help="documents to probe tonight (default 120)")
    ap.add_argument("--canaries", type=int, default=25,
                    help="richest documents always included (default 25)")
    ap.add_argument("--workers", type=int, default=min(12, (os.cpu_count() or 4)),
                    help="parallel probes")
    ap.add_argument("--timeout", type=int, default=120, help="seconds per probe")
    ap.add_argument("--deep", action="store_true",
                    help="also run mutating commands in a throwaway copy")
    ap.add_argument("--binary", default="pdfdrill")
    ap.add_argument("--dry-run", action="store_true",
                    help="select the cohort and exit without probing")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    outdir = root / "reports"
    (outdir / "history").mkdir(parents=True, exist_ok=True)

    started = dt.datetime.now(dt.timezone.utc)
    t0 = time.monotonic()
    prev = load_prev(outdir / "state.json")

    print(f"[1/4] sweeping corpus at {root} …", flush=True)
    docs = sweep_corpus(root)
    print(f"      {len(docs)} documents, "
          f"{sum(1 for d in docs.values() if not d.sidecar_ok)} unreadable sidecars")

    last = dict(prev.last_audited) if prev else {}
    cohort = choose_cohort(docs, last, args.cohort, args.canaries)
    print(f"[2/4] cohort: {len(cohort)} documents "
          f"({sum(1 for d in cohort if len(docs[d].facts) > 1)} canaries)")

    if args.dry_run:
        for d in cohort:
            print("   ", d)
        return 0

    version = detect_version(root, docs)

    jobs = []
    for d in cohort:
        pdf = docs[d].pdf
        if not pdf:
            continue
        for cmd in DEFAULT_BATTERY:
            jobs.append(("live", d, pdf, cmd))
        if args.deep:
            for cmd in DEEP_BATTERY:
                jobs.append(("copy", d, pdf, cmd))

    print(f"[3/4] running {len(jobs)} probes with {args.workers} workers …",
          flush=True)
    probes: dict[str, dict] = {}
    done = 0
    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        futs = {}
        for mode, d, pdf, cmd in jobs:
            if mode == "live":
                f = pool.submit(run_probe, root, d, pdf, cmd, args.timeout, args.binary)
            else:
                f = pool.submit(run_deep_probe, root, d, cmd, args.timeout, args.binary)
            futs[f] = (d, cmd)
        for f in concurrent.futures.as_completed(futs):
            d, cmd = futs[f]
            try:
                p = f.result()
            except Exception as exc:
                p = Probe(d, cmd, "crash", -1, 0.0, "harness",
                          f"{type(exc).__name__}: {exc}"[:200])
            probes[f"{d}\x00{cmd}"] = asdict(p)
            done += 1
            if done % 100 == 0:
                print(f"      {done}/{len(jobs)}", flush=True)

    today = started.date().isoformat()
    for d in cohort:
        last[d] = today
    # drop documents that no longer exist, else coverage exceeds 100%
    last = {d: when for d, when in last.items() if d in docs}

    cur = RunState(
        started=started.isoformat(),
        finished=dt.datetime.now(dt.timezone.utc).isoformat(),
        pdfdrill_version=version,
        host=os.uname().nodename,
        cohort=cohort,
        docs={k: asdict(v) for k, v in docs.items()},
        probes=probes,
        last_audited=last,
        corpus_total=len(docs),
    )

    print("[4/4] diffing against previous run …", flush=True)
    diff = diff_runs(prev, cur)
    elapsed = time.monotonic() - t0

    report = write_report(cur, diff, outdir / "latest.md", elapsed)
    (outdir / "history" / f"{today}.md").write_text(report, encoding="utf-8")
    (outdir / "state.json").write_text(json.dumps(asdict(cur)), encoding="utf-8")
    (outdir / "latest.json").write_text(json.dumps({
        "date": today,
        "pdfdrill_version": version,
        "probes": len(probes),
        "cohort": len(cohort),
        "corpus": len(docs),
        "new_failures": len(diff["new_failures"]),
        "fact_regressions": len(diff["fact_regressions"]),
        "corrupted": len(diff["corrupted"]),
        "vanished": len(diff["vanished"]),
        "fixed": len(diff["fixed"]),
        "still_failing": len(diff["still_failing"]),
        "elapsed_sec": round(elapsed, 1),
    }, indent=2), encoding="utf-8")

    print()
    print(report[:2000])
    print(f"\nfull report: {outdir/'latest.md'}")

    # exit 1 only for genuine regressions, so the timer/Actions run goes red
    return 1 if (diff["new_failures"] or diff["fact_regressions"]
                 or diff["corrupted"] or diff["vanished"]) else 0


if __name__ == "__main__":
    sys.exit(main())
