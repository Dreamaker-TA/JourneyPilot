# `12306-train` MCP server (repo-owned)

`station_name.js` is a **verbatim, unmodified snapshot** of the official 12306
station table:

    https://kyfw.12306.cn/otn/resources/js/framework/station_name.js
    fetched 2026-07-29 — HTTP 200, 167709 bytes, 3375 records

Record format: `@`-separated records, `|`-delimited fields —
`[0]` pinyin abbreviation, `[1]` 中文站名, `[2]` telecode, `[3]` full pinyin,
`[4]` short abbreviation, `[5]` index, `[6]` city code, `[7]` 城市名.

It is committed so station resolution is deterministic and offline: it is loaded
on first use by `src/travel_agent/services/rail_12306.py` and is **never** fetched
at runtime.

`station_name.meta.json` beside it carries the same facts in machine-readable form
(`source_url` / `fetched_at` / `byte_size` / `record_count`).  It is what
`station_snapshot_freshness()` judges the snapshot's age from, what
`/api/health/ready`'s `data_snapshots` component reports, and what preflight item 4
checks the file against.  12306 publishes no version token, so those two numbers
are the only fingerprint available — and the only thing that catches a refresh
that downloaded half the file.

## Refreshing

1. Re-download the URL above verbatim.
2. Update **every** field in `station_name.meta.json`, `fetched_at` included.
3. **Restart the backend.** `station_table()` and its two derived indexes are
   `lru_cache(maxsize=1)`, so the parse lives as long as the process: a refreshed
   file on disk changes nothing in a running server, and the old table keeps
   answering with no sign that a newer one exists.
4. Verify the fingerprint, the hand-maintained hub table's city membership, and
   the age threshold stay consistent.

Age threshold: `Settings.data_snapshots.station_max_age_days` (default 90).
A stale snapshot is **reported, never blocking** — see D19-6.
