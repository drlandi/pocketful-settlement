# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A hackathon entry (Dark Factory, pocketful track; submission due Oct 6 2026) for autonomous payment settlement. Three Band Remote Agents (Planner, Executor, Reconciler), registered on app.band.ai, coordinate transfers in a Band room by calling an HTTP service. The service is a C ledger core wrapped by a Python FastAPI layer. The track is graded on **transaction safety**: atomicity, idempotency, concurrency and recovery. Features don't count. `docs/BRIEF-FOR-MUBASHIR.md` holds the current status and stage plan.

## Commands

```bash
make ledger-compile          # ledger/ledger.c -> ledger/ledger.so (gcc -fPIC -pthread)
make build                   # ledger-compile + pip install -r requirements.txt
make clean                   # remove .o/.so and __pycache__

# Run locally from the repo root (the wrapper looks for ./ledger/ledger.so)
python -m uvicorn api.main:app --reload --port 8000

# Tests (76 pass: 21 on the C core, 55 on the API)
python -m pytest tests/ -q
#   tests/test_ledger_core.py   C core
#   tests/test_api.py           API layer
#   tests/ledger_stress.c       ThreadSanitizer stress test (not a pytest file): build with
#                               -fsanitize=thread, run under: setarch "$(uname -m)" -R

# Docker (build context is stage-1/, not the repo root)
make docker-build            # docker build -t pocketful:stage1 stage-1/
make docker-run

# End-to-end smoke test against a running service (curl + python3 only)
bash stage-1/test-api.sh     # API=http://host:port to override
```

Run the tests before and after any change to `ledger/` or `api/`. Against the original, pre-hardening code 33 of the 55 API tests fail, so they do catch regressions in the fixed behaviour.

Env vars: `SEED_ACCOUNTS=0` turns off seeding. By default `alice`, `bob` and `carol` are created at startup with `SEED_BALANCE` (default 5000) dollars. All state is in memory and is lost on restart.

**Never run uvicorn with `--workers` greater than 1.** State is in process memory, so each worker would have its own separate ledger.

## Architecture

```
Band agents (agents/) --HTTP--> api/main.py (FastAPI) --> api/ledger_wrapper.py (ctypes) --> ledger/ledger.so (C)
```

- **`ledger/ledger.h`** is the contract. Money is `int64` cents (`money_t`). All functions are thread-safe under one pthread mutex. Functions return `ledger_status_t` codes and write details into a caller-supplied error buffer. Account and transaction storage are fixed-size arrays (`LEDGER_MAX_ACCOUNTS` and `LEDGER_MAX_TRANSACTIONS`). The header makes no durability claim: state is memory only.
- **`api/ledger_wrapper.py`** is a thin ctypes binding. The `ctypes.Structure` classes and function signatures must match `ledger.h` exactly, so change both sides together. It converts between dollars (API) and cents (C), and the conversion must give **exact cents or reject the input**: never round (`0.285` is a 400, not 28 cents) and never let an out-of-range amount reach C (it once wrapped to 4096 cents). Post-transfer balances are read back from the ledger, never computed in Python. `_find_ledger_so()` searches `/app/ledger`, `./ledger`, `../ledger`, then the path relative to the file.
- **`agents/`** runs each agent as its own Python process (`uv run planner`, `executor`, `reconciler`) via the Band SDK's `AnthropicAdapter`. `agents/src/band_seats/seat.py` loads the mandate verbatim as the adapter's `prompt=` and credentials from `agent_config.yaml` (gitignored). `service.py` defines the HTTP tools; each role module picks its own subset, so tool access enforces who can move money (only `executor.py` gets the transfer tool). Endpoint paths and field names belong in `service.py`, never in the mandates.
- **`api/main.py`** maps ledger status to HTTP status on purpose, so agents can branch on the response: invalid account → 404, insufficient balance → 409, invalid amount (including out-of-range and sub-cent) → 400, concurrent conflict → 503 with `retryable: true`. An idempotent replay returns **200 with `status: "duplicate"`**, not an error. The same `txn_id` with a different sender, receiver or amount returns **409 `TXN_ID_CONFLICT`**. `/transfer` and `/execute` share `_do_transfer`. `/ledger` returns aggregates plus the verification result, not a full dump, because the C API has no JSON dump. `/debug/dump` prints to container stdout.
- **Handlers are `async def`**, so HTTP requests are serialized by the event loop. The C mutex is exercised by the thread tests (`tests/ledger_stress.c`), not by HTTP traffic. Do not describe HTTP-level concurrency as tested.

### Input limits

Account ids must be shorter than 256 bytes (longer ids are rejected, not truncated). Initial balances are capped at 10^14 cents. The transaction-table-full check runs before the debit, so a full table needs no rollback. Keep these limits in step between `ledger.h`, `ledger.c` and `ledger_wrapper.py`.

### Idempotency

The duplicate-`txn_id` check lives in `ledger_transfer()`, inside the mutex, and returns `LEDGER_DUPLICATE_TXN` on a replay. Do not add a Python-side pre-check: it would run outside the lock. Only executed transactions are recorded (status 1), so a record exists if and only if the transfer took effect. The Executor's recovery from a lost response depends on that property. A reused id with different parameters is a conflict (409 `TXN_ID_CONFLICT`), not a duplicate: do not collapse the two, because `duplicate` tells the caller its request already took effect.

The duplicate lookup is a linear scan under the global lock (about 25 s to fill 100,000 transactions, measured in a sandbox). Don't "optimise" it without re-running the stress test and the tests.

### Verification

`ledger_verify_state` compares the sum of balances to the total seeded (conservation) and detects negative balances. It does **not** check duplicate ids or orphaned transactions. Those gaps and the other known ones are under "Known limitations" in `FACTORY.md`.

## Repo layout: staged snapshots

Each stage ships as a self-contained, buildable folder (`stage-1/` … `stage-4/`) with its own `ledger/`, `src/`, `requirements.txt` and `Dockerfile`. Only complete stages are submitted.

- The root `ledger/` and `api/` are the working copy, and the tests in `tests/` run against it. **An edit to the working copy does not reach `stage-1/`, and the reverse.** Decide which one you are changing, and sync when you freeze a stage. Re-run `stage-1/test-api.sh` after a sync.
- The stage Dockerfile copies `src/` and runs `uvicorn src.main:app`. `main.py` puts its own directory on `sys.path`, so `from ledger_wrapper import ...` works in both layouts.
- `stage-2/` through `stage-4/` are empty scaffolds. The root `src/` is empty.

## Agent mandates (`mandates/`)

`planner.md`, `executor.md` and `reconciler.md` are loaded at startup by `agents/src/band_seats/seat.py` as each agent's prompt (from `MANDATES_DIR`). **Hard constraint (disqualifier): mandates must stay generic.** They must not name this project's endpoint paths, field names, error codes or account IDs. They describe roles and decision logic only. Review every mandate edit with that rule in mind. This applies to new codes too: `TXN_ID_CONFLICT` must not appear in a mandate.

## FACTORY.md

`FACTORY.md` is the design document judges read first. Keep it factual: every claim has to be traceable to code or to a verification run. Its "Measured costs" section is intentionally all TODOs until agents have run. Never fill it with estimates. Test and stress-run results belong in its "Verification" section. When a fix lands or a limitation is closed, update "Status at a glance", "Findings fixed" and "Known limitations" to match.
