# FACTORY.md: pocketful-settlement

**Dark Factory hackathon · pocketful track · submission due Oct 6 2026**

Three AI agents (Planner, Executor, Reconciler) settle payments with no human in the loop. Each is a Remote Agent registered on app.band.ai and running as its own Python process (`agents/`, Band SDK with the Anthropic adapter). They coordinate in a Band room and call an HTTP settlement service. The service is where correctness lives: a C ledger core (integer cents, one mutex, atomic transfers) wrapped in a Python FastAPI layer.

The track is graded on transaction safety: atomicity, idempotency, concurrency and recovery. This document explains how the design addresses each of those, what we have measured, and what we have not. Every claim below traces to code or to a run listed under "Verification".

---

## Status at a glance

| Area | Status | Evidence |
|---|---|---|
| Basic transfers, atomicity (stage 1, eligibility requirement) | **Done** | `stage-1/test-api.sh` passes in Docker |
| Idempotency, signalled from C under the lock | **Done** | Replay returns 200 `duplicate`; same `txn_id` with different parameters returns 409 `TXN_ID_CONFLICT` (Docker, `test-api.sh`; API tests) |
| Concurrency | **Verified on the C core** | ThreadSanitizer stress: 18,286 transfers, 8 threads, no races, conservation held. HTTP requests are serialized by the event loop (limitation 5) |
| Verification (conservation, negative balances) | **Done**, with gaps | `verify` now compares balances to total seeded; duplicate-id and orphan checks are not implemented (limitation 6) |
| Input hardening (amount range, precision, id length, initial balance cap) | **Done** | See "Findings fixed" and the test suites |
| Retry path (transient failure → Executor retry) | **Not exercised** | Limitation 7 |
| Automated tests | **76 passing** | 21 on the C core, 55 on the API |
| Band room, three Remote Agents, recording | **First run done and recorded** | `docs/first-run-log.md` (run predates the hardening fixes below) |
| `band-room-export.json` | **Not yet in the repo** | |
| Measured costs | **TODO** | See "Measured costs" |

Each stage ships as a self-contained, buildable folder (`stage-1/` … `stage-4/`). We submit only completed stages. The verification results below were produced on the working copy (root `ledger/` and `api/`) and, for the Docker rows, on `stage-1/`.

---

## Architecture

```
        External request
               ↓
        Band room (app.band.ai)
        ├── Planner      reads state, approves or rejects; never writes
        ├── Executor     the only agent that changes state; retries transient failures
        └── Reconciler   audits recorded state independently; never writes
          (each agent: own Python process, Band SDK + Anthropic adapter, agents/src/band_seats/)
               ↓ HTTP / JSON
        FastAPI service            api/main.py
               ↓ ctypes
        Ledger core (C, .so)       ledger/ledger.c, ledger/ledger.h
```

---

## Agent setup

The three agents are Band Remote Agents, registered on app.band.ai. Each runs as its own Python process from `agents/` (`uv run planner`, `uv run executor`, `uv run reconciler`), with its own agent ID and API key from `agents/agent_config.yaml` (gitignored). `agents/src/band_seats/seat.py` builds each one the same way: a Band SDK `AnthropicAdapter` whose `prompt=` is the agent's mandate file from `mandates/`, read verbatim, and whose `additional_tools` are that agent's HTTP tools. Using `prompt=` rather than `system_prompt=` keeps the SDK's base instructions (how to reply and @mention in the room) and appends the mandate after them. The tools call the service at `SETTLEMENT_API_URL` (default `http://localhost:8000`).

The tools live in `agents/src/band_seats/service.py`, and each role module picks its own subset. Tool access therefore enforces the mandates rather than merely describing them:

| Agent | Mandate | Can change state? | Tools (service calls) |
|---|---|---|---|
| Planner | `mandates/planner.md` | No | health (`GET /health`), account lookup (`GET /account/{id}`), transaction lookup (`GET /transaction/{txn_id}`) |
| Executor | `mandates/executor.md` | **Yes, the only one** | health, execute transfer (`POST /execute`), transaction lookup (for a lost response) |
| Reconciler | `mandates/reconciler.md` | No | verify (`GET /verify`), ledger overview (`GET /ledger`), account lookup, transaction lookup |

`POST /transfer` and `POST /execute` behave identically. `/execute` is a separate route only so the Executor's calls show up distinctly in service logs and in the room trace.

