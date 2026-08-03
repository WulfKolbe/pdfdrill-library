# pdfdrill soak test — 2026-08-03 21:19

## 🟢 all invariants held

- **Ran for:** 0.05 h of a 0.1 h budget
- **Probes:** 521 over 41 document-passes (4 full pass(es) of 6 documents)
- **Crashes (traceback):** 0
- **Invariant violations:** 0
- **pdfdrill version:** 0.4.0
- **Phases:** light, idempotency, medium, repeat1, repeat2, repeat3, repeat4

Pass rate 521/521 (100.00%) · 0 refusals · 0 timeouts

## Slowest probes

| Seconds | Document | Command |
|---|---|---|
| 30.5 | `001 Handbook of Categorical Algebra Volume 1, Basic Category Theory (Francis Borceux) (Z-Library)` | `tables` |
| 30.3 | `001 Handbook of Categorical Algebra Volume 1, Basic Category Theory (Francis Borceux) (Z-Library)` | `tables` |
| 30.3 | `001 Handbook of Categorical Algebra Volume 1, Basic Category Theory (Francis Borceux) (Z-Library)` | `tables` |
| 29.8 | `001 Handbook of Categorical Algebra Volume 1, Basic Category Theory (Francis Borceux) (Z-Library)` | `tables` |
| 22.1 | `%E2%80%8B%E2%80%8B%EF%BB%BFSuperagency%20%E2%80%94%20The%20Hidden%20Blueprint%20of%20Reality%2C%20Formula%20for%20Traveling%20Dimensions%2C%20and%20The%20Ultimate%20Invariant%20Principle` | `tables` |
| 21.9 | `%E2%80%8B%E2%80%8B%EF%BB%BFSuperagency%20%E2%80%94%20The%20Hidden%20Blueprint%20of%20Reality%2C%20Formula%20for%20Traveling%20Dimensions%2C%20and%20The%20Ultimate%20Invariant%20Principle` | `tables` |
| 21.5 | `%E2%80%8B%E2%80%8B%EF%BB%BFSuperagency%20%E2%80%94%20The%20Hidden%20Blueprint%20of%20Reality%2C%20Formula%20for%20Traveling%20Dimensions%2C%20and%20The%20Ultimate%20Invariant%20Principle` | `tables` |
| 21.5 | `%E2%80%8B%E2%80%8B%EF%BB%BFSuperagency%20%E2%80%94%20The%20Hidden%20Blueprint%20of%20Reality%2C%20Formula%20for%20Traveling%20Dimensions%2C%20and%20The%20Ultimate%20Invariant%20Principle` | `tables` |
| 21.4 | `%E2%80%8B%E2%80%8B%EF%BB%BFSuperagency%20%E2%80%94%20The%20Hidden%20Blueprint%20of%20Reality%2C%20Formula%20for%20Traveling%20Dimensions%2C%20and%20The%20Ultimate%20Invariant%20Principle` | `tables` |
| 9.7 | `0001124` | `tables` |
| 9.6 | `0001124` | `tables` |
| 9.5 | `0001124` | `tables` |
| 9.4 | `0001124` | `tables` |
| 9.2 | `0001124` | `tables` |

## Interpreting this run

A refusal (non-zero exit, no traceback) is often legitimate — asking for a TOC of a document that has none. A **crash** or an **invariant violation** never is. Triage the violations first, then the crash signatures, and treat the refusal counts as background.
