# Automated settlement scenario tests — 2026-10-03

Branch: `mubashir-scenario-tests`, created from `mubashir-work` at `9f02eea`. Starting working
tree was clean; existing mandates and scenario document were already present.
No backend, mandate, tool, dependency, or existing run-log changes were made.

## Reproduce

From the repository root, with the existing Python 3.11 environment and GCC:

```bash
.venv/bin/python --version
.venv/bin/python -m pytest --version
.venv/bin/python -m pytest -q tests -o faulthandler_timeout=15
```

Observed Python 3.11.16 and pytest 7.4.3. After removing the input-quantity
characterization assertion, the rerun exited 1:
**25 passed, 2 failed, 4 warnings in 1.02s**. The four warnings concern existing
FastAPI lifecycle deprecations. The two failures are deliberately ordinary failed
assertions, not skips or expected failures:

| Test case | Response transferred quantity | Recorded quantity and exact debit/credit | Result |
|---|---:|---:|---|
| `test_success_response_reports_actual_applied_quantity[0.006-1]` | 0.006 dollars | 1 cent | Failed |
| `test_success_response_reports_actual_applied_quantity[0.125-12]` | 0.125 dollars | 12 cents | Failed |
| Same assertion for 0.01 dollars | 0.01 dollars | 1 cent | Passed |

Evidence: `tests/test_settlement_scenarios.py`,
`test_success_response_reports_actual_applied_quantity`. Each failing case first
checks the recorded cents, exact balances, unchanged 10,000-cent aggregate, and
one transaction. It then compares the reported quantity using Decimal, without
rounding away the discrepancy. `api/ledger_wrapper.py`, `transfer`, returns the
original input quantity while `dollars_to_cents` rounds the applied quantity;
`api/main.py`, `_do_transfer`, forwards that response value.

The initial sandbox run stalled after ten concurrency cases when TestClient's
thread-to-event-loop bridge did not progress. Diagnostic command:

```bash
timeout 20s .venv/bin/python -m pytest -vv tests/test_settlement_scenarios.py::test_matching_replay_moves_nothing -o faulthandler_timeout=5
```

The successful full run above used approved execution outside the sandbox. The
initial stalled run was interrupted. No test contacted port 8000, opened a service
listener, or used the already-running API. In-process TestClient still needs its
runtime's thread/event-loop facilities; sandbox execution may require approval.

## Coverage and interpretation

- Five two-way concurrent drain cases each permit exactly one 8,000-cent transfer
  against a 10,000-cent source. The loser has no record; the winner's parties,
  completed status, quantity, and resulting integer balances are checked.
- Five eight-way repeated-identifier cases each produce one success and seven
  duplicates, one record, one debit/credit, and conserved integer total.
- Matching API replay preserves its original immutable record and all balances.
- Three changed-content replay cases vary amount, destination, or direction.
  **Characterization:** the current API returns a duplicate for the identifier,
  retains the original record, and applies no new effect. This confirms missing
  content-conflict detection; it does not establish that the new request succeeded.
  No undocumented HTTP conflict policy was imposed. Content validation remains a
  backend follow-up for Landi, with approval matching still required in mandates.
- Four rejection cases cover insufficient funds, missing source, missing
  destination, and self-transfer. They preserve nonempty history, exact balances,
  counts, and conservation, and leave no rejected transaction record.
- Six precision characterization cases cover 0.01, 0.004, 0.005, 0.006, 0.125,
  and `0.1 + 0.2`. Current results are respectively 1, 0, 0, 1, 12, and 30 cents;
  quantities rounding to zero are rejected. Valid applied quantities are checked
  against stored cents, balance deltas, and response balances. This characterization
  no longer asserts that the reported transferred quantity equals the input;
  `test_success_response_reports_actual_applied_quantity` remains unchanged and
  independently detects inaccurate transferred-quantity reporting.

Rounding versus rejecting sub-cent input remains a product-policy question. Tests
characterize the documented half-even conversion rather than invent a reject-all
policy. Response accuracy is a separate safety requirement and fails as above.
Self-transfer policy is explicit in `ledger/ledger.c`, `validate_inputs`: source
and destination must differ, with rejection before effects.

## Isolation, lifecycle, and concurrency evidence

`tests/conftest.py` compiles current root `ledger/ledger.c` into temporary plain
and instrumented shared objects. Build commands are generated as:

```bash
gcc -fPIC -shared -pthread ledger/ledger.c -o <temporary-build>/plain.so
gcc -fPIC -shared -pthread ledger/ledger.c tests/overlap_probe.c -Wl,--wrap=pthread_mutex_lock -o <temporary-build>/overlap.so
```

Each test copies a library to a distinct temporary path, providing separate C
globals and a fresh static mutex, initializes it before use, and destroys it only
after workers have joined. TestClient overrides only the wrapper dependency; it
does not run production startup or shutdown. The fixture owns initialization,
account provisioning, and cleanup, preventing default seeding or singleton use.
Repository ledger binaries are neither loaded nor overwritten.

Concurrency does not rely on concurrent HTTP scheduling. ctypes CDLL releases
the GIL; worker threads call the actual `ledger_transfer` directly. The test-only
linker wrapper rendezvous occurs when each valid call has entered the ledger and
reached its mutex acquisition. It waits until every participant is inside that
call, then invokes the real pthread lock unchanged. Assertions require all C
arrivals and no five-second rendezvous timeout. This proves overlapping C calls
while the critical section remains serialized as designed. It deliberately forces
contention rather than measuring uninstrumented scheduling or throughput.

## Limits

These finite tests check root implementation behavior, not staged Docker copies,
all schedules, storage caps, integer overflow, durability, or complete audit
history. Conservation uses the fixture's trusted 10,000-cent baseline, not the
service's record-derived balance flag. Record count plus queried known identifiers
does not enumerate unknown global records. No tests verify agent orchestration,
approval freshness, lost-response recovery, restart recovery, or prompt-enforced
retry budgets. The broader scenarios in `mandate-review-and-test-scenarios.md`
remain proposals outside the implemented service/ledger cases.
