# Synthetic bid-publish fixtures

Every file here is deterministic synthetic data created for this repository;
no real suppliers, packages, or announcements are represented.

Regenerate all fixtures from the repository root:

```sh
python3 fixtures/publishing/generate_fixtures.py
```

The generator only uses the standard library plus the local `procurement`
package and overwrites exactly the JSON files listed below. Re-running it
produces byte-identical output.

## Files

- `base-mirror.json` — the complete mirror an information station currently
  serves, at position 3. Three suppliers (`V-ALPHA`, `V-BETA`, `V-GAMMA`)
  and three announcements across packages `PKG-A` and `PKG-B` (the same
  lot id `LOT-1` occurs in both packages without colliding).
- `release.json` — the procurement centre's new complete release. `V-ALPHA`
  leaves the directory and `V-DELTA` joins taking over public reference
  `PUB-100`; the two `PKG-A` announcements move to `V-DELTA`; the `PKG-B`
  award is withdrawn; and `PKG-C` gains a new award. The embedded package
  results keep extra `bid-review` projection fields (`eligible`,
  `service_start`, `selection_reasons`, ...) to show they are ignored.
- `bundle.json` — the publication bundle produced with `--max-ops 4`. The
  constrained handover (delete `V-ALPHA`, insert `V-DELTA`, move both
  `PKG-A` announcements) fills the first page; the independent changes fill
  the second.
- `page-0.json`, `page-1.json` — the individual pages, each independently
  savable and applicable.
- `expected-next-mirror.json` — the complete mirror at position 5 obtained
  by applying both pages in order; this is what a correct receiver ends
  with.

## Manual replay

```sh
bid-publish apply --base fixtures/publishing/base-mirror.json \
  --page fixtures/publishing/page-0.json --max-ops 4 \
  --output run/mirror-4.json
bid-publish apply --base run/mirror-4.json \
  --page fixtures/publishing/page-1.json --max-ops 4 \
  --output run/mirror-5.json
```
