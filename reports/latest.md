# pdfdrill nightly audit — 2026-08-03

## 🟢 no new failures

- **New failures since last run:** 0
- **Documents that lost facts:** 0
- **Sidecars newly corrupted:** 0
- **Documents vanished:** 0

Ran 1440 probes over 120 documents in 5.1 min. Corpus: 3318 documents (26 unreadable sidecars). pdfdrill 0.4.0.

## ⚪ Pre-existing failures (10) — not new today

| Command | Docs | Signature | Example |
|---|---|---|---|
| `images` | 6 | `timeout-images` | exceeded 120s |
| `bibtex, dests, toc` | 3 | `978049f31861` | Error [JSONDecodeError]: Expecting value: line 1 column 1 (char 0) |
| `images` | 1 | `0a013dded0f5` | Error [TypeError]: 'NoneType' object is not iterable |

## Coverage

- 132/3318 documents have been probed at least once (4%)
- At 120 documents/night the full corpus is covered every 28 days
- Probe pass rate tonight: 1430/1440 (99.3%)
