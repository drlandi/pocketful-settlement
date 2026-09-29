# FACTORY.md: pocketful-settlement

**Dark Factory hackathon · pocketful track · submission due Oct 6 2026**

Three AI agents (Planner, Executor, Reconciler) settle payments with no human in the loop. Each is a Remote Agent registered on app.band.ai and running as its own Python process (`agents/`, Band SDK with the Anthropic adapter). They coordinate in a Band room and call an HTTP settlement service. The service is where correctness lives: a C ledger core (integer cents, one mutex, atomic transfers) wrapped in a Python FastAPI layer.

The track is graded on transaction safety: atomicity, idempotency, concurrency and recovery. This document explains how the design addresses each of those, what we have measured, and what we have not.

---

## Status at a glance

| Stage | Scope | Status |
|---|---|---|
| 1 | Basic transfers, atomicity (eligibility requirement) | **Done.** Verified end to end in Docker. |
| 2 | Concurrency, retries, idempotency moved into C | **In progress.** Idempotency is now signalled from C and verified in Docker; concurrency work remains. |
| 3 | Verification, anomaly detection, recovery | Pending |
| 4 | Edge cases, precision, final hardening | Pending |
| — | Band room, three Remote Agents, room export, recording | **In progress.** Agents run from `agents/`; first end-to-end run completed and recorded (`docs/first-run-log.md`). Room export not yet in the repo. |

Each stage ships as a self-contained, buildable folder (`stage-1/` … `stage-4/`). We submit only completed stages.

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

- **Money is `int64` cents** (`money_t` in `ledger.h`), so there is no floating-point arithmetic on balances. The Python layer converts dollars to cents once, at the boundary (`dollars_to_cents`, which rounds rather than truncates).
- **One `pthread_mutex_t` guards all ledger state.** Every public function, reads included, takes the lock, so concurrent transfers are serialized and never interleave. We chose serialization over finer-grained locking because correctness is graded and throughput is not.
- **Atomicity is an explicit contract** in `ledger.h`. `LEDGER_OK` means both the debit and the credit landed and a transaction record was written; any error means neither leg happened. In `ledger_transfer`, the validation, duplicate check, balance check, debit, credit and record write all happen inside one critical section. If the record cannot be written (ledger full), both legs are rolled back before the lock is released. No other thread can observe a half-applied transfer.

### Why a thin ctypes layer

`api/ledger_wrapper.py` mirrors the C structs as `ctypes.Structure`s and declares every exported signature. It adds no business logic of its own. Post-transfer balances are **read back from the ledger**, never computed in Python, so a response always shows what the ledger actually holds.

### Idempotency

Every transfer carries a caller-supplied `txn_id` (1–63 characters). The C ledger checks whether that ID is already recorded, **inside the same lock as the transfer itself**. So two concurrent attempts with the same ID cannot both move money: one executes and the other sees the record.

This is the protection the Executor's retry policy depends on. The Executor always resends the same ID, so a retry after a lost response either finds the original record or performs the transfer for the first time. It never does both.

**The Stage 2 change.** In Stage 1 the C ledger already refused to move money twice, but it reported the replay as `LEDGER_OK`, so a caller could not tell a retry from a first attempt. The Python layer compensated with a lookup before each transfer. That lookup ran outside the lock and cost a linear scan. The fix moves the signal into C: the existing lookup under the lock now returns `LEDGER_DUPLICATE_TXN`, and the Python pre-check is removed.
*Verified in the `stage-1/` Docker image with `stage-1/test-api.sh`: the first transfer returns `success`, and replaying the same ID returns HTTP 200 with `status: "duplicate"` / `LEDGER_DUPLICATE_TXN`. After the replay, `alice` is still at 4950.00 and `num_transactions` is still 1.*

### Status codes built for agents

`main.py` maps ledger statuses onto HTTP status codes so an agent can branch on the code without parsing prose:

| Ledger status | HTTP | Meaning for the Executor |
|---|---|---|
| `LEDGER_OK` | 200, `status: "success"` | Took effect |
| `LEDGER_DUPLICATE_TXN` | **200**, `status: "duplicate"` | Already took effect exactly once. Not an error; do not retry. |
| `LEDGER_INVALID_ACCOUNT` | 404 | Permanent; stop |
| `LEDGER_INSUFFICIENT_BALANCE` | 409 | Permanent; stop |
| `LEDGER_INVALID_AMOUNT` | 400 | Permanent; stop |
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
| Invalid request (unknown account, amount ≤ 0, self-transfer, overdraft) | Rejected before anything is committed. The Planner checks each condition against live state; the ledger rejects it regardless. | Planner mandate; `validate_inputs` and the balance check in `ledger.c` |
| Same request sent twice | Stopped at up to three layers (see below). The ledger layer moves no money and reports a duplicate. | Planner mandate step 5; Executor mandate step 6; duplicate check under the lock in `ledger_transfer` |
| Executor's response lost, or unclear whether the work landed | The Executor looks up the txn_id before any resubmission. A record means the transfer happened; no record means it did not, and it is safe to retry with the same ID. | Executor mandate, step 6; transaction lookup tool |
| Transient failure | Retry with backoff, at most three attempts, always with the same ID | Executor mandate, step 5 |
| Permanent failure | Stop immediately, report, notify the Reconciler | Executor mandate, step 5 |
| Ledger full while recording | Both legs rolled back before the lock is released; `LEDGER_DB_ERROR` returned | `ledger_transfer` |
| Inconsistent state detected | Reported with specifics and escalated to a human; never auto-repaired | Reconciler mandate, step 8 |

