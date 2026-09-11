# Repository validation

ATHB software completion is established without a live Home Assistant installation or physical
device. Validation has four layers:

1. Frozen numerical/golden tests for ATHB, psychrometrics, radiation and inverse roots.
2. Pure policy, history, normalization, ownership and recovery state-machine tests.
3. Versioned Virtual Installation scenarios VI-001 through VI-030 using the real calculation,
   policy, ownership and broker path with controlled external boundaries.
4. Home Assistant API-contract tests for config entries, entities, services, Recorder and Store.

Run:

```bash
python -m pytest
python -m pytest tests/virtual_installations -v \
  --athb-report=artifacts/virtual-installations.md
ruff format --check custom_components tests tools
ruff check .
mypy
```

The Virtual Installation report command writes a human-readable Markdown report, a JSON companion
and an assertion summary. Missing fixtures, skipped mandatory variants or absent required expected
fields fail the suite. Numerical goldens are checked in; tests do not fetch truth from the network.

The qualification also verifies at least 95% branch-aware total coverage, exact climate payloads,
all 26 named safety invariants, deterministic 40-zone/10,000-event/30-minute stability budgets,
packaging/import identity and HACS structure. Release publication and merging remain separate.
