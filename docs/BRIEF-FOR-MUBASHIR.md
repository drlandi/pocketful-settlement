# settlement-agents — Status Brief
**Dark Factory hackathon · pocketful track · Sep 28, 2026**
**Repo:** https://github.com/dlandi/pocketful-settlement (public, MIT)
**Submission:** Oct 6, 3:59 AM BST · lablab.ai

---

## What we're building, in one paragraph

A payment settlement system where three AI agents coordinate a money transfer
without a human in the loop. One agent validates the request, a second executes
it against a ledger, a third audits the ledger afterwards to confirm nothing was
lost, duplicated, or left half-finished. The agents run as separate seats in a
BAND Desktop room and talk to our service over HTTP. The service itself is the
part that has to be *correct* — it's a C ledger core (thread-safe, atomic,
integer-cents arithmetic) wrapped in a Python FastAPI layer.

The track is graded on transaction safety: atomicity, idempotency, concurrency,
and recovery from bad states. Not on features.

---

## Architecture

```
        Human / external request
                  ↓
        BAND Desktop room
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
atomicity as an explicit contract — a transfer either records both the debit and
the credit, or neither.

---

## What is built and working (as of tonight)

The service runs in Docker and passes end-to-end tests:

| Behaviour | Result |
|---|---|
| Transfer between accounts | Debit + credit + transaction record, atomic |
| Same request id sent twice | Second call moves no money, returns `duplicate` |
| Transfer exceeding balance | Rejected, `409`, ledger untouched |
| Unknown account | Rejected, `404` |
| Ledger audit | `debits == credits`, no anomalies, transaction count correct |

Endpoints live: account lookup, transfer, execute, transaction lookup,
verification, ledger overview, health.

There's a test script in `stage-1/test-api.sh` that exercises all of it against
a running container.

**Important:** accounts (`alice`, `bob`, `carol` at $5000) are seeded in memory at
startup purely so the API is testable on boot. Nothing persists; nothing is real.

---

## What is NOT built — and this is where you come in

**No agents exist yet.** Everything above is the *tool* the agents will call. The
hackathon is judged on agent orchestration, and we currently have zero of it.
That's the critical path now, not the backend.

Three things outstanding:

### 1. BAND Desktop room + three agent seats — **yours**
Install BAND Desktop, create three seats, paste in the mandates (attached
separately), point them at the service. The room has to be exported as
`band-room-export.json` for submission, and a **recording of the room is a stated
disqualifier if missing** — no recording, no score, regardless of code quality.

### 2. Mandates — **drafted, needs your review**
Three files: planner, executor, reconciler. Attached.

⚠️ **Hard constraint:** mandates must stay *generic*. If they name our specific
field names, endpoint paths, or error codes, that's a disqualifier. I've written
them to describe roles and decision logic without naming a single
pocketful-specific identifier. Please read them with that lens specifically —
a second pair of eyes on this is worth more than anything else right now.

### 3. Test scenarios + edge cases — **yours**
Your fintech domain knowledge is the real value here. What we have covers the
obvious paths. What's missing is the nasty stuff: simultaneous transfers draining
the same account, retry storms, transfers to self, amounts at precision
boundaries, partial-failure recovery. If you can write these as scenarios (plain
English is fine, I'll implement), that shapes Stage 2 and 3.

---

## Known issue, documented honestly — now fixed

**Was:** the ledger correctly refused to process a repeated request id, but
reported it as a plain success rather than flagging it as a repeat. The Python
layer compensated with a pre-check that sat outside the mutex and cost a linear
scan per transfer.

**Fixed (Sep 28, commit `87d0a83`):** the existing lookup in the C, which already
runs under the lock, now returns a proper duplicate status, and the Python
pre-check is gone. Verified in the rebuilt Docker image: the replay comes back as
`duplicate`, alice stays at $4,950, and the transaction count stays at 1.

This is still worth *showing* in the demo video: finding it, reasoning about
where the fix belongs, and fixing it there is a stronger story than pretending it
never happened. The next finding of the same kind is the balance check that
can't fail (see Stage 3 priorities below).

---

## Stage plan

| Stage | Content | Target | Status |
|---|---|---|---|
| 1 | Basic transfers, atomicity, required for eligibility | Sep 28 | ✅ done |
| 2 | Concurrency, retries, idempotency in C | Sep 29–Oct 1 | in progress: idempotency in C ✅ done; concurrency + retries next |
| 3 | Verification, anomaly detection, recovery | Oct 2–4 | pending |
| 4 | Edge cases, precision, final hardening | Oct 5 | pending |

Each stage ships as a self-contained, buildable folder. We submit only what's
actually complete — a solid Stage 1 and 2 beats four half-finished ones.

### Stage 3 priorities

1. **Top priority: the ledger's balance check cannot fail.**
   `ledger_verify_state` adds every executed transaction's amount to *both*
   `total_debits` and `total_credits`, so `is_balanced` is true by
   construction. The conservation check it reports is vacuous: it would say
   "balanced" even if money had been created or destroyed. The real invariant
   is that the sum of all account balances equals the sum of initial
   balances, and nothing checks that today. Until it's fixed, a "verified"
   result from the service proves nothing about conservation, and the
   Reconciler has to derive the check itself. (Known limitation #5 in
   `FACTORY.md`.)

---

## Division of work

**Landi:** backend (C ledger, API, concurrency), integration, submission, video,
FACTORY.md design doc.

**Mubashir:** BAND Desktop room + seats, mandate review, test scenarios and edge
cases, domain validation.

Parallel tracks, no blocking. Ping me on anything rather than waiting for a sync.

---

## The one thing that matters most this week

Agents. The backend is ahead of schedule; the agent layer hasn't started. A
flawless ledger with no BAND room scores zero on this track. If you can get the
room standing with three seats talking to the service, even crudely, everything
else is refinement.
