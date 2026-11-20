# Synthetic response-audit fixtures

Every file here is deterministic synthetic data created for this repository;
no real sites, suppliers, tickets or check-ins are represented.

Regenerate everything from the repository root:

```sh
python3 fixtures/response-audit/generate_fixtures.py
```

The generator uses only the standard library, contains no clock or
randomness, and produces byte-identical output when re-run. It never runs the
audit itself, so the 30,000-record scale files are produced instantly.

## Files

- `review.json` — an archived `bid-review` package result with two awarded
  lots (`LOT-A` → `V1`, `LOT-B` → `V2`) and one unawarded lot. An unawarded
  offer is retained on an awarded lot to show that unrelated review fields are
  tolerated. Regenerate the audit report with:

  ```sh
  bid-response-audit --review fixtures/response-audit/review.json \
    --input fixtures/response-audit/logs.json \
    --output run/response-report.json
  ```

- `logs.json` — the small example. `LOT-A` sites use the prompt's 5-minute SLA
  and 20-minute search window; `LOT-B` uses 10/30.

  | site | case |
  | --- | --- |
  | `LOT-A/SITE-1` | tickets 09:00, 09:03; one 09:06 check-in → max 1, both ambiguous |
  | `LOT-A/SITE-2` | one 09:00 ticket, 09:02 and 09:04 check-ins → on time provable, no confirmed ID |
  | `LOT-A/SITE-3` | 09:00/09:05 tickets, 09:01/09:08 check-ins; the 09:08 check-in cannot be a maximum partner of the first ticket |
  | `LOT-A/SITE-4` | two tickets and two check-ins at one instant, written with different offsets; distinct records stay distinct |
  | `LOT-A/SITE-5` | one ticket, on-time and late check-ins → `needs_review` with both verdicts |
  | `LOT-A/SITE-6` | one confirmed late pairing, one ticket with no candidate check-in |
  | `LOT-B/SITE-1` | the same site number as `LOT-A/SITE-1` under another lot: isolated, different vendor and limits |
  | `LOT-B/SITE-2` | configured with no records (empty queue) |

- `expected-report.json` — the exact audit report for `review.json` /
  `logs.json`. Regenerate it after intentional behaviour changes with:

  ```sh
  bid-response-audit --review fixtures/response-audit/review.json \
    --input fixtures/response-audit/logs.json \
    --output fixtures/response-audit/expected-report.json
  ```

- `scale-review.json`, `scale-logs.json` — the reproducible benchmark: 30,000
  tickets and 30,006 check-ins at one site. Ticket `i` opens at `3i` minutes;
  check-in `j` arrives 90 s after `3j` minutes, so each 20-minute window holds
  seven candidate check-ins (≈210,000 edges). Overlapping windows make every
  ticket's partner interchangeable, so no check-in ID is confirmable while
  all 30,000 tickets are matched.

  ```sh
  RESPONSE_AUDIT_SCALE=1 python3 -m unittest tests.test_response_audit.ScaleBenchmarkTests
  ```

  The test counts candidate edges independently, checks at least 30,000 of
  each record, fewer than 300,000 edges and interchangeable partners, and
  times the audit under 60 s. To measure peak memory, run the CLI under a
  512 MiB cap:

  ```sh
  ulimit -v 524288
  bid-response-audit --review fixtures/response-audit/scale-review.json \
    --input fixtures/response-audit/scale-logs.json \
    --output run/scale-report.json
  ```

  `scale-logs.json` is stored compactly because of its size; the other files
  are pretty-printed.
