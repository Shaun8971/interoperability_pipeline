# common package (shared plumbing)

| File | Job |
|---|---|
| `settings.py` | Read `config/settings.toml` (and `claims.toml`) |
| `errors.py` | Named exceptions so callers react differently to different failures |
| `event_log.py` | Event log (`events.jsonl`) and error queue (`error_queue.jsonl`, keeps the raw message) |
| `report.py` | Volume report and the three failure detectors (unknown / missing / wrong-but-believable) |

Used by every other package; depends on none of them.
