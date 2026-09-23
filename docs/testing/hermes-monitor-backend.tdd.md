# Hermes Monitor backend — TDD evidence

Source plan: kanban task `t_97b0000f`; journeys derived from its acceptance criteria.

## User journeys

- A desktop client can read active workers with stable state, timing, and link fields without receiving message contents.
- A desktop client can read Claude, Codex, and DeepSeek quota data in fixed order, while individual provider failures degrade to `n/d`.
- A click can bypass the five-minute endpoint cache with `fresh=1`.
- Database access is read-only and response serialization cannot expose injected credentials.

## Task report

- RED: `python -m unittest discover -s tests -p test_workers.py -v` produced one intended failure for a historical four-call run that had already recovered.
- GREEN: `source .venv/bin/activate && python3 -m pytest tests/ -q` produced `11 passed in 0.18s`.
- Syntax: `python3 -c "import ast; ast.parse(open('dashboard/plugin_api.py').read())"` exited successfully.

## Test specification

| Guarantee | Test file | Type | Result |
|---|---|---|---|
| Four identical trailing calls are a loop; interrupted or historical runs are not | `tests/test_workers.py` | unit | PASS |
| The strict 300-second stall boundary and loop precedence are preserved | `tests/test_workers.py` | unit | PASS |
| Worker and quota payloads have the agreed exact shapes | `tests/test_workers.py`, `tests/test_quotas.py` | unit | PASS |
| Provider failures degrade independently and injected fake secrets are absent | `tests/test_quotas.py`, `tests/test_plugin_api.py` | unit/integration | PASS |
| `fresh=1` bypasses cache and routes remain relative | `tests/test_plugin_api.py` | integration | PASS |
| SQLite connections reject writes | `tests/test_plugin_api.py` | security integration | PASS |

## Coverage and known gaps

All 11 specified tests pass. No live provider or network calls are made in tests. A percentage coverage plugin was not added; endpoint orchestration, pure transformations, failure paths, cache behavior, secret filtering, and read-only SQLite are directly exercised.

## Merge evidence

Checkpoint `53bab9a` contains the behavior specifications; checkpoint `68afac7` contains the GREEN implementation.
