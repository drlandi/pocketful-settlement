# settlement-agents — Status Brief
**Dark Factory hackathon · pocketful track · updated Oct 3, 2026 (first written Sep 28)**
**Repo:** https://github.com/drlandi/pocketful-settlement (public, MIT)
**Submission:** Oct 6, 3:59 AM BST · lablab.ai

---

## What we're building, in one paragraph

A payment settlement system where three AI agents coordinate a money transfer
without a human in the loop. One agent validates the request, a second executes
it against a ledger, a third audits the ledger afterwards to confirm nothing was
lost, duplicated, or left half-finished. The agents are Band Remote Agents,
registered on app.band.ai. Each runs as its own Python process (`agents/`),
joins a shared Band room, and talks to our service over HTTP. The service itself
is the part that has to be *correct*: a C ledger core (thread-safe, atomic,
integer-cents arithmetic) wrapped in a Python FastAPI layer.

The track is graded on transaction safety: atomicity, idempotency, concurrency,
and recovery from bad states. Not on features.

---

## Architecture

```
        Human / external request
                  ↓
        Band room (app.band.ai)
        ├── Planner agent      validates, decides go/no-go
        ├── Executor agent     performs the transfer, retries on transient failure
        └── Reconciler agent   audits ledger consistency, reports anomalies
                  ↓ HTTP
        FastAPI service (Python)
                  ↓ ctypes
        Ledger core (C, compiled to .so)
```

Why C for the ledger: money is stored as `int64` cents so there is no
floating-point rounding, and the transfer path runs under a `pthread` mutex so
concurrent transfers serialize rather than interleave. The header specifies
atomicity as an explicit contract: a transfer either records both the debit and
the credit, or neither.

---

## What is built and verified

### The agents

All three Remote Agents exist and run from `agents/` (`uv run planner`,
`executor`, `reconciler`). Tool access enforces the roles: only the Executor has
a transfer tool; the Planner and Reconciler are read-only. The first end-to-end
run (Sep 29) was recorded and is written up in `docs/first-run-log.md`: a single
human message produced an approved, executed and audited $50 transfer in about 30
seconds, and the Executor correctly declined to resubmit when a human sent a
redundant instruction. That run happened **before** the hardening fixes below, so
it hasn't been repeated against the current service.

### The service

| Behaviour | Result |
|---|---|
| Transfer between accounts | Debit + credit + transaction record, atomic |
| Same request id sent twice | Second call moves no money, returns `duplicate` |
| Same request id, different sender / receiver / amount | Rejected, `409 TXN_ID_CONFLICT` |
| Transfer exceeding balance | Rejected, `409`, ledger untouched |
| Unknown account | Rejected, `404` |
| Amount with sub-cent precision (`0.285`) | Rejected, `400` (previously rounded to 28 cents) |
| Ledger audit | Total of balances checked against total seeded, no anomalies, transaction count correct |

Verification we actually ran (Pop!_OS, Python 3.12, pinned dependencies):

- **76 automated tests pass**: 21 on the C core, 55 on the API. Run against the
  original code, 33 of the 55 API tests fail, so the suite catches the bugs below.
- **ThreadSanitizer stress test**: 18,286 transfers across 8 threads, no data
  races reported, conservation held.
- **Docker `stage-1`**: `test-api.sh` passes; `0.285` returns 400; reusing
  `txn_001` with a different amount returns 409 `TXN_ID_CONFLICT`.

The full list is in `FACTORY.md` under "Verification". `stage-1/test-api.sh`
still exercises the endpoints against a running container.

**Important:** accounts (`alice`, `bob`, `carol` at $5000) are seeded in memory at
startup purely so the API is testable on boot. Nothing persists; nothing is real.

---

## Findings, documented honestly

Finding bugs in our own code and fixing them where they belong is a stronger demo
story than pretending there were none. Each of these is worth showing.

1. **Duplicate signalled in Python, not C (fixed Sep 28, commit `87d0a83`).** The
   ledger refused a repeated request id but reported plain success; a pre-check in
   Python, outside the mutex, compensated. The existing lookup in C, which runs
   under the lock, now returns a proper duplicate status and the pre-check is gone.