The lost-response recovery relies on one property: **a record exists if and only if the transfer took effect.** That property holds because the debit, credit and record write happen in one critical section, and only executed transfers are recorded.

### Observed in the first run

`docs/first-run-log.md` records what the agents actually did. Two of the rows above were exercised by agents, not only by `test-api.sh`:

- **The Executor looked up before resubmitting.** After `txn_100` had settled, a human sent the Executor a redundant instruction to proceed with the approved transfer. The Executor did not resubmit. It looked up `txn_100`, found it recorded as executed ($50.00 alice→bob, no error), and reported that resubmitting would only return a duplicate. It added that a further transfer would need a new approval and a new transaction id. The lost-response path in its mandate thus fired on a genuinely ambiguous instruction, not a scripted test.
- **The Planner rejected a replay.** In an earlier session with the same `txn_id` already recorded, the Planner returned NOT APPROVED with the existing record as evidence. No transfer call was made.

So a replay can be stopped at three independent layers: the Planner at validation, the Executor before resubmitting, and the C ledger under the mutex (`LEDGER_DUPLICATE_TXN`). Only the innermost one guarantees the money is safe. The outer two save a round trip.

The retry-on-transient-failure path and a true lost response (a timeout after the request was sent) have **not** been exercised by agents yet (see Known limitations, item 7).

---

## Measured costs

> **PENDING. One end-to-end agent run has happened (`docs/first-run-log.md`), but none of the numbers below have been recorded yet.** Every row is a TODO. We will fill them only with measured values, and state how each was measured.

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

1. **Rejected attempts leave no trace.** `ledger_transfer` records only executed transactions (`status = 1`). `ledger_transaction_t` defines `status = 2` (rejected) and an `error_reason` field, but nothing ever writes them. The Reconciler can audit successful transfers but cannot see failed attempts, for example how many times an overdraft was tried. Closing this gap is Stage 3 recovery work.

2. **Fixed-size storage.** Accounts and transactions live in arrays capped at `LEDGER_MAX_ACCOUNTS` (10,000) and `LEDGER_MAX_TRANSACTIONS` (100,000). The code has a rollback path for a full transaction table, but **behaviour at either cap is untested**.

3. **Memory only.** All state lives in process memory, and nothing survives a restart. The three seed accounts (`alice`, `bob`, `carol` at $5,000 each) are created at startup only so the API can be tested on boot. The header's "durability" guarantee is not implemented.

4. **No automated tests.** `tests/` is empty. Current verification is the manual script `stage-1/test-api.sh`, run against a live container.

5. **The debit/credit balance check cannot fail.** `ledger_verify_state` adds each executed transaction's amount to *both* `total_debits` and `total_credits`, so `is_balanced` is true by construction and cannot catch a mismatch. The real conservation check is that the sum of all balances (`total_value` from `ledger_get_stats`) equals the sum of initial balances. The service does not compute that comparison yet; the Reconciler has to derive it.

6. **Checks the header promises but the code doesn't do.** `ledger.h` says verification checks for duplicate transaction IDs and orphaned transactions. `ledger_verify_state` implements only the negative-balance check (plus the balance check from item 5).

7. **The retry path is never exercised.** Because every call is serialized under one lock, `ledger.c` never returns `LEDGER_CONCURRENT_CONFLICT`. The 503 / `retryable` response and the Executor's transient-retry branch are therefore never triggered by the ledger itself.

8. **Sub-cent amounts are silently rounded.** `amount_dollars` is a float, and `dollars_to_cents` rounds to the nearest cent using Python's round-half-to-even. For example, `0.125` becomes 12 cents. Sub-cent input is rounded rather than rejected, and amounts below half a cent round to zero and are rejected as invalid. Precision hardening is Stage 4.

9. **`GET /ledger` lists only accounts created through this service process.** The C API has no call that lists accounts, so the service keeps its own list (`_known_accounts`).

---

## Reproducing

```bash
make docker-build && make docker-run     # builds and runs stage-1/ on :8000
bash stage-1/test-api.sh                 # health, transfer, replay, overdraft, unknown account, verify
```

Expected results (verified in Docker after the Stage 2 idempotency change):

| Behaviour | Result |
|---|---|
| Transfer between accounts | Debit, credit and record, atomic |
| Same `txn_id` sent twice | Second call moves no money; returns `duplicate` |
| Transfer exceeding balance | `409`, ledger untouched |
| Unknown account | `404` |
| Ledger audit | Balanced, no anomalies, correct transaction count |
