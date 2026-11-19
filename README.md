# data-qd-174 | Public procurement bid review

Checks supplier submissions against lot-specific deadlines, mandatory evidence, and budget, then ranks eligible offers by price with deterministic ties. Python 3.11+; standard library only.

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-build.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
.venv/bin/bid-review --input fixtures/input.json --output run/decisions.json
.venv/bin/python -m unittest discover -s tests -v
```

Fixture data is synthetic. This tool performs mechanical checks only and is not a legal procurement decision. MIT licensed.

Fixture summary: 2 lots reviewed, 1 eligible award.