2. **Amount wrap.** $184,467,440,737,095,552 reached C as 4096 cents, moved $40.96
   and returned success. Now rejected (400).
3. **Floats rounded wrongly.** `0.285` became 28 cents. Now exact cents or rejected.
4. **The balance check could not fail.** `ledger_verify_state`'s `is_balanced` was
   true by construction. It now compares the sum of balances to the total seeded
   (conservation) and detects negative balances.
5. **Account ids of 256 bytes or more** were accepted and truncated. Now rejected.
6. **No cap on initial balance** (overflow risk). Now 10^14 cents maximum.
7. **Table-full check ran after the debit** and needed a rollback. It now runs
   before the debit.
8. **Same request id with different parameters** came back as `duplicate`. It now
   returns 409 `TXN_ID_CONFLICT`.

---

## What is still open

Documented in `FACTORY.md` under "Known limitations" so nobody reads more into the
system than it does:

- The duplicate lookup is a linear scan under the global lock (about 25 s to fill
  100,000 transactions, measured in a sandbox, not on our hardware).
- HTTP handlers are `async def`, so requests are serialized by the event loop. The
  C mutex is exercised by the thread tests, not by HTTP traffic. We never run more
  than one uvicorn worker.
- `verify` does not check duplicate ids or orphaned transactions.
- State is in memory only.
- The retry path (the Executor's transient-failure branch) has never been
  triggered, because the ledger never returns a concurrent-conflict status.
- Rejected attempts leave no record, so the Reconciler can't see failed attempts.

---

## Remaining work

1. **Measured costs** in `FACTORY.md`: tokens, cost, wall-clock time and HTTP calls
   per settled transfer. These stay TODO until we pull real usage numbers. No
   estimates.
2. **`band-room-export.json`**: export the room for submission.
3. **Demo video**, including a recording of the room. **A missing recording is a
   stated disqualifier**, regardless of code quality.
4. **Submit** on lablab.ai by Oct 6, 3:59 AM BST.

---

## Where you come in

### 1. Room export and a fresh recorded run: **yours**
The room and three seats exist. What's needed now is a clean run against the
current service (the Sep 29 run predates the fixes), the room exported as
`band-room-export.json`, and the recording confirmed.

### 2. Mandates: **drafted, still needs your review**
Three files in `mandates/`: planner, executor, reconciler.

⚠️ **Hard constraint:** mandates must stay *generic*. If they name our specific
field names, endpoint paths, error codes or account ids, that's a disqualifier.
They describe roles and decision logic only. That includes `TXN_ID_CONFLICT`, which
must not appear in a mandate. Please read them with that lens specifically; a
second pair of eyes on this is worth more than anything else right now.

### 3. Agent-level scenarios and edge cases: **yours**
The service-level nasties are covered by the test suites: overflowing amounts,
sub-cent precision, id reuse with different parameters, and concurrent transfers
on the C core under ThreadSanitizer. What isn't covered is how the *agents*
behave on them. Your fintech domain knowledge is the real value here: what should
the Planner and Executor do on a `409` conflict versus a `409` overdraft, on a
transfer to self, on a retry storm, on a lost response? Plain English scenarios
are fine and I'll implement.

---

## Stage plan

| Stage | Content | Status |
|---|---|---|
| 1 | Basic transfers, atomicity, required for eligibility | ✅ done, verified in Docker |
| 2 | Concurrency, retries, idempotency in C | Idempotency ✅. Concurrency ✅ on the C core (ThreadSanitizer). Retry path not exercised. |
| 3 | Verification, anomaly detection, recovery | Conservation check ✅. Duplicate-id and orphan checks not implemented. |
| 4 | Edge cases, precision, final hardening | Precision and input limits ✅ |

Each stage ships as a self-contained, buildable folder. We submit only what's
actually complete. The fixes above were verified on the working copy, and for the
Docker rows on `stage-1/`.

---

## Division of work

**Landi:** backend (C ledger, API, concurrency), integration, submission, video,
FACTORY.md design doc.

**Mubashir:** Band room, mandate review, agent-level scenarios and edge cases,
domain validation.

Parallel tracks, no blocking. Ping me on anything rather than waiting for a sync.
