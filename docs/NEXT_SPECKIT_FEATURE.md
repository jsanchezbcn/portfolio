# Next Speckit Feature Handoff

## 1) Start a New Feature Spec

From repo root:

```bash
.specify/scripts/bash/check-prerequisites.sh --json --require-tasks --include-tasks
```

If no active feature exists yet, run your Speckit workflow in order:

1. Create or update `specs/<feature-id>/spec.md`
2. Generate/update `plan.md`
3. Generate/update `tasks.md`
4. Run implement mode against that feature

## 2) Implementation Guardrails

- Keep runtime/debug outputs out of git (`.positions_snapshot_*`, `.greeks_debug_*`, gateway logs)
- Run targeted tests for touched modules first, then broader suite
- Update `tasks.md` checkboxes as each item is completed

## 3) Ready-to-use Validation Commands

```bash
PYTHONPATH=. ./.venv/bin/pytest tests/test_unified_position.py tests/test_regime_detector.py tests/test_market_data_tools.py tests/test_portfolio_tools.py tests/test_ibkr_adapter.py tests/test_tastytrade_adapter.py tests/test_end_to_end.py -vv -x
```

Optional integration checks:

```bash
PYTHONPATH=. ./.venv/bin/pytest tests/integration -q
```

## 4) Dashboard Smoke Run

```bash
chmod +x start_dashboard.sh && ./start_dashboard.sh
```

Open Streamlit at `http://localhost:8506`.

## 5) Completion Checklist

- [ ] All spec tasks completed and checked in `tasks.md`
- [ ] Focused + integration tests green (or clearly documented skips)
- [ ] Demo script/output captured
- [ ] Commit includes only intentional source/docs changes
