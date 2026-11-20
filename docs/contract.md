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

## Supplier qualification evidence (optional)

A second, optional input lets the reviewer confirm that each offer's declared
required documents are backed by a valid supplier credential at review time.
Qualification data is exported locally by the buyer; nothing is fetched from
an external service. The dependency set is unchanged: Python 3.11+ standard
library only.

```sh
bid-review --input PACKAGE.json --output RESULT.json \
  [--qualification-file QUALIFICATIONS.json --qualification-as-of TIMESTAMP]
```

The reusable `review(data, qualification=None, qualification_as_of=None)` call
accepts the same pair. Both arguments (or CLI flags) must be supplied together.
When neither is supplied, the command, input, output, eligibility rules, and
award plan are byte-for-byte the historical behaviour: no qualification fields
appear in the result.

### Qualification file

```json
{ "schema_version": 1, "credentials": [ ... ] }
```

The file is the complete revision history of every credential. Each record
has exactly these fields:

- `credential_id` — non-empty string, shared by all revisions of one credential;
- `revision` — positive integer, starting at 1 and contiguous per credential;
- `vendor_id` — declared vendor in the package; identical in every revision;
- `document_code` — non-empty string; identical in every revision;
- `status` — `active`, `suspended`, or `revoked`;
- `valid_from`, `valid_until`, `recorded_at` — ISO-8601 timestamps with a UTC
  offset. `valid_from` must be before `valid_until`.

Revisions of one credential must be recorded with strictly increasing
`recorded_at`. Each `(credential_id, revision)` pair is unique. The file's
vendors must all exist in the package `vendors`.

### Review instant and visibility

`--qualification-as-of` is an ISO-8601 timestamp with an offset. For each
credential, only revisions with `recorded_at <= qualification_as_of` are
visible, and the highest visible revision is that credential's complete state.
A `suspended` or `revoked` visible revision never falls back to an earlier
`active` one. Revisions recorded after the review instant have no effect at
all, even if their `valid_from` reaches back before the review instant.

### Eligibility extension

Qualification checking runs before the existing package-level award
optimisation. For each offer, for each code the offer declares in `documents`
that is required by its lot:

- the offer's own vendor must have at least one visible credential version for
  that code that is `active` and whose half-open validity interval contains the
  offer's `submitted` instant (`valid_from <= submitted < valid_until`);
- timestamps are compared as instants, so equal instants written with
  different offsets compare equal.

If such a credential exists, the code is verified. If not, the offer gains the
reason `unverified_documents:<codes>` (codes sorted, comma-separated). A code
the offer did not declare keeps producing the existing
`missing_documents:<codes>` reason and is not checked for credentials.
Declared non-required codes are ignored. When several credentials can prove
the same code, the one with the lexicographically smallest `credential_id` is
used, deterministically.

An ineligible offer, whether from qualification or any existing rule, never
consumes package budget or vendor capacity. Every existing eligibility, budget,
capacity, tie-break, date-boundary, and award-objective rule is unchanged.

### Output extension

With qualification enabled, and only then:

- top level adds `qualification_enabled: true` and `qualification_as_of` (the
  supplied text);
- each offer keeps `offer_id`, `vendor_id`, `price_cents`, `eligible`,
  `reasons`, `awarded`, and `selection_reasons`, and adds
  `qualification_evidence`, a list sorted by required document code. Each item
  is either
  `{"document_code", "verified": true, "credential_id", "revision",
  "recorded_at"}` for a verified code (the selected version's exact identity),
  or `{"document_code", "verified": false, "reason"}` for an unverified one.
  Reasons are `no_credential`, `outside_validity`, `credential_suspended`, and
  `credential_revoked`.

Qualification failure is reported through `eligible` and `reasons`; it is
separate from `missing_documents`, and it is never used as a
`selection_reasons` value. An eligible offer blocked by budget or capacity
keeps its verified evidence and is not reported as a qualification failure.

### Qualification errors

Invalid JSON, duplicate JSON object keys, non-finite numbers, an unknown or
missing `schema_version`, missing or extra document or credential fields,
duplicate `(credential_id, revision)` pairs, gapped revision numbers, a
`vendor_id` or `document_code` that changes between revisions,
non-increasing `recorded_at`, malformed or offset-less timestamps, an invalid
validity interval, an unknown vendor, an unknown status, and supplying only one
of the qualification arguments all fail the whole run. Errors name the
offending credential, revision, or field. The CLI exits 2 and writes no output
file; an existing output file is never overwritten.
