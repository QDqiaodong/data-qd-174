# data-qd-174 | Public procurement bid review and settlement

Checks supplier submissions against lot-specific deadlines, mandatory evidence, and lot budget, then builds one executable award plan for a whole procurement package. Eligible offers are chosen across lots under a shared package budget and per-vendor concurrency limits, optimising awarded lot count first, then total price, then a deterministic offer-ID sequence. Python 3.11+; standard library only.

After the award, `bid-settle` freezes that decision in a local ledger and
continues the post-award lifecycle: versioned acceptance receipts, invoices,
per-invoice cash, recovery of overpayments against the same supplier's later
payables, and batch reports. See [`docs/settlement.md`](docs/settlement.md).

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-build.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
.venv/bin/bid-review --input fixtures/input.json --output run/decisions.json
.venv/bin/bid-settle init  --input fixtures/settlement/package.json --state run/state.json
.venv/bin/bid-settle apply --state run/state.json --batch fixtures/settlement/batch-1.json --output run/b1.json
.venv/bin/python -m unittest discover -s tests -v
```

`bid-review` is unchanged: same command, input contract, output fields, award
rules, and exit behaviour. `bid-settle` is a separate command and module built
on the same package; all amounts are integer cents and all state is local.

Input adds `package_budget_cents` and `vendors` to the existing `lots` and `offers`; each lot now carries `service_start` and `service_end` (`YYYY-MM-DD`, start before end) and each vendor carries a positive `concurrent_lot_capacity`. Service intervals are half-open, so abutting dates do not overlap. The award picks at most one eligible offer per lot; ineligible offers never use budget or capacity. See `docs/contract.md` for the full contract.

Eligibility and selection are reported separately: an eligible offer excluded by package budget or vendor capacity keeps `eligible: true` with empty `reasons` and gains a `selection_reasons` entry. The `package` block reports the budget, awarded total, remainder, and each vendor's peak concurrent lots against capacity.

Bid review can additionally verify supplier qualification evidence from a local, buyer-exported revision history:

```sh
.venv/bin/bid-review --input fixtures/qualification/package.json --output run/qualified.json \
  --qualification-file fixtures/qualification/qualification.json \
  --qualification-as-of 2026-03-05T00:00:00+00:00
```

Both qualification flags are optional and must be supplied together; omitting them preserves the original behaviour exactly. A declared required code that no active credential proves at the offer's submission instant makes the offer ineligible with `unverified_documents:<codes>` and per-code `qualification_evidence`. See `docs/contract.md` for the full qualification contract.

Fixture data is synthetic. This tool performs mechanical checks only and is not a legal procurement decision. MIT licensed.

Fixture summary: 2 lots reviewed, shared package budget 60,000 cents, 1 eligible award at 42,000 cents (18,000 remaining).

Settlement fixture summary: 3 awarded lots (L1: 100 cents over three days,
L2: 50, L3: 40). Batch 1 pays 67 cents on I1; batch 2 revokes day two and uses
I1's 33-cent recovery to clear I2 before paying 17 cents cash with 3 cents
unused; batch 3 restores day two and reopens 33 cents due on I1.
