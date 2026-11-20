# Bid publish contract

`bid-publish` turns the difference between an information station's current
complete mirror and the procurement centre's complete release into small,
ordered, independently applicable pages. Every page is an all-or-nothing
transaction: a station imports one page, immediately serves queries against
the resulting mirror, and only then imports the next page. Nothing is
fetched from an external service; all inputs and outputs are local files.
Python 3.11+ standard library only.

```sh
bid-publish build --base MIRROR.json --release RELEASE.json \
  --max-ops N --output BUNDLE.json
bid-publish apply --base MIRROR.json --page PAGE.json \
  --max-ops N --output NEXT.json
```

The equivalent reusable Python interface lives in
`procurement.publishing`: `build_bundle(base_mirror, release, max_ops)`,
`apply_page(base_mirror, page, max_ops)`, plus `validate_mirror`,
`validate_release`, `mirror_sha256`, `canonical_mirror_bytes`,
`validate_bundle`, and `load_json_file`.

## Documents

### Mirror

A complete mirror object has exactly these fields:

- `schema_version` — integer `1`;
- `stream_id` — non-empty string identifying the publication stream;
- `position` — non-negative integer starting at 0;
- `suppliers` — list of `{vendor_id, public_ref}` rows;
- `awards` — list of `{package_id, lot_id, offer_id, vendor_id,
  price_cents}` rows.

`vendor_id` is the stable, cross-package identity assigned by the
procurement centre. `public_ref` is the public number the station indexes
on; it may be corrected or reassigned, but two suppliers can never hold the
same `public_ref` in one mirror. Awards are uniquely identified by
`(package_id, lot_id)`; the same lot id under different packages is a
different row. Every award must reference a supplier present in the mirror,
while the directory may contain suppliers that hold no award yet.

### Release

A release object has exactly `schema_version` (integer `1`), `stream_id`, a
complete `suppliers` array with the same row shape, and a complete
`packages` array of `{package_id, result}` items, where `result` is an
existing `bid-review` output object.

Each awarded lot is projected to one award row using the awarded offer's
`offer_id`, `vendor_id`, and `price_cents`. Publication verifies, rather
than re-decides, the award:

- the lot's `award_offer_id` and `award_vendor_id` must match the offer
  carrying `awarded: true`, and no two offers may be awarded in one lot;
- an unawarded lot must have both award IDs equal to `null` and every offer
  unawarded, and produces no award.

Extra fields on the result, lot, and offer objects (qualification evidence,
service dates, eligibility, ...) are allowed and ignored because they are
existing public projections. Extra fields are not allowed on release,
package, supplier, award, mirror, page, bundle, or operation objects.

A package or lot missing from the release, or a lot that becomes
unawarded, removes the corresponding award. The supplier directory is
replaced wholesale by the release directory.

### Validation rules

IDs and public references are non-empty, case-sensitive strings. Amounts
are non-negative integer cents; booleans are never accepted as integers.
The strict JSON loader rejects invalid JSON, duplicate object keys, and
non-finite numbers. Duplicate packages, duplicate lots within a package,
duplicate offers within a lot, duplicate mirror row keys, public-reference
conflicts, awards referencing unknown suppliers, contradictory award
fields, and mismatched `stream_id` values all fail the whole operation.

## How the publisher splits the change

The base mirror and the release target are compared row by row on stable
keys. Each changed key produces exactly one operation: `upsert` carrying
the final whole row, or `delete`. Unchanged rows produce nothing. The
publisher never temporarily deletes an award that must survive, creates a
shadow supplier, or issues a temporary public reference.

A page may release and reuse a public reference internally; intermediate
states inside a page are not checked. At every page boundary, however, all
award references and the uniqueness of public references must hold. The
planner models three precedence rules between operations:

1. an inserted supplier is published before any award that ends on it;
2. an award that stops referencing a supplier disappears before that
   supplier is deleted;
3. a public reference is released by its old holder before the new holder
   acquires it.

Operations joined by a precedence cycle are mutually constrained and must
take effect on one page (the cycle's strongly connected component).
Everything else can be spread across pages in topological order, packed
greedily up to `max-ops` — a positive integer set by the receiver's import
limit. When a constrained component is larger than the limit the entire
publication fails with an error naming the records in the oversized
component(s); no partial bundle is written.

The output is stable: reordering source arrays, mirror arrays, or object
keys never changes the bundle. With no changes, the bundle's `pages` is
`[]` and no empty page is produced.

### Worked example

Supplier `S1` holds public reference `R1`, and announcements `(P1, L1)` and
`(P1, L2)` both reference `S1`. The new directory deletes `S1`, inserts
`S2` using the same `R1`, and moves both announcements to `S2`. Inserting
`S2` first conflicts on `R1`; deleting `S1` first strands the old awards;
moving an award first points at a supplier that does not exist yet. All
four operations therefore form one component and must land on a single
page. An independent price correction on a third announcement can go on a
separate page, so `max-ops 4` publishes the whole change (a four-operation
page plus the correction), while `max-ops 3` must fail even though the
correction could be sent first.

Two retained suppliers swapping public references also form a cycle, so the
swap is one page; announcements keyed on the stable vendor identities need
no operation at all.

## Bundle and page shape

The bundle is `{schema_version, stream_id, pages}`. Each page is a
standalone JSON object:

- `schema_version` — integer `1`;
- `stream_id`;
- `from_position` — the mirror position the page expects;
- `before_sha256`, `after_sha256` — lower-case hex digests;
- `operations` — a non-empty list of at most `max-ops` operations.

An operation is `{table, action, key, row?}`. `table` is `suppliers` or
`awards`; `action` is `upsert` or `delete`; `key` is the vendor id string
or the `[package_id, lot_id]` pair; `row` appears only on upserts and its
identity fields must equal the key.

### Mirror digest

The digest of a mirror at any position is the SHA-256 of the whole mirror
object: both row arrays are first ordered by their stable keys
(`vendor_id`, then `(package_id, lot_id)`), then serialised as UTF-8 JSON
with sorted object keys, non-ASCII characters unescaped, no whitespace,
and no trailing newline — equivalently

```python
json.dumps(mirror, sort_keys=True, ensure_ascii=False,
           separators=(",", ":")).encode("utf-8")
```

Including `position` in the digest means an old page can never be replayed
as a new one even if the rows later return to identical content.

## How the receiver validates a page

`apply_page` uses only the delivered page and the current mirror and
checks, in order:

1. stream id, expected position, and the per-page operation limit;
2. page envelope, digest field shape, and that operations have no
   duplicated keys;
3. each operation against the current rows: deletes must target an
   existing row and upserts must actually change something;
4. the `before_sha256` against the current mirror digest (detects a wrong
   baseline, skipped page, or corruption);
5. the complete candidate mirror: every award references an existing
   supplier and public references stay unique;
6. the candidate digest against `after_sha256`.

Only when every check passes is the next mirror written, with `position`
incremented by one. Empty pages are rejected. The digest rules out
mis-wiring and damage but is not authentication, which is out of scope.

A page whose `after_sha256` honestly matches an invalid candidate is still
rejected: the receiver independently re-validates the candidate's
references and public references rather than trusting the claimed result.

## Errors and file handling

Every validation failure names the offending position, row, or field and
exits the CLI with status 2. Input files are never modified. Outputs are
written via a temporary file in the target directory and published only if
the destination does not exist; an existing output is never overwritten
and a failed run leaves no partial output behind.
