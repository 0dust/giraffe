# Stop-cause outcome fixture

`stop_cause_outcomes.json` contains the normalized `outcome_bytes` payloads
from runner revision `544bc84282d160fa013e21b7a25dfdff7b2a40bc` for the nine
unchanged scenarios in `tests/test_stop_cause_bytes.py`. IDs, host resource
measurements, and timing are excluded; verdicts, prose, attribution, request
outcomes, and stop events remain byte-compared after canonical JSON encoding.
CI reads the JSON directly and needs neither Git nor the old revision.
The load-stop scenario spaces arrivals and delivers the stop after the completed
request releases its slot. Its monitor is held until cleanup so it exercises
cooperative stopping consistently, without racing forced task cancellation.

To regenerate from the repository root, using a checkout that has that revision:

```sh
git show 544bc84282d160fa013e21b7a25dfdff7b2a40bc:src/giraffe/runner.py > /tmp/giraffe-544bc842-runner.py
PYTHONPATH="$PWD/src" HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  .venv/bin/python scripts/generate_stop_cause_outcomes.py \
  /tmp/giraffe-544bc842-runner.py tests/fixtures/stop_cause_outcomes.json
```

Use a Python environment with the project's test dependencies installed. Review
fixture changes against the pinned runner rather than accepting current output.
