# Response-time audit contract

`bid-response-audit` is a local audit of property-maintenance response times.
It reconciles the buyer's repair tickets with the awarded supplier's
check-ins and reports which relationships and which on-time / late / missing
conclusions are actually confirmable. It never re-runs the award and produces
no acceptance receipt, deduction, or supplier default. Python 3.11+,
standard library only. Every input is local; nothing is fetched.

```sh
bid-response-audit --review REVIEW.json --input LOGS.json --output REPORT.json
```

The reusable interface takes parsed objects and returns the same
JSON-serialisable report:

```python
from procurement import audit_responses, load_json_file

report = audit_responses(
    load_json_file("REVIEW.json"),
    load_json_file("LOGS.json"),
)
```

`load_json_file(path)` is the strict loader: invalid JSON, duplicate object
keys and non-finite numbers fail the whole run. The CLI writes only to a path
that does not already exist, serialises the complete report before touching
the filesystem, and therefore never leaves a partial file or overwrites an
existing one. On any error it exits 2 and writes nothing.

## Inputs

### Review

`REVIEW.json` is the archived `bid-review` output for **one** procurement
package. Only the lot and awarded-supplier identity is used:

- every `lots[].lot_id` must be unique and every `offers[].offer_id` across
  the whole review must be unique;
- a lot referenced by `LOGS.json` must have `awarded: true`, exactly one
  offer with `awarded: true`, and `award_offer_id` / `award_vendor_id` equal
  to that offer's `offer_id` / `vendor_id`;
- a referenced lot that is not awarded is an error, not a silently empty
  result.

All other review fields (`price_cents`, `eligible`, `reasons`,
`selection_reasons`, `package`, qualification fields, …) are tolerated and
ignored, so an archived review can be audited unchanged.

### Logs

`LOGS.json` is `{closed_through, sites, tickets, checkins}`.

- `closed_through` — ISO-8601 timestamp with a UTC offset. The buyer confirms
  the check-in export is complete through this instant, **inclusive**.
- `sites` — `{lot_id, site_id, sla_minutes, search_minutes}`, unique by
  `(lot_id, site_id)`. Both limits are positive integers with
  `sla_minutes <= search_minutes`. The same `site_id` may appear under
  different lots and is then a completely separate queue.
- `tickets` — `{ticket_id, lot_id, site_id, opened_at}`. `ticket_id` is unique
  across the whole file.
- `checkins` — `{checkin_id, lot_id, site_id, vendor_id, arrived_at}`.
  `checkin_id` is unique across the whole file, and `vendor_id` must be the
  awarded vendor of its lot.

Timestamps are ISO-8601 with a UTC offset and are compared as actual
instants, so `09:00+00:00` and `11:00+02:00` are the same moment. Distinct
records that share an instant are still distinct facts: nothing is
deduplicated by timestamp. Array order carries no meaning.

Any ticket whose search window ends after `closed_through`, and any check-in
arriving after `closed_through`, is an input error. Otherwise records that
have not been exported yet would be misreported as missing check-ins.

## Reconciliation

Within one `(lot_id, site_id)` the site is a single response queue. A ticket
may be matched to a check-in of the same site arriving from the ticket's
opening instant through `opened_at + search_minutes`, both ends inclusive.
Each ticket and each check-in is used at most once. Tickets are ordered by
`(opened_at, ticket_id)` and check-ins by `(arrived_at, checkin_id)`, with the
record ID breaking ties. If two tickets are both matched, the earlier ticket
must take the earlier check-in; either queue may skip records. The report
maximises the number of matched pairs per site, independently of other
sites.

The audit reports every fact that holds across **all** maximum reconciliations;
it never picks an arbitrary one. A candidate relationship is reported for a
ticket when some maximum reconciliation pairs them. A ticket is reported as
possibly unmatched when some maximum reconciliation leaves it out. In
particular, a check-in that lies inside a ticket's window but cannot appear
in any maximum reconciliation (pairing it would strand a later ticket) is
**not** listed for that ticket.

## Report

