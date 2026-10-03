# pocketful-settlement

Autonomous payment settlement for the **Dark Factory hackathon, pocketful track**. Three AI agents settle a transfer with no human in the loop, and the service they call is built to be safe: atomic, idempotent, serialized under a lock, and auditable.

The track is graded on transaction safety (atomicity, idempotency, concurrency, recovery), not on features. [`FACTORY.md`](FACTORY.md) is the design document: rationale, what we verified, what we did not, and the known limitations.

## How it works

```
Band room (app.band.ai)
├── Planner      validates a request, approves or rejects; read-only
├── Executor     carries out approved transfers; the only agent that can move money
└── Reconciler   audits the ledger from fresh state; read-only
        ↓ HTTP
FastAPI service       api/main.py
        ↓ ctypes
C ledger core         ledger/ledger.c, ledger/ledger.h
```

- **Agents.** Three Band Remote Agents, each its own Python process (`agents/`, Band SDK with the Anthropic adapter). Each loads a generic role mandate from `mandates/`. Tool access enforces the roles: only the Executor has a transfer tool.
- **Service.** A thin FastAPI layer over a C ledger. Money is `int64` cents, all ledger state sits behind one mutex, and a transfer records both the debit and the credit or neither.
- **Idempotency.** Every transfer carries a `txn_id`. Replaying the same request moves no money and returns `200` with `status: "duplicate"`. Reusing an id with a different sender, receiver or amount returns `409 TXN_ID_CONFLICT`.

## Quick start

Requires `gcc`, Python 3.12 and `uv` (for the agents).

```bash
make build                                      # compile ledger/ledger.so, install pinned deps
python -m uvicorn api.main:app --port 8000      # run from the repo root
```

On startup the service seeds `alice`, `bob` and `carol` with $5,000 each so it is testable immediately (`SEED_ACCOUNTS=0` turns this off, `SEED_BALANCE` changes the amount). **All state is in memory and is lost on restart.** Never run uvicorn with `--workers` greater than 1: each worker would have its own separate ledger.

Try it:

```bash
curl -s localhost:8000/health
curl -s localhost:8000/account/alice
curl -s localhost:8000/verify
```

For a transfer, see the request bodies in `stage-1/test-api.sh` and the models in `api/main.py`.

### Docker

```bash
make docker-build && make docker-run      # builds and runs stage-1/ on :8000
bash stage-1/test-api.sh                  # end-to-end smoke test
```

### Agents

Configure credentials in `agents/agent_config.yaml` (gitignored), start the service, then run each agent in its own terminal:

```bash
cd agents
uv run planner
uv run executor
uv run reconciler
```

They read the service location from `SETTLEMENT_API_URL` (default `http://localhost:8000`). Then message `@planner` in the Band room with a transfer request.

## Tests

```bash
python -m pytest tests/ -q
```

76 tests pass: 21 on the C core (`tests/test_ledger_core.py`) and 55 on the API (`tests/test_api.py`). Against the original code, 33 of the API tests fail.

A ThreadSanitizer stress test lives in `tests/ledger_stress.c`: 18,286 transfers across 8 threads ran with no data races reported and conservation intact. Build it with `-fsanitize=thread` and run it under `setarch "$(uname -m)" -R`.

## API at a glance

| Endpoint | Purpose |
|---|---|
| `GET /health` | Service health and account / transaction counts |
| `GET /account/{id}` | Account balance |
| `POST /transfer`, `POST /execute` | Move money (identical behaviour; `/execute` is a separate route so the Executor's calls are distinguishable in logs) |
| `GET /transaction/{txn_id}` | Look up a recorded transaction |
| `GET /verify` | Consistency check: conservation and negative balances. Returns 200 even on an anomaly |
| `GET /ledger` | Aggregates plus the verification result |

Status codes are chosen so agents can branch on them: `404` unknown account, `409` insufficient balance or `TXN_ID_CONFLICT`, `400` invalid amount, `503` with `retryable: true` for a concurrent conflict, and `200` for both success and an idempotent duplicate.

## Repository layout

```
ledger/        C ledger core (ledger.c, ledger.h): the correctness-critical part
api/           FastAPI service and the ctypes binding (working copy)
agents/        Band Remote Agents: one process per role
mandates/      Generic role mandates loaded by the agents
tests/         pytest suites and the ThreadSanitizer stress test
stage-1/       Self-contained, buildable snapshot (Dockerfile, test-api.sh)
docs/          Status brief and the first end-to-end run log
FACTORY.md     Design document
CLAUDE.md      Guidance for Claude Code
```

The root `ledger/` and `api/` are the working copy. `stage-1/` holds its own copy, and an edit to one does not reach the other.

## Limitations

The full list is in [`FACTORY.md`](FACTORY.md). The ones to know before using it:

- **In memory only.** Nothing survives a restart.
- **Requests are serialized by the event loop** (the handlers are `async def`), so the C mutex is exercised by the thread tests rather than by HTTP traffic.
- **Duplicate lookup is a linear scan** under the global lock: about 25 s to fill 100,000 transactions, measured in a sandbox rather than on our hardware.
- **`verify` does not check for duplicate ids or orphaned transactions.**
- **The retry path has never been triggered**, because the ledger never returns a concurrent-conflict status.
- **Cost figures are not yet measured**, so `FACTORY.md` leaves them as TODO.

## Hackathon notes

Mandates are deliberately generic: they describe roles and decision logic without naming this service's endpoints, fields, error codes or account ids. Each agent discovers the interface through its tools.

## License

MIT.
