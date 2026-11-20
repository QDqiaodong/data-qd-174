# Post-award settlement ledger

`bid-settle` continues a procurement package after `bid-review` has chosen
suppliers.  It freezes the award in a local ledger, then accepts business
batches of acceptance receipts, invoices, and cash settlements.  It is a
standard-library-only, single-writer, local tool.  All data in this repository
is synthetic; the tool never contacts a payment system or any external service.

## Commands

```sh
bid-settle init  --input PACKAGE.json --state STATE
bid-settle apply --state STATE --batch BATCH.json --output REPORT.json
```

`bid-review --input ... --output ...` is unchanged: same arguments, same input
contract, same award rules, same exit behaviour.

Both commands are thin wrappers around the reusable Python interface in
`procurement.settlement`:

```python
from procurement.settlement import initialize_ledger, apply_batch, SettleError

state = initialize_ledger("package.json", "state.json")
report = apply_batch("state.json", {"batch_id": "B1", "events": []},
                     "report.json")
```

`apply_batch` also accepts a path to a batch JSON file.  It returns the report
object.  Errors raise `StateError`, `BatchError`, or `ExportError`, all
subclasses of `SettleError` (and of `ValueError`).  The CLI exits 2 on any
error.

All data is local synthetic data.  The ledger and reports are ordinary JSON
files; amounts are integer cents and money arithmetic never uses floats.

## 1. Freezing a contract

`init` reads the existing package input, runs the existing `review` exactly
once, and stores the original package plus that award result in a new state
file.  Later changes to the input file, receipts, or cash budgets never re-run
the award.

- The state path must not already exist; `init` refuses instead of
  reinitialising.
- Invalid input is validated before anything is published, so a failed `init`
  leaves no file that could be mistaken for a successful ledger.
- Only awarded lots become contracts.  The contract vendor is the awarded
  offer's `vendor_id`; the contract total is that offer's `price_cents`.

The corpus service interval is half-open: `service_start` is the first service
date and `service_end` is not a service date.  Let `D` be the number of
calendar days in `[service_start, service_end)`.  The contract total `P` is
split so every day receives `P // D` cents and the earliest `P % D` days each
receive one extra cent.  Daily amounts are therefore deterministic and sum to
exactly `P`, including a zero-price contract.  Real calendar dates are used, so
months, years, and leap days are handled correctly.

Every receipt range and invoice range must be non-empty and lie completely
inside the corresponding contract service interval.

## 2. Batches and events

A batch is:

```json
{"batch_id": "B1", "events": []}
```

`events` is ordered by business occurrence and may be empty.  `batch_id`,
`event_id`, `receipt_id`, and `invoice_id` must be non-empty strings.  Lot
references must name an awarded contract.  Boolean is never accepted where an
integer is required.

Every event has a unique `event_id` and one `kind`:

- `receipt` — `receipt_id`, `revision`, `lot_id`, `accepted_ranges`, where each
  range is `{"from_date": "YYYY-MM-DD", "to_date": "YYYY-MM-DD"}`.
  The first revision is 1 and every later revision is exactly one greater.  A
  receipt's `lot_id` is immutable.  A new revision completely replaces the old
  one; an empty range list revokes all acceptance from that receipt.  Ranges in
  one receipt may not overlap, but may abut.  Different receipts may overlap,
  and a lot's accepted dates are the union of the latest revision of every
  receipt.  A date is earned at most once.  Revoking one receipt does not
  remove dates another receipt still confirms.
- `invoice` — `invoice_id`, `lot_id`, `from_date`, `to_date`.  The invoice
  claims the sum of the contract's daily amounts over its range; callers cannot
  supply an amount.  Invoice IDs are globally unique and immutable.  Different
  invoices of the same lot may not overlap, but may abut.  An invoice may be
  issued before acceptance; unaccepted days stay pending acceptance and are not
  errors, deletions, or early payments.  Invoice cancellation and tax handling
  are out of scope.
- `settle` — non-negative integer `cash_budget_cents`, the most cash this
  settlement may actually spend.  It uses the facts from all previously
  accepted events; later events never retroactively change this settlement's
  payment details.

Events are applied in list order and each accepted event gets the next
ledger-wide `seq`, starting at 1.  Invoice order is the `seq` of the invoice
event.  `event_id` values may not be reused across batches.

A batch is all-or-nothing.  Any unknown or unawarded lot, document identity
conflict, skipped or repeated receipt revision, out-of-range date, overlapping
invoice, unknown kind, unknown field, or other format error rejects the whole
batch.  Earlier valid events in that batch also leave no trace, and the failed
`batch_id`, `event_id` values, and sequence numbers remain unused.  Errors name
the batch, event index, document, and field where applicable.

## 3. Acceptance corrections, recovery, and cash

For each invoice:

- `claimed_cents` = the invoice's contract amount over its whole date range.
- `earned_cents` = the contract amount over days in its range that are
  currently accepted.
- `cash_paid_cents` = immutable total cash actually paid on this invoice.
- `offset_in_cents` = immutable total offset used to settle this invoice.
- `recovered_cents` = immutable total recovered from this invoice and given to
  other invoices.
- `net_settled_cents = cash_paid_cents + offset_in_cents - recovered_cents`.
- `due_cents = max(earned_cents - net_settled_cents, 0)`.
- `recoverable_cents = max(net_settled_cents - earned_cents, 0)`.
- `pending_acceptance_cents = claimed_cents - earned_cents`.

All amounts are integer cents.