```json
{
  "closed_through": "2026-04-30T23:59:59+00:00",
  "lots": [
    {
      "lot_id": "LOT-A",
      "vendor_id": "V1",
      "sites": [
        {
          "site_id": "SITE-1",
          "sla_minutes": 5,
          "search_minutes": 20,
          "tickets_total": 2,
          "checkins_total": 1,
          "max_pairs": 1,
          "tickets": [
            {
              "ticket_id": "TA1",
              "opened_at": "2026-04-01T09:00:00+00:00",
              "possible_checkin_ids": ["CA1"],
              "can_be_unmatched": true,
              "confirmed_checkin_id": null,
              "verdict": "needs_review",
              "possible_verdicts": ["late", "no_checkin"]
            }
          ]
        }
      ]
    }
  ],
  "totals": {
    "lots": 1, "sites": 1, "tickets": 2, "checkins": 1, "max_pairs": 1,
    "verdicts": {"on_time": 0, "late": 0, "no_checkin": 0, "needs_review": 2}
  }
}
```

Lots are sorted by `lot_id`, sites by `site_id`, tickets by business order
(`opened_at`, then `ticket_id`).

Per ticket:

- `possible_checkin_ids` — sorted IDs of every check-in that can be paired
  with the ticket in some maximum reconciliation. Empty when no candidate
  exists at all.
- `can_be_unmatched` — whether some maximum reconciliation leaves the ticket
  unpaired.
- `confirmed_checkin_id` — the check-in ID only when it is the single possible
  partner **and** the ticket is matched in every maximum reconciliation;
  otherwise `null`. Two interchangeable check-ins therefore keep the claim
  "on time" without a confirmed ID.
- `verdict` — `on_time`, `late`, `no_checkin`, or `needs_review`.
- `possible_verdicts` — every conclusion achievable in some maximum
  reconciliation, in the fixed order `on_time`, `late`, `no_checkin`:
  - `on_time` — paired within `sla_minutes` of opening (boundary included);
  - `late` — paired but later than `sla_minutes`;
  - `no_checkin` — unpaired in that reconciliation.

`verdict` equals the single element of `possible_verdicts` when there is
exactly one, and `needs_review` otherwise, with all possibilities retained. A
missing check-in concerns this closed export only: it is not proof that the
supplier never attended, and the report makes no deduction or acceptance
decision.

Changing source array order, or rewriting a timestamp as the same instant with
another offset, does not change the report.

## Worked examples

With `sla_minutes = 5` and `search_minutes = 20`:

- Tickets 09:00 and 09:03, one check-in 09:06. Maximum 1. The first ticket may
  be late or unmatched, the second on time or unmatched; both are
  `needs_review`.
- One ticket at 09:00, check-ins 09:02 and 09:04. Both support a maximum of 1.
  No check-in ID is confirmable, but `verdict` is `on_time`.
- Tickets 09:00 and 09:05, check-ins 09:01 and 09:08. The 09:08 check-in is
  inside the first ticket's window but cannot be used: the second ticket would
  be left unpairable and the total would drop below 2. It is not listed for
  the first ticket.

## Errors

Invalid JSON; duplicate object keys; non-finite numbers; wrong field types
(booleans are not integers); empty or duplicate IDs; duplicate sites;
unconfigured sites; unknown lots; a referenced lot that is not awarded; an
`award_offer_id`/`award_vendor_id` that does not match the lot's sole awarded
offer; a check-in vendor that is not the lot's awarded vendor; timestamps
without an offset, malformed timestamps; non-positive or inverted time limits;
and any ticket window or check-in beyond `closed_through` all fail the whole
run, naming the offending record or field. No partial output is produced.

## Performance

Per site, candidate edges are generated only inside each ticket's search
window and the maximum reconciliation is found as a longest chain over those
edges with Fenwick-tree prefix maxima, `O(E log N)`; alternating reachability
over the same edges is `O(E)`. Short windows therefore keep large sites cheap.
The reproducible benchmark in `fixtures/response-audit/` uses 30,000 tickets
and 30,000 check-ins at one site with ~210,000 candidate edges and heavy
interchangeability:

```sh
python3 fixtures/response-audit/generate_fixtures.py
RESPONSE_AUDIT_SCALE=1 python3 -m unittest tests.test_response_audit.ScaleBenchmarkTests
```

Observed on a single ordinary core with a 512 MiB address-space cap
(`ulimit -v 524288`): about 0.6 s and ~105 MiB peak RSS for the pure audit
call, and ~1.1 s / ~150 MiB for the CLI including parsing and report
serialisation.
