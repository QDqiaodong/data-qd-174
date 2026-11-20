# Bid review contract

## Input

The input is one JSON object describing a single procurement package:

- `package_budget_cents` — non-negative integer, the shared package budget.
- `vendors` — list of vendors. Each vendor has a unique `id` and a positive
  integer `concurrent_lot_capacity`, the maximum number of lots that vendor may
  perform on any one date.
- `lots` — list of service lots. Each lot names a unique `id`, a `deadline`,
  a `budget_cents` integer, `required_documents` codes, and `service_start` /
  `service_end` dates in `YYYY-MM-DD` form with `service_start` strictly before
  `service_end`.
- `offers` — list of offers with a unique `id`, `lot_id`, `vendor_id`,
  `submitted` ISO-8601 timestamp with an offset, `price_cents`, and `documents`.
  Every `vendor_id` must reference a declared vendor.

Service intervals are half-open: a lot ending on a date and another starting on
that same date do not overlap.

## Per-lot eligibility

An offer is eligible for its lot only when it arrives no later than the lot
deadline, includes every required document code, and its price does not exceed
the lot budget. Eligibility is reported per offer as `eligible` plus `reasons`
(`late`, `missing_documents:<codes>`, `over_budget`). Ineligible offers never
consume package budget or vendor capacity.

## Package award

Choose at most one eligible offer per lot, or leave the lot unawarded. The plan
must satisfy:

- total selected prices ≤ `package_budget_cents`;
- for every vendor, the peak number of its awarded lots whose half-open service
  intervals cover the same date ≤ that vendor's `concurrent_lot_capacity`.

Among all feasible plans the winner is chosen lexicographically by:

1. most awarded lots;
2. lowest total selected price;
3. the selected offer IDs in ascending lot-ID order, with an unawarded lot
   sorting before any awarded offer.

Per-lot lowest prices are therefore not simply concatenated: the search may
skip a cheap offer to avoid a budget or capacity overrun, and it may leave an
early lot unawarded when the tie-break requires it.

## Output

`lots` contains, per lot in ascending ID order, its service dates, budget, all
offers sorted by `(price_cents, vendor_id, offer_id)` with `eligible`, `reasons`,
`awarded`, and `selection_reasons`, plus `award_offer_id`, `award_vendor_id`,
and `awarded`. Eligibility and selection are separate: an eligible offer that
loses to package budget, vendor capacity, or the objective ordering stays
eligible with empty `reasons` and reports `selection_reasons` such as
`package_budget`, `vendor_capacity`, or `objective_tradeoff`.

`package` reports `budget_cents`, `awarded_total_cents`, `remaining_cents`,
`lots_total`, `lots_awarded`, and a `vendors` list with each vendor's
`concurrent_lot_capacity`, `peak_concurrent_lots`, and `awarded_lot_ids`.
The top-level `awarded` is the number of awarded lots.

## Errors

Duplicate lot, offer, or vendor IDs; unknown lot or vendor references; negative
prices; timestamps without an offset; malformed service dates; a service start
not before its end; a negative or non-integer package budget; and a
non-positive or non-integer vendor capacity fail the whole run. The CLI exits 2
and does not overwrite an existing output file.

This is a transparent mechanical screen; policy, conflicts of interest, and
legal review remain human responsibilities.
