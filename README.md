# data-qd-174 | Public procurement bid review

Checks supplier submissions against lot-specific deadlines, mandatory evidence, and lot budget, then builds one executable award plan for a whole procurement package. Eligible offers are chosen across lots under a shared package budget and per-vendor concurrency limits, optimising awarded lot count first, then total price, then a deterministic offer-ID sequence. Python 3.11+; standard library only.

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-build.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
.venv/bin/bid-review --input fixtures/input.json --output run/decisions.json
.venv/bin/python -m unittest discover -s tests -v
```

Input adds `package_budget_cents` and `vendors` to the existing `lots` and `offers`; each lot now carries `service_start` and `service_end` (`YYYY-MM-DD`, start before end) and each vendor carries a positive `concurrent_lot_capacity`. Service intervals are half-open, so abutting dates do not overlap. The award picks at most one eligible offer per lot; ineligible offers never use budget or capacity. See `docs/contract.md` for the full contract.

Eligibility and selection are reported separately: an eligible offer excluded by package budget or vendor capacity keeps `eligible: true` with empty `reasons` and gains a `selection_reasons` entry. The `package` block reports the budget, awarded total, remainder, and each vendor's peak concurrent lots against capacity.

Fixture data is synthetic. This tool performs mechanical checks only and is not a legal procurement decision. MIT licensed.

Fixture summary: 2 lots reviewed, shared package budget 60,000 cents, 1 eligible award at 42,000 cents (18,000 remaining).