**Handoffs**, as the mandates define them:

1. The **Planner** receives a request. It checks that every referenced entity exists, that the amount is positive and covered by the sender's balance, that sender and receiver differ, and that the ID has not already been used. It then approves or rejects, citing the state it saw.
2. The **Executor** accepts only approved work. It submits the request with the **original ID unchanged**, classifies the result as succeeded, already recorded, or failed, and retries only transient failures. It notifies the Reconciler in every case.
3. The **Reconciler** reads fresh state (never another agent's summary). It checks conservation, negative balances, duplicates, incomplete operations and counts, then reports "consistent" with numbers or "anomaly" with specifics. It never repairs state; anomalies go to a human.

**Mandate constraint.** The mandates are deliberately generic. They describe roles and decision logic without naming this service's endpoints, field names, error codes or account IDs, because naming them is a disqualifier on this track. Each agent has to discover the service's interface on its own.

**Submission artefacts:** a recording of the first end-to-end run exists (`docs/first-run-log.md`). `band-room-export.json` is not yet in the repo. A missing recording is a stated disqualifier.

---

## The collaboration

**The crew.** Three Band Remote Agents, each a Band SDK `AnthropicAdapter` process running `claude-sonnet-5-5` (the default in `seat.py`, overridable with `SEAT_MODEL`):

- **Planner** decides whether a request may proceed. It reads state and never moves money.
- **Executor** carries out approved transfers, exactly once. It is the only agent with a transfer tool.
- **Reconciler** audits the ledger from fresh state after the fact. It is read-only.

**Who talks to whom.** Routing is by @mention. Under the Band SDK's base instructions, an @mention triggers the mentioned agent's turn, and an agent that is not mentioned stays silent. A human mentions `@planner`. The Planner hands its decision to the Executor, the Executor notifies the Reconciler, and the Reconciler reports the audit, which the Planner checks against what it approved. The Reconciler is deliberately left out of the Planner's approval: its mandate forbids auditing against another agent's summary, so it is brought in only by the Executor, after the fact, and reads the ledger itself. The chain also holds when a message is misrouted. In one session a human sent the request to `@executor` directly. The Executor refused to act without a Planner approval and tagged the Planner itself.

**One typical flow** (first run, `txn_100`):

Human `@planner` "$50 alice→bob, txn_100" → Planner checks health, both accounts and the txn_id (404, unused) → APPROVED to Executor → Executor executes once, txn_id unchanged → Reconciler audits: txn_100 recorded once, alice $4,950, bob $5,050, total $15,000 unchanged → Planner confirms and closes. About 30 seconds, no human input after the first message.

**The delete test.** Without the room nothing settles: the Planner has no tool that moves money, and the Executor refuses any transfer that arrives without a Planner approval.

---

## Design rationale

### Why a C ledger core

- **Money is `int64` cents** (`money_t` in `ledger.h`), so there is no floating-point arithmetic on balances. The Python layer converts dollars to cents once, at the boundary, and either produces exact cents or rejects the input: `0.285` is rejected with 400 `LEDGER_INVALID_AMOUNT` rather than rounded. Amounts too large to represent as cents are rejected with 400 as well (see "Findings fixed", items 1 and 2).
- **One `pthread_mutex_t` guards all ledger state.** Every public function, reads included, takes the lock, so concurrent transfers are serialized and never interleave. We chose serialization over finer-grained locking because correctness is graded and throughput is not.
- **Atomicity is an explicit contract** in `ledger.h`. `LEDGER_OK` means both the debit and the credit landed and a transaction record was written; any error means neither leg happened. In `ledger_transfer`, the validation, duplicate check, capacity check, balance check, debit, credit and record write all happen inside one critical section. The transaction-table-full check runs **before** the debit, so a full table is rejected with nothing to undo. No other thread can observe a half-applied transfer.
- **Inputs are bounded.** Account ids of 256 bytes or more are rejected rather than truncated, and an initial balance is capped at 10^14 cents (the cap that keeps balance arithmetic clear of overflow).

### Why a thin ctypes layer

`api/ledger_wrapper.py` mirrors the C structs as `ctypes.Structure`s and declares every exported signature. Apart from input conversion and range checks at the boundary, it adds no business logic. Post-transfer balances are **read back from the ledger**, never computed in Python, so a response always shows what the ledger actually holds.

### Idempotency

Every transfer carries a caller-supplied `txn_id` (1–63 characters). The C ledger checks whether that ID is already recorded, **inside the same lock as the transfer itself**. So two concurrent attempts with the same ID cannot both move money: one executes and the other sees the record.

This is the protection the Executor's retry policy depends on. The Executor always resends the same ID, so a retry after a lost response either finds the original record or performs the transfer for the first time. It never does both.

**A reused ID is not always a replay.** A replay means the same request sent again. If a `txn_id` that is already recorded arrives with a different sender, receiver or amount, the service returns **409 `TXN_ID_CONFLICT`**. Earlier it returned `duplicate`, which told the caller its (different) request had already taken effect when it had not. Docker check: `txn_001` reused with a different amount returned 409 `TXN_ID_CONFLICT`.

**The Stage 2 change.** In Stage 1 the C ledger already refused to move money twice, but it reported the replay as `LEDGER_OK`, so a caller could not tell a retry from a first attempt. The Python layer compensated with a lookup before each transfer. That lookup ran outside the lock and cost a linear scan. The fix moves the signal into C: the existing lookup under the lock now returns `LEDGER_DUPLICATE_TXN`, and the Python pre-check is removed.
*Verified in the `stage-1/` Docker image with `stage-1/test-api.sh`: the first transfer returns `success`, and replaying the same ID returns HTTP 200 with `status: "duplicate"` / `LEDGER_DUPLICATE_TXN`. After the replay, `alice` is still at 4950.00 and `num_transactions` is still 1.*

### Status codes built for agents

`main.py` maps ledger statuses onto HTTP status codes so an agent can branch on the code without parsing prose:

| Ledger status | HTTP | Meaning for the Executor |
|---|---|---|
| `LEDGER_OK` | 200, `status: "success"` | Took effect |
| `LEDGER_DUPLICATE_TXN` | **200**, `status: "duplicate"` | Already took effect exactly once. Not an error; do not retry. |
| Same `txn_id`, different sender / receiver / amount | **409**, `TXN_ID_CONFLICT` | Permanent; the ID belongs to a different request. Stop and escalate. |
| `LEDGER_INVALID_ACCOUNT` | 404 | Permanent; stop |
| `LEDGER_INSUFFICIENT_BALANCE` | 409 | Permanent; stop |
| `LEDGER_INVALID_AMOUNT` | 400 | Permanent; stop. Also returned for out-of-range amounts and sub-cent precision |
| `LEDGER_CONCURRENT_CONFLICT` | 503, `retryable: true` | Transient; retry with the same ID |
| `LEDGER_DB_ERROR`, `LEDGER_UNKNOWN_ERROR` | 500 | Escalate |

A duplicate is a 200 on purpose: the correct outcome (one effect) already exists, and treating it as a failure would push an agent toward retrying. The duplicate response includes the recorded amount and current balances.

`GET /verify` returns **200 even when the ledger is inconsistent** (`status: "anomaly"`). The Reconciler needs to read the finding, not catch an exception.

### Separation of duties

Only the Executor can change state. The Planner and Reconciler only read. This limits the damage any single mistaken agent can do. It also means the Reconciler's findings are independent of both the decision and the action it is auditing.

---

## Failure recovery

How each failure mode is handled, and where the handling lives:

| Failure | Handling | Where |
|---|---|---|
| Invalid request (unknown account, amount ≤ 0, out-of-range or sub-cent amount, self-transfer, overdraft) | Rejected before anything is committed. The Planner checks each condition against live state; the ledger and API reject it regardless. | Planner mandate; input checks in `api/ledger_wrapper.py`; `validate_inputs` and the balance check in `ledger.c` |
| Same request sent twice | Stopped at up to three layers (see below). The ledger layer moves no money and reports a duplicate. | Planner mandate step 5; Executor mandate step 6; duplicate check under the lock in `ledger_transfer` |
| Same `txn_id`, different request | Rejected with 409 `TXN_ID_CONFLICT`; nothing moves | Duplicate check in `ledger_transfer` path |
| Executor's response lost, or unclear whether the work landed | The Executor looks up the txn_id before any resubmission. A record means the transfer happened; no record means it did not, and it is safe to retry with the same ID. | Executor mandate, step 6; transaction lookup tool |
| Transient failure | Retry with backoff, at most three attempts, always with the same ID | Executor mandate, step 5 |
| Permanent failure | Stop immediately, report, notify the Reconciler | Executor mandate, step 5 |
| Transaction table full | Rejected before the debit, so there is nothing to roll back | `ledger_transfer` |
| Inconsistent state detected | Reported with specifics and escalated to a human; never auto-repaired | Reconciler mandate, step 8 |

The lost-response recovery relies on one property: **a record exists if and only if the transfer took effect.** That property holds because the debit, credit and record write happen in one critical section, and only executed transfers are recorded.

### Observed in the first run

`docs/first-run-log.md` records what the agents actually did (Sep 29, before the hardening fixes below). Two of the rows above were exercised by agents, not only by `test-api.sh`:

- **The Executor looked up before resubmitting.** After `txn_100` had settled, a human sent the Executor a redundant instruction to proceed with the approved transfer. The Executor did not resubmit. It looked up `txn_100`, found it recorded as executed ($50.00 alice→bob, no error), and reported that resubmitting would only return a duplicate. It added that a further transfer would need a new approval and a new transaction id. The lost-response path in its mandate thus fired on a genuinely ambiguous instruction, not a scripted test.
- **The Planner rejected a replay.** In an earlier session with the same `txn_id` already recorded, the Planner returned NOT APPROVED with the existing record as evidence. No transfer call was made.

So a replay can be stopped at three independent layers: the Planner at validation, the Executor before resubmitting, and the C ledger under the mutex (`LEDGER_DUPLICATE_TXN`). Only the innermost one guarantees the money is safe. The outer two save a round trip.

The retry-on-transient-failure path and a true lost response (a timeout after the request was sent) have **not** been exercised by agents yet (see Known limitations, item 7).

---

## Verification

What we ran, on what, and what it showed. Environment for the local runs: Pop!_OS, Python 3.12, pinned dependencies from `requirements.txt`.

| Check | Result |
|---|---|
| `tests/test_ledger_core.py` | 21 tests pass |
| `tests/test_api.py` | 55 tests pass |
| Same API tests against the original, pre-fix code | **33 of 55 fail**, so the suite does detect the bugs listed below |
| ThreadSanitizer stress, `tests/ledger_stress.c` | 18,286 transfers across 8 threads: no data races reported, conservation held. Run under `setarch "$(uname -m)" -R` |
| Docker `stage-1/`, `bash stage-1/test-api.sh` | Passes |
| Docker `stage-1/`, amount `0.285` | 400 `LEDGER_INVALID_AMOUNT` |
| Docker `stage-1/`, `txn_001` reused with a different amount | 409 `TXN_ID_CONFLICT` |

What these runs do **not** show: HTTP-level concurrency (requests are serialized by the event loop, limitation 5), behaviour at the account or transaction caps (limitation 2), or any agent-level run after the fixes.

### Findings fixed

Found during verification and fixed. Each describes behaviour before and after.

1. **Amount wrap.** An amount of $184,467,440,737,095,552 reached the C layer as 4096 cents, moved $40.96, and returned success. Out-of-range amounts are now rejected with 400.
2. **Wrong float rounding.** `0.285` became 28 cents. Amounts now convert to exact cents or are rejected.
3. **`verify` could not fail.** `ledger_verify_state` added each executed amount to both `total_debits` and `total_credits`, so `is_balanced` was true by construction and could not catch money being created or destroyed. It now compares the sum of balances to the total seeded (conservation) and detects negative balances.
4. **Long account ids.** Ids of 256 bytes or more were accepted and truncated. They are now rejected.
5. **No cap on initial balance.** An unbounded initial balance risked overflow. It is now capped at 10^14 cents.
6. **Table-full check after the debit.** A full transaction table was detected only after the debit, which needed a rollback. The check now runs before the debit.
7. **Reused `txn_id` with different parameters returned `duplicate`.** It now returns 409 `TXN_ID_CONFLICT`.

---

## Measured costs

> **PENDING. One end-to-end agent run has happened (`docs/first-run-log.md`), but none of the numbers below have been recorded yet.** Every row is a TODO. We will fill them only with measured values, and state how each was measured. Test and stress-run results are under "Verification", not here.

**Agent layer** (per settled transfer, measured in the Band room):

| Metric | Planner | Executor | Reconciler | Total |
|---|---|---|---|---|
| Model tokens (input / output) | TODO | TODO | TODO | TODO |
| Model cost (USD) | TODO | TODO | TODO | TODO |
| Wall-clock time | TODO | TODO | TODO | TODO |
| HTTP calls to the service | TODO | TODO | TODO | TODO |
| Retries | — | TODO | — | TODO |
| Human interventions (target: 0) | TODO | TODO | TODO | TODO |

**Scenario coverage** (a count for each scenario type: happy path, replay, overdraft, unknown account, lost response, concurrent drain):

- TODO: transfers attempted / settled / correctly rejected
- TODO: anomalies the Reconciler reported, and how many were real
- TODO: duplicate effects observed (target: 0)

**Service layer:**

- TODO: `POST /transfer` latency, p50 / p99, single client
- TODO: throughput and latency under N concurrent clients hitting the same account
- TODO: Docker image size and cold-build time for `stage-1/`

---

## Known limitations

These are known gaps in the current code, listed so nobody reads more into the system than it does.

1. **Rejected attempts leave no trace.** `ledger_transfer` records only executed transactions (`status = 1`). `ledger_transaction_t` defines `status = 2` (rejected) and an `error_reason` field, but nothing ever writes them. The Reconciler can audit successful transfers but cannot see failed attempts, for example how many times an overdraft was tried.

2. **Fixed-size storage.** Accounts and transactions live in arrays capped at `LEDGER_MAX_ACCOUNTS` (10,000) and `LEDGER_MAX_TRANSACTIONS` (100,000). A full transaction table is now detected before the debit, so there is nothing to roll back. We have no recorded verification of behaviour at either cap.

3. **Memory only.** All state lives in process memory, and nothing survives a restart. The three seed accounts (`alice`, `bob`, `carol` at $5,000 each) are created at startup only so the API can be tested on boot. The "durability" claim that `ledger.h` used to make has been removed because it was never implemented.

4. **The duplicate lookup is a linear scan under the global lock.** Filling the transaction table to its 100,000 cap took about 25 s. That figure was measured in a sandbox, not on the author's hardware, and it is the cost of the scan growing with each recorded transaction. It is acceptable for a demo-scale ledger and would not be for a production one.

5. **HTTP requests are serialized by the event loop.** The FastAPI handlers are `async def` and call the ledger synchronously, so one request runs at a time. The C mutex is exercised by the ThreadSanitizer stress test, not by HTTP traffic. **Never run `uvicorn --workers` greater than 1**: each worker process would hold its own separate in-memory ledger.

6. **`verify` does not check duplicate transaction IDs or orphaned transactions.** `ledger.h` describes those checks, but `ledger_verify_state` implements the negative-balance check and the conservation comparison (sum of balances against total seeded) only. The Reconciler mandate asks the agent to look for duplicates, and the agent has to do that from the transaction records itself.

7. **The retry path is never exercised.** Because every call is serialized under one lock, `ledger.c` never returns `LEDGER_CONCURRENT_CONFLICT`. The 503 / `retryable` response and the Executor's transient-retry branch are therefore never triggered by the ledger itself.

8. **`GET /ledger` lists only accounts created through this service process.** The C API has no call that lists accounts, so the service keeps its own list (`_known_accounts`).

Closed since the previous revision of this document: no automated tests (now 76), the conservation check that could not fail (finding 3), and silent sub-cent rounding (findings 1 and 2).

---

## Reproducing

```bash
make build                               # compile the ledger, install pinned dependencies
python -m pytest tests/ -q               # 76 tests: 21 C core, 55 API

make docker-build && make docker-run     # builds and runs stage-1/ on :8000
bash stage-1/test-api.sh                 # health, transfer, replay, overdraft, unknown account, verify
```

The ThreadSanitizer stress test is `tests/ledger_stress.c`. Build it with `-fsanitize=thread` and run it under `setarch "$(uname -m)" -R`, which disables address-space randomization so ThreadSanitizer can start on some kernels.

Expected results (Docker `stage-1/`):

| Behaviour | Result |
|---|---|
| Transfer between accounts | Debit, credit and record, atomic |
| Same `txn_id` sent twice | Second call moves no money; returns `duplicate` |
| Same `txn_id`, different amount | `409`, `TXN_ID_CONFLICT`, ledger untouched |
| Transfer exceeding balance | `409`, ledger untouched |
| Amount `0.285` | `400`, `LEDGER_INVALID_AMOUNT` |
| Unknown account | `404` |
| Ledger audit | Balanced against total seeded, no anomalies, correct transaction count |
