# Synthetic bid-cost-compare fixtures

Every file here is deterministic synthetic data created for this repository;
no real suppliers, sites, services, prices or review decisions are represented.

Regenerate all fixtures from the repository root:

```sh
python3 fixtures/cost-compare/generate_fixtures.py
```

The generator only uses the standard library plus the local `procurement`
package and overwrites exactly the six JSON files listed below. Re-running it
produces byte-identical output, and the generator itself fails if any emitted
allocation does not independently re-verify against its annex.

## Files

- `review.json` — a `bid-review`-shaped result for two lots with five offers
  on `LOT-1` (one awarded, one ineligible over-budget quote included on
  purpose) and two offers on `LOT-2`. Only `lot_id`, `offer_id` and
  `price_cents` drive the cost analysis.
- `annex.json` — service categories with per-category unit conversions
  (`day` = 8 hours, `block` = 6 visits, `half_day` = 4 hours) and seven
  quotes:
  - `O1`, `O2`, `O4` (`LOT-1`, 1,500 cents each) are self-consistent and
    match the basket quantities. `O1`'s basket can total 400–700 cents, `O2`
    is fixed at 500, and `O4` ranges 200–300, so `O4` is provably strictly
    cheaper than both.
  - `O3` (`LOT-1`, 12 cents) has both subtotal grand totals equal to the
    price but no feasible allocation: sites A and B need 8 cents that cannot
    fit into categories X and Y totalling 6. The output carries the
    Hoffman-cut proof.
  - `O5` (`LOT-1`, 200 cents) is self-consistent but omits the Y-site basket
    line and quotes the X/Q line at a different quantity, so it is excluded
    from the comparison while keeping a complete sample allocation.
  - `O7` and `O6` (`LOT-2`, 200 cents each) back the boundary-touch case:
    `O7` can reach 60 cents for the selected line and `O6` is fixed at 60,
    so neither a strict-cheaper nor an equality claim is allowed.
- `basket.json` — the `LOT-1` basket: two blocks of Q at site X (12 base
  visits) and 16 hours of P at site Y, comparing all five `LOT-1` offers.
- `touch-basket.json` — the `LOT-2` basket whose two participant intervals
  only meet at the 60-cent boundary.
- `expected-result.json` — the expected output for `basket.json`.
- `expected-touch-result.json` — the expected output for `touch-basket.json`.

## Manual replay

```sh
bid-cost-compare --review fixtures/cost-compare/review.json \
  --annex fixtures/cost-compare/annex.json \
  --basket fixtures/cost-compare/basket.json \
  --output run/cost-result.json
bid-cost-compare --review fixtures/cost-compare/review.json \
  --annex fixtures/cost-compare/annex.json \
  --basket fixtures/cost-compare/touch-basket.json \
  --output run/touch-result.json
```
