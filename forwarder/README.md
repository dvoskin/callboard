# Getting the emailed RingCX report into /v5

`/v5` reports RingCX from whichever source is better:

1. **The emailed Interaction Report** — every figure was verified by hand against
   this report. Used whenever a fresh one has been delivered.
2. **The live CDR pull** — a *different* RingCX report
   (`GLOBAL_CALL_TYPE_DELIMITED`). Fallback only; may not carry every interaction.

The report always says which one it used (`meta.ringcx_source`), and warns on the
page when it fell back. `?source=api` forces the live pull so the two can be compared.

## One-time setup

**On Render**, set two env vars:

| Variable | Purpose |
|---|---|
| `INGEST_API_KEY` | shared secret for `/api/v5/ingest`. Without it every upload is rejected. |
| `SCOREBOARD_TOKEN` | the no-login share link. Without it `/v5/board` 404s for everyone. |

Any long random string works for both — different values.

**Then** paste `gmail_to_ingest.gs` into script.google.com (delete the `myFunction` placeholder first — pasting inside it hides every function from the Run and Trigger dropdowns), set `INGEST_KEY`, run `testOnce`, and add an hourly time-driven trigger on `forwardRingCXReports`.
settings at the top, run `testOnce` to confirm the search matches your report
emails, and add an hourly time-driven trigger on `forwardRingCXReports`.

The script runs under your own Google account. Nothing else reads the mailbox.

## Check it is working

- `GET /api/v5/ingest/status` (signed in) — what the inbox holds and how stale it is.
- In Gmail, an ingested thread gets the `ringcx/ingested` label; a failed one gets
  `ringcx/failed` and is **retried** on the next run rather than skipped.

A forwarder that has quietly stopped looks exactly like a quiet day downstream,
so check the age in `/api/v5/ingest/status` rather than assuming delivery.

## Without Gmail

Anything that can POST a file works — Zapier, Make, a cron job:

```bash
curl -X POST https://<host>/api/v5/ingest \
     -H "X-API-Key: $INGEST_API_KEY" \
     -F "file=@'Daily Interaction Report (Sales).csv'"
```

Returns the row count, the detected time unit, and the day it filed the report under.
Wrong file, wrong shape or zero rows are rejected with 400 — a bad attachment can
never quietly replace a good report with an empty day.
