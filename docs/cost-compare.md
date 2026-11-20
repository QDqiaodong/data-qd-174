# Bid cost comparison contract

`bid-cost-compare` is an internal analysis tool.  It never changes an award or
settlement: the reviewed offer price from `bid-review` is read only as the
total each annex must reconcile to.  Annex line prices are *ranges*, so one
contract total can correspond to many line allocations; the tool reports what
is provable across **all** legal allocations instead of treating one arbitrary
split as fact.

## Command and reusable interface

```sh
bid-cost-compare --review REVIEW.json --annex ANNEX.json \
                 --basket BASKET.json --output RESULT.json
```

Python 3.11+, standard library only.  The reusable interface is:

```python
from procurement import compare_bid_costs, load_json_file, verify_allocation

result = compare_bid_costs(
    load_json_file("REVIEW.json"),
    load_json_file("ANNEX.json"),
    load_json_file("BASKET.json"),
)
```

`compare_bid_costs(review, annex, basket)` takes parsed objects and returns a
JSON-serialisable result.  `load_json_file(path)` is the strict loader (no
duplicate object keys, no non-finite numbers).  `verify_allocation(quote,
allocations, expected_total_cents=None)` independently re-checks any line-level
allocation against one annex's ranges, site subtotals and service subtotals.
The CLI writes the result only to a path that does not already exist; an
existing output is never overwritten and no partial file is left behind.

## Inputs

### Review

`REVIEW.json` is a `bid-review` result.  Only `lots[].lot_id`,
`lots[].offers[].offer_id` and `lots[].offers[].price_cents` are read; award,
eligibility and qualification fields are tolerated and ignored.  Awarded,
unawarded and ineligible offers can therefore all be selected for cost
analysis.  Offer IDs must be unique across the whole review.

### Annex

`ANNEX.json` is `{"services": [...], "quotes": [...]}`.

Each service is `{service_id, base_unit, units}`.  The base unit converts to
one; every other unit is `{unit, numerator, denominator}` with two positive
integers meaning one unit equals `numerator / denominator` base units.
Conversions are interpreted per service category.

Each quote is `{lot_id, offer_id, site_totals, service_totals, lines}` and must
reference a lot/offer pair that exists in the review.

- `site_totals` entries are `{site_id, total_cents}` and `service_totals`
  entries are `{service_id, total_cents}`.  Each list must cover exactly the
  sites and service categories that appear in that quote's lines, once each.
- A line is `{site_id, service_id, quantity, unit, min_cents, max_cents}`.
  At most one line exists per `(site_id, service_id)`.  A missing line means
  the offer does not cover that combination; it is **not** padded to a zero
  row.  Quantities are positive integers; both amount bounds are non-negative
  integers with `min_cents <= max_cents` and inclusive endpoints in integer
  cents.  The unit must be declared for the service.

### Basket

`BASKET.json` is `{lot_id, offer_ids, items}`.  At least two distinct offers
of the named lot are required, and each selected offer must have an annex
quote for that lot.  `items` is a non-empty list of
`{site_id, service_id, quantity, unit}`, each `(site_id, service_id)` at most
once.  Offers may carry additional lines the basket does not select.

## Annex self-consistency check

Every selected quote is checked independently, even if it cannot join the
comparison.  A quote is self-consistent only when all of the following hold:

1. the sum of `site_totals` equals the reviewed offer price;
2. the sum of `service_totals` equals the same price;
3. at least one complete integer line allocation exists in which every amount
   lies within its line range, each site's lines sum to its site subtotal, and
   each category's lines sum to its service subtotal.

The two grand totals matching the price does **not** by itself make an annex
consistent: site and category subtotals can still contradict one another
through the line bounds.

When a subtotal grand total is wrong, `total_mismatches` names which set
(`site_totals` or `service_totals`) failed and gives its actual and expected
totals.  When the totals match but no allocation exists, `conflict` gives a
Hoffman-cut certificate for a site set `S` and a service set `T` (either may
be empty):

- `subtotal_difference_cents` = sum of subtotals of sites in `S` minus sum of
  subtotals of services in `T`;
- `bound_cents` = sum of `max_cents` over lines at sites in `S` and services
  not in `T`, minus sum of `min_cents` over lines at sites not in `S` and
  services in `T`;
- `upper_bound_lines` and `lower_bound_lines` list the exact lines behind the
  bound, each with its bound amount.

The difference is always strictly greater than the bound, so a reviewer can
prove from subtotals and line bounds alone that they cannot hold together.
An inconsistent quote never prevents the other quotes from being analysed.

## Basket matching

For every quote, each basket item must correspond to an existing line and its
quantity, converted exactly to the service's base unit, must equal the
basket's converted quantity.  Comparisons use `fractions.Fraction`; a
fractional normalised quantity is emitted as `{"numerator", "denominator"}`
in lowest terms, and an integer is emitted as a plain integer.  Every
difference is recorded under `basket_match` as either a missing line or a
quantity mismatch (both sides' quantities shown).  Annex prices are never
scaled by quantity, and unselected lines are never subtracted from either set
of subtotals.

## Cost comparison

Only quotes that are self-consistent **and** match every basket quantity
participate.  For each participant the result gives the minimum and maximum
achievable total of the selected items, and each endpoint is reached by its
own complete, legal allocation.  `min_allocation` and `max_allocation` list
every annex line (selected or not) with its concrete `amount_cents`, so the
buyer can re-add the subtotals and the basket total by hand.
`sample_allocation` gives one complete allocation for every self-consistent
quote, including ones excluded from the comparison.

The extrema are bounded transportation problems solved as integer min-cost
flow on the site/service bipartite graph.  All arithmetic is exact integer or
`Fraction`; floating point never represents money or quantities, and the work
depends on the number of lines and augmentations rather than on the cent
magnitude, so large quotes need no per-cent trial.

For every pair of participants:

- `strictly_cheaper` with `cheaper_offer_id` only when that offer's highest
  possible basket total is strictly below the other's lowest;
- `necessarily_equal` when each offer has one possible basket total and the
  two totals are the same;
- otherwise `indeterminate`.  Intervals that merely meet at one boundary
  value are indeterminate, and the note says so.

## Output shape

Top level: `schema_version`, `lot_id`, `basket` (items with normalised base
quantities and the selected offer IDs), `offers`, and `pairwise_verdicts`.
Each entry in `offers` shows the reviewed price, both subtotal grand sums,
`annex_self_consistent`, any `total_mismatches`, any `conflict`, the
`sample_allocation`, the `basket_match` detail, `participates_in_comparison`,
`exclusion_reasons`, the basket interval (or nulls), and both complete
extremal allocations (or nulls).  A missing or contradictory offer never
removes comparisons between the remaining participants.

## Input errors versus analysis findings

Input errors fail the whole run with exit code 2 and write no output:
malformed JSON, duplicate JSON object keys, non-finite numbers, wrong types
(booleans are never accepted as integers), empty identifiers, invalid amounts
or conversion coefficients, inverted ranges, duplicate line or subtotal
identities, unknown units, annex references to lots or offers absent from the
review, subtotals that do not exactly cover the quote's lines, and a basket
with fewer than two offers, an offer without an annex quote, an empty item
list, or duplicate items.

Annex amount contradictions and basket quantity/coverage differences are
**analysis conclusions**, not input errors: they appear in the result and the
run still succeeds.  Re-running identical data, or reordering the services,
units, quotes, lines, subtotals or basket arrays, produces the same output.
