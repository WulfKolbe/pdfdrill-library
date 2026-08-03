#!/usr/bin/env python3
"""
Long-running pdfdrill soak test — designed to fill a wall-clock budget
(default 12 hours) and then stop cleanly with a report.

This is not the nightly audit. The nightly asks "what broke since
yesterday?" over 120 documents in five minutes. The soak asks "does
pdfdrill hold its own invariants when you hammer the entire corpus for
half a day?" and it checks that with real oracles, not just exit codes:

  MONOTONIC    a document's fact set may grow, never shrink. Introspection
               commands add cached knowledge; if one ever removes a fact,
               enrichment is being destroyed.
  PDF_FROZEN   the source PDF is read-only input. Its checksum must be
               identical before and after. A tool that rewrites the user's
               PDF is a catastrophic bug worth a dedicated oracle.
  JSON_VALID   the sidecar must still parse after every single command.
               This is how the library's 26 unreadable sidecars were born.
  IDEMPOTENT   running the same command twice must leave the same fact set.
               pdfdrill documents many commands as idempotent; this checks it.
  NO_CRASH     a non-zero exit is a refusal and may be legitimate; a Python
               traceback never is.

Work is scheduled in phases and the last phase repeats until the budget is
spent, so the run lasts about as long as you asked for regardless of how
fast the corpus turns out to be.

Progress is checkpointed to JSONL after every document, so the run is
resumable: kill it, restart it, and it picks up where it stopped. That
matters for a 12-hour job on any machine that might be interrupted.

Concurrency is per DOCUMENT, never per command. All commands for one
document run sequentially in one worker, otherwise the monotonic and
idempotency oracles would race against each other.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# Cheap, additive, safe against the live corpus.
BATTERY_LIGHT = [
    "size", "pdfinfo", "status", "fonts", "fonts_layer",
    "abstract", "toc", "links", "dests", "bibtex", "artifacts",
]

# Slower but still non-destructive introspection.
BATTERY_MEDIUM = ["images", "tables", "identifiers", "booktoc", "urls"]

# Rebuilds models. NEVER run against the live corpus; sandboxed copy only.
BATTERY_DEEP = ["model", "llmtext", "eqblobs", "bibliography", "tiddlers"]

# Commands asserted to be idempotent, checked by running them twice.
IDEMPOTENT_CHECK = ["size", "pdfinfo", "fonts", "bibtex", "dests"]

HASH_MAX = 64 * 1024 * 1024   # hash PDFs up to 64 MB; larger use size+mtime


# ---------------------------------------------------------------- budget


class Budget:
    """Wall-clock budget with a clean stop."""

    def __init__(self, hours: float):
        self.total = hours * 3600.0
        self.start = time.monotonic()
        self._stop = threading.Event()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.start

    @property
    def remaining(self) -> float:
        return max(0.0, self.total - self.elapsed)

    def expired(self) -> bool:
        return self._stop.is_set() or self.remaining <= 0

    def stop(self) -> None:
        self._stop.set()

    def pct(self) -> float:
        return 100.0 * min(1.0, self.elapsed / self.total) if self.total else 100.0

    def eta(self) -> str:
        r = int(self.remaining)
        return f"{r//3600}h{(r%3600)//60:02d}m"


# ---------------------------------------------------------------- state


@dataclass
class Violation:
    doc: str
    cmd: str
    oracle: str
    detail: str
    phase: str


@dataclass
class Snapshot:
    facts: list[str]
    pdf_fingerprint: str
    sidecar_valid: bool
    sidecar_error: str = ""


@dataclass
class Totals:
    probes: int = 0
    ok: int = 0
    failed: int = 0
    crashed: int = 0
    timeout: int = 0
    docs_done: int = 0
    violations: list = field(default_factory=list)
    errors: dict = field(default_factory=dict)   # signature -> [detail, count]
    slowest: list = field(default_factory=list)  # (seconds, doc, cmd)


# ---------------------------------------------------------------- helpers


def fingerprint(pdf: Path) -> str:
    try:
        st = pdf.stat()
        if st.st_size > HASH_MAX:
            return f"size:{st.st_size}:mtime:{int(st.st_mtime)}"
        h = hashlib.sha256()
        with open(pdf, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return "sha256:" + h.hexdigest()
    except Exception as exc:
        return f"error:{exc}"


def read_sidecar(folder: Path) -> tuple[list[str], bool, str]:
    scs = sorted(folder.glob("*.drill.json"))
    if not scs:
        return [], False, "no sidecar"
    try:
        data = json.loads(scs[0].read_text())
        return sorted(data.get("facts") or []), True, ""
    except Exception as exc:
        return [], False, f"{type(exc).__name__}: {exc}"[:180]


def snapshot(folder: Path, pdf: Path) -> Snapshot:
    facts, valid, err = read_sidecar(folder)
    return Snapshot(facts, fingerprint(pdf), valid, err)


_NOISE = [
    (re.compile(r'File "[^"]+"'), 'File "..."'),
    (re.compile(r"0x[0-9a-fA-F]+"), "0xADDR"),
    (re.compile(r"/[\w./+-]{4,}"), "/PATH"),
    (re.compile(r"\b\d+\b"), "N"),
    (re.compile(r"\s+"), " "),
]


def signature(text: str) -> tuple[str, str]:
    blob = (text or "").strip()
    if not blob:
        return "", ""
    lines = [l for l in blob.splitlines() if l.strip()]
    if not lines:
        return "", ""
    for line in reversed(lines):
        if re.match(r"^[A-Za-z_][\w.]*(Error|Exception)\b", line.strip()):
            d = line.strip()
            break
    else:
        d = lines[-1].strip()
    n = d
    for pat, rep in _NOISE:
        n = pat.sub(rep, n)
    return hashlib.sha1(n.encode()).hexdigest()[:12], d[:240]


def invoke(binary: str, cmd: str, pdf: str, cwd: Path, timeout: int):
    env = dict(os.environ)
    env["PDFDRILL_NO_PREFLIGHT"] = "1"
    t0 = time.monotonic()
    try:
        p = subprocess.run([binary, cmd, pdf], cwd=cwd, env=env,
                           timeout=timeout, capture_output=True,
                           text=True, errors="replace")
        return p.returncode, p.stdout, p.stderr, time.monotonic() - t0
    except subprocess.TimeoutExpired:
        return -9, "", f"TIMEOUT after {timeout}s", float(timeout)
    except Exception as exc:
        return -1, "", f"{type(exc).__name__}: {exc}", time.monotonic() - t0


# ---------------------------------------------------------------- worker


def process_document(root: Path, doc: str, pdf_rel: str, battery: list[str],
                     phase: str, binary: str, timeout: int,
                     budget: Budget, check_idempotent: bool):
    """Run a whole battery against one document, sequentially.

    Returns (records, violations). Sequential-per-document is required: the
    monotonic and idempotency oracles compare sidecar state before and after
    each command, which two concurrent commands would corrupt.
    """
    folder = root / doc
    pdf = root / pdf_rel
    records, viols = [], []

    before = snapshot(folder, pdf)

    for cmd in battery:
        if budget.expired():
            break

        rc, out, err, dur = invoke(binary, cmd, pdf_rel, root, timeout)
        after = snapshot(folder, pdf)

        if rc == 0:
            status = "ok"
        elif rc == -9:
            status = "timeout"
        elif "Traceback" in (err or ""):
            status = "crash"
        else:
            status = "fail"

        sig, detail = ("", "") if status == "ok" else signature(err or out)

        # ---- oracles -------------------------------------------------
        if status == "crash":
            viols.append(Violation(doc, cmd, "NO_CRASH", detail, phase))

        lost = set(before.facts) - set(after.facts)
        if lost:
            viols.append(Violation(doc, cmd, "MONOTONIC",
                                   f"lost {sorted(lost)}", phase))

        if before.pdf_fingerprint != after.pdf_fingerprint:
            viols.append(Violation(doc, cmd, "PDF_FROZEN",
                                   f"{before.pdf_fingerprint[:24]} -> "
                                   f"{after.pdf_fingerprint[:24]}", phase))

        if before.sidecar_valid and not after.sidecar_valid:
            viols.append(Violation(doc, cmd, "JSON_VALID",
                                   after.sidecar_error, phase))

        if check_idempotent and cmd in IDEMPOTENT_CHECK and status == "ok" \
                and not budget.expired():
            rc2, out2, err2, dur2 = invoke(binary, cmd, pdf_rel, root, timeout)
            again = snapshot(folder, pdf)
            if rc2 == 0 and set(again.facts) != set(after.facts):
                delta = set(again.facts) ^ set(after.facts)
                viols.append(Violation(doc, cmd, "IDEMPOTENT",
                                       f"second run changed facts: {sorted(delta)}",
                                       phase))
            records.append({"doc": doc, "cmd": cmd + "(2nd)", "status":
                            "ok" if rc2 == 0 else "fail", "dur": round(dur2, 3),
                            "phase": phase, "sig": "", "detail": ""})

        records.append({"doc": doc, "cmd": cmd, "status": status,
                        "dur": round(dur, 3), "phase": phase,
                        "sig": sig, "detail": detail})
        before = after

    return records, viols


def process_sandboxed(root: Path, doc: str, battery: list[str], phase: str,
                      binary: str, timeout: int, budget: Budget):
    """Deep battery inside a throwaway copy; the live corpus is never touched."""
    src = root / doc
    with tempfile.TemporaryDirectory(prefix="soak-") as tmp:
        dst = Path(tmp) / doc
        try:
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
                "*.docmodel.json", "*.docpack.json", "chars.json", "*.lines.json",
                "rasterize", "inspect", "visionocr", "viewer", "images_extracted"))
        except Exception as exc:
            return [{"doc": doc, "cmd": "copytree", "status": "fail",
                     "dur": 0.0, "phase": phase, "sig": "copy",
                     "detail": str(exc)[:200]}], []
        pdfs = sorted(dst.glob("*.pdf"))
        if not pdfs:
            return [], []
        rel = str(pdfs[0].relative_to(tmp))
        return process_document(Path(tmp), doc, rel, battery, phase,
                                binary, timeout, budget, check_idempotent=False)


# ---------------------------------------------------------------- discovery


def discover(root: Path) -> list[tuple[str, str]]:
    out = []
    for e in sorted(os.scandir(root), key=lambda x: x.name):
        if not e.is_dir() or e.name.startswith(".") \
                or e.name in {"audit", "reports", "node_modules"}:
            continue
        d = Path(e.path)
        pdfs = sorted(d.glob("*.pdf"))
        if not pdfs:
            continue
        match = [p for p in pdfs if p.stem == e.name]
        out.append((e.name, str((match or pdfs)[0].relative_to(root))))
    return out


# ---------------------------------------------------------------- report


def build_report(t: Totals, budget: Budget, passes: int, corpus: int,
                 version: str, phases_run: list[str]) -> str:
    L: list[str] = []
    A = L.append
    hrs = budget.elapsed / 3600

    by_oracle: dict[str, list] = {}
    for v in t.violations:
        by_oracle.setdefault(v["oracle"], []).append(v)

    bad = bool(t.violations) or t.crashed > 0
    A(f"# pdfdrill soak test — {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    A("")
    A(f"## {'🔴 INVARIANT VIOLATIONS' if bad else '🟢 all invariants held'}")
    A("")
    A(f"- **Ran for:** {hrs:.2f} h of a {budget.total/3600:.1f} h budget")
    A(f"- **Probes:** {t.probes:,} over {t.docs_done:,} document-passes "
      f"({passes} full pass(es) of {corpus:,} documents)")
    A(f"- **Crashes (traceback):** {t.crashed}")
    A(f"- **Invariant violations:** {len(t.violations)}")
    A(f"- **pdfdrill version:** {version or 'unknown'}")
    A(f"- **Phases:** {', '.join(phases_run)}")
    A("")
    A(f"Pass rate {t.ok:,}/{t.probes:,} "
      f"({100*t.ok/max(1,t.probes):.2f}%) · "
      f"{t.failed:,} refusals · {t.timeout:,} timeouts")
    A("")

    if by_oracle:
        A("## 🔴 Invariant violations")
        A("")
        A("These are stronger findings than a failing exit code: each one is "
          "pdfdrill breaking a property it is supposed to guarantee.")
        A("")
        for oracle, items in sorted(by_oracle.items(), key=lambda kv: -len(kv[1])):
            A(f"### {oracle} — {len(items)} violation(s)")
            A("")
            explain = {
                "MONOTONIC": "A command REMOVED facts from a sidecar. "
                             "Enrichment was destroyed.",
                "PDF_FROZEN": "The source PDF changed on disk. pdfdrill must "
                              "treat it as read-only input.",
                "JSON_VALID": "The sidecar stopped parsing after this command. "
                              "This is how unreadable sidecars are created.",
                "IDEMPOTENT": "Running the command twice produced a different "
                              "fact set. It is documented as idempotent.",
                "NO_CRASH": "Python traceback — an unhandled exception.",
            }.get(oracle, "")
            if explain:
                A(f"> {explain}")
                A("")
            seen = set()
            for v in items[:15]:
                key = (v["doc"], v["cmd"])
                if key in seen:
                    continue
                seen.add(key)
                A(f"- `{v['doc']}` · `{v['cmd']}` ({v['phase']}) — {v['detail']}")
            if len(items) > 15:
                A(f"- …and {len(items)-15} more")
            A("")

    if t.errors:
        A("## Failures grouped by signature")
        A("")
        A("| Count | Signature | Example |")
        A("|---|---|---|")
        for sig, (detail, count, cmds) in sorted(
                t.errors.items(), key=lambda kv: -kv[1][1])[:30]:
            cmdlist = ", ".join(sorted(cmds)[:4])
            A(f"| {count} | `{cmdlist}` | {detail[:100]} |")
        A("")

    if t.slowest:
        A("## Slowest probes")
        A("")
        A("| Seconds | Document | Command |")
        A("|---|---|---|")
        for sec, doc, cmd in sorted(t.slowest, reverse=True)[:20]:
            A(f"| {sec:.1f} | `{doc}` | `{cmd}` |")
        A("")

    A("## Interpreting this run")
    A("")
    A("A refusal (non-zero exit, no traceback) is often legitimate — asking "
      "for a TOC of a document that has none. A **crash** or an **invariant "
      "violation** never is. Triage the violations first, then the crash "
      "signatures, and treat the refusal counts as background.")
    A("")
    return "\n".join(L)


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--hours", type=float, default=12.0,
                    help="wall-clock budget (default 12)")
    ap.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) - 4))
    ap.add_argument("--timeout", type=int, default=180, help="seconds per command")
    ap.add_argument("--binary", default="pdfdrill")
    ap.add_argument("--limit", type=int, default=0, help="cap documents per pass")
    ap.add_argument("--no-deep", action="store_true",
                    help="skip the sandboxed model-rebuilding phase")
    ap.add_argument("--resume", action="store_true",
                    help="skip documents already recorded in the checkpoint")
    ap.add_argument("--seed", type=int, default=1234)
    args = ap.parse_args()

    root = Path(args.root).resolve()
    outdir = root / "reports"
    outdir.mkdir(parents=True, exist_ok=True)
    ckpt_path = outdir / "soak.jsonl"

    if shutil.which(args.binary) is None and not Path(args.binary).exists():
        print(f"FATAL: {args.binary} not found on PATH.", file=sys.stderr)
        print("On a fresh machine run: audit/cloud_bootstrap.sh", file=sys.stderr)
        return 2

    budget = Budget(args.hours)
    docs = discover(root)
    if not docs:
        print(f"FATAL: no documents under {root}", file=sys.stderr)
        return 2
    if args.limit:
        docs = docs[:args.limit]

    done_keys: set[str] = set()
    if args.resume and ckpt_path.exists():
        for line in ckpt_path.read_text(errors="replace").splitlines():
            try:
                r = json.loads(line)
                done_keys.add(f"{r.get('phase')}::{r.get('doc')}")
            except Exception:
                continue
        print(f"resuming: {len(done_keys)} document-phases already recorded")

    version = ""
    for doc, _ in docs[:50]:
        f, ok, _ = read_sidecar(root / doc)
        if ok:
            try:
                sc = sorted((root / doc).glob("*.drill.json"))[0]
                version = json.loads(sc.read_text()).get("pdfdrill_version") or ""
            except Exception:
                pass
            if version:
                break

    print("=" * 68)
    print(f"pdfdrill soak test — budget {args.hours} h, {len(docs):,} documents")
    print(f"root={root}")
    print(f"workers={args.workers} timeout={args.timeout}s version={version}")
    print("=" * 68, flush=True)

    totals = Totals()
    lock = threading.Lock()
    ckpt = open(ckpt_path, "a", buffering=1)

    def absorb(records, viols):
        with lock:
            for r in records:
                totals.probes += 1
                st = r["status"]
                if st == "ok":
                    totals.ok += 1
                elif st == "crash":
                    totals.crashed += 1
                elif st == "timeout":
                    totals.timeout += 1
                else:
                    totals.failed += 1
                if r.get("sig"):
                    e = totals.errors.setdefault(r["sig"], [r["detail"], 0, set()])
                    e[1] += 1
                    e[2].add(r["cmd"])
                if r["dur"] > 5:
                    totals.slowest.append((r["dur"], r["doc"], r["cmd"]))
                    if len(totals.slowest) > 400:
                        totals.slowest = sorted(totals.slowest, reverse=True)[:100]
                ckpt.write(json.dumps(r) + "\n")
            for v in viols:
                totals.violations.append(v.__dict__)
                print(f"  !! {v.oracle}  {v.doc} · {v.cmd} — {v.detail}", flush=True)

    def run_phase(name: str, work: list, fn) -> None:
        if budget.expired():
            return
        pending = [w for w in work
                   if f"{name}::{w[0]}" not in done_keys] if args.resume else work
        if not pending:
            return
        print(f"\n--- phase {name}: {len(pending):,} documents "
              f"(budget left {budget.eta()}) ---", flush=True)
        n = 0
        with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
            futs = {pool.submit(fn, w): w[0] for w in pending}
            try:
                for fut in concurrent.futures.as_completed(futs):
                    try:
                        recs, viols = fut.result()
                    except Exception as exc:
                        recs, viols = [], []
                        print(f"  harness error: {exc}", flush=True)
                    absorb(recs, viols)
                    n += 1
                    with lock:
                        totals.docs_done += 1
                    if n % 25 == 0:
                        print(f"  {name}: {n}/{len(pending)} docs · "
                              f"{totals.probes:,} probes · "
                              f"{len(totals.violations)} violations · "
                              f"{budget.pct():.0f}% of budget · "
                              f"{budget.eta()} left", flush=True)
                    if budget.expired():
                        print(f"  budget reached, draining {name} …", flush=True)
                        for f2 in futs:
                            f2.cancel()
                        break
            except KeyboardInterrupt:
                budget.stop()
                for f2 in futs:
                    f2.cancel()
                raise

    phases_run: list[str] = []
    rnd = random.Random(args.seed)
    passes = 0

    try:
        # P1 light battery over everything
        phases_run.append("light")
        run_phase("light", docs,
                  lambda w: process_document(root, w[0], w[1], BATTERY_LIGHT,
                                             "light", args.binary, args.timeout,
                                             budget, check_idempotent=False))

        # P2 idempotency oracle on a subset (each command runs twice)
        if not budget.expired():
            phases_run.append("idempotency")
            sub = docs[:]
            rnd.shuffle(sub)
            run_phase("idempotency", sub[:max(50, len(sub) // 4)],
                      lambda w: process_document(root, w[0], w[1],
                                                 IDEMPOTENT_CHECK, "idempotency",
                                                 args.binary, args.timeout,
                                                 budget, check_idempotent=True))

        # P3 medium battery
        if not budget.expired():
            phases_run.append("medium")
            run_phase("medium", docs,
                      lambda w: process_document(root, w[0], w[1], BATTERY_MEDIUM,
                                                 "medium", args.binary,
                                                 args.timeout, budget, False))

        # P4 deep, sandboxed - this is what actually consumes hours
        if not budget.expired() and not args.no_deep:
            phases_run.append("deep")
            sub = docs[:]
            rnd.shuffle(sub)
            run_phase("deep", sub,
                      lambda w: process_sandboxed(root, w[0], BATTERY_DEEP,
                                                  "deep", args.binary,
                                                  args.timeout, budget))

        # P5 repeat until the budget is spent, reshuffled each pass
        while not budget.expired():
            passes += 1
            tag = f"repeat{passes}"
            phases_run.append(tag)
            sub = docs[:]
            rnd.shuffle(sub)
            run_phase(tag, sub,
                      lambda w, _t=tag: process_document(
                          root, w[0], w[1], BATTERY_LIGHT + BATTERY_MEDIUM,
                          _t, args.binary, args.timeout, budget, False))
            if budget.expired():
                break

    except KeyboardInterrupt:
        print("\ninterrupted — writing report from work completed so far",
              flush=True)

    ckpt.close()

    report = build_report(totals, budget, max(1, passes), len(docs),
                          version, phases_run)
    (outdir / "soak-latest.md").write_text(report, encoding="utf-8")
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    (outdir / "history" / f"soak-{stamp}.md").parent.mkdir(parents=True, exist_ok=True)
    (outdir / "history" / f"soak-{stamp}.md").write_text(report, encoding="utf-8")
    (outdir / "soak-latest.json").write_text(json.dumps({
        "hours": round(budget.elapsed / 3600, 3),
        "budget_hours": args.hours,
        "probes": totals.probes,
        "ok": totals.ok,
        "failed": totals.failed,
        "crashed": totals.crashed,
        "timeout": totals.timeout,
        "violations": len(totals.violations),
        "documents": len(docs),
        "phases": phases_run,
        "pdfdrill_version": version,
    }, indent=2), encoding="utf-8")

    print()
    print(report[:2500])
    print(f"\nfull report: {outdir/'soak-latest.md'}")
    print(f"checkpoint : {ckpt_path}")

    return 1 if (totals.violations or totals.crashed) else 0


if __name__ == "__main__":
    sys.exit(main())