A `settle` first performs offsets, then spends cash:

1. Within each vendor, and never across vendors, positive `recoverable_cents`
   on older invoices is applied to positive `due_cents` on other invoices.
   Sources and targets are each chosen in ascending invoice `seq`; each transfer
   is `min(recoverable, due)`.  This repeats until no pairable balance remains
   within the vendor.  Each transfer increases the source's `recovered_cents`
   and the target's `offset_in_cents`, and is reported as
   `from_invoice_id`, `to_invoice_id`, `vendor_id`, `amount_cents`.  Offsets do
   not consume cash, and are performed even when the cash budget is zero.
   Recovered amounts cannot be recovered twice.
2. Remaining due invoices of all vendors are paid in ascending invoice `seq`
   using the batch's `cash_budget_cents`.  Each payment is the smaller of its
   due and the remaining budget, so partial payment is allowed.  Each payment
   is reported with `invoice_id`, `vendor_id`, `amount_cents`.  Unused budget is
   reported, never carried into a later settlement and never counted as paid.
   A settlement with no due invoices succeeds.

A receipt correction may lower `earned_cents` on an already settled invoice.
This creates a recoverable balance; prior cash and offset history is preserved
and only a later `settle` actually recovers it.  Such a correction is valid and
must not be rejected merely because payment already happened.  If acceptance is
restored, due is recomputed from the current net settled amount, so a paid
invoice can become due again.  An invoice may simultaneously have pending
acceptance and recoverable amounts; the report keeps them as separate fields.

## 4. Import retries, persistence, and reports

The importer may retry a batch after a report-export failure.  For a
successfully accepted `batch_id`, a request that parses to the same structure
and values with the same event order — even with a different JSON object key
order — is an exact replay.  An exact replay executes nothing, allocates no
sequence numbers, and returns the report persisted at acceptance time, even if
other batches arrived in between.  The same `batch_id` with different content,
including changing an old batch to empty `events`, is a conflict.

Exact replay is recognised before validating old revisions or event IDs,
because those facts have legitimately moved on in the current ledger.

Invalid JSON, duplicate JSON object keys, and non-finite numbers (`NaN`,
`Infinity`) are rejected so that ambiguous input can never become replay
credentials.  Unknown fields are rejected too.

The ledger is one JSON state file on local disk.  Business state, accepted
batch identity/content, and its report are committed atomically together via a
temporary file and rename; nothing depends on process memory.  Only a single
writer is supported.  An unknown state schema, corrupt JSON, or incomplete
state is an error; it is never silently reinitialised.

The report is a stable JSON object with no clock or random fields:

- `schema_version` = 1, `batch_id`
- `events`: this batch's `event_id` → `seq` (with `kind`)
- `settlements`: per `settle`, `cash_budget_cents`, `cash_paid_cents`,
  `unused_budget_cents`, `offset_cents`, itemised `offsets` and
  `cash_payments`
- batch totals `cash_budget_cents`, `cash_paid_cents`, `unused_budget_cents`,
  `offset_cents`
- `lots`: contract acceptance amounts in ascending `lot_id` order, including
  the deterministic `daily_amounts`, `accepted_cents`, and invoice claims
- `invoices`: every invoice in ascending invoice `seq` order with
  `claimed_cents`, `earned_cents`, `cash_paid_cents`, `offset_in_cents`,
  `recovered_cents`, `net_settled_cents`, `due_cents`, `recoverable_cents`,
  and `pending_acceptance_cents`.

Report amounts reconcile: invoice `cash_paid_cents` sums to the batch's paid
cash, offsets name both sides, and contract daily amounts sum to the contract
total.  The CLI prints the same report object to stdout on success.

Reports are published whole: the tool writes a complete temporary file and
links/renames it into place, so no half-written JSON is ever visible.  An
existing target with different bytes is not overwritten.  If export fails after
the business commit, the command exits 2 and states explicitly that the batch
WAS ACCEPTED and may be replayed to export it without paying twice.  If the
target already holds exactly this batch's report bytes, replay succeeds.

### Committed-but-not-exported vs. not accepted

- **Committed, export failed**: events, sequence numbers, offsets, and payments
  are durable.  Retrying the identical batch is an exact replay: no new money
  moves; the report is rewritten.
- **Not accepted**: no event, sequence number, batch ID, or money movement was
  recorded, and no report was created.  Fix the batch and use a fresh ID or
  correct the same unused ID.

## Example

`fixtures/settlement/` contains a synthetic package and three batches.  L1 is
three days for 100 cents, so daily amounts are 34, 33, 33.  I1 invoices the
first two days and is paid 67 cents after acceptance.  Revising R1 to revoke
day two makes 33 cents recoverable.  L2's accepted invoice I2 has 50 cents due;
a 20-cent settle first offsets 33 cents from I1 to I2, then pays 17 cents cash
and leaves 3 cents unused.  A third batch restores day two, and I1 again shows
33 cents due.

```sh
bid-settle init  --input fixtures/settlement/package.json --state run/state.json
bid-settle apply --state run/state.json --batch fixtures/settlement/batch-1.json --output run/b1.json
bid-settle apply --state run/state.json --batch fixtures/settlement/batch-2.json --output run/b2.json
bid-settle apply --state run/state.json --batch fixtures/settlement/batch-3.json --output run/b3.json
```

## Errors

The CLI exits 2 for all errors and writes a specific message to stderr.
Rejected batches commit nothing; a committed batch whose report cannot be
exported is identified as accepted and safely replayable.
