# First end-to-end agent run

**Session:** Sep 29 2026, 13:28–13:32 UTC
**Room:** Band session, three remote agent seats
**Service:** `pocketful:stage1` container, in-memory ledger seeded with
alice/bob/carol at $5000.00 each
**Recorded:** yes (screen capture of the Band room)

---

## What was asked

A single sentence typed into the room by a human:

> @planner Transfer $50 from alice to bob. Use transaction id txn_100

No further human input until after the work was complete.

---

## What happened

**13:29:01 — Planner approved**, with the evidence its mandate requires, each
item actually checked against the service rather than assumed:

- service healthy, 3 accounts, 0 transactions
- `alice` exists, balance $5000.00 (500000 cents), covers $50.00
- `bob` exists, balance $5000.00 (500000 cents)
- amount positive, source and destination differ
- `txn_100` lookup returned 404 — no existing record, so a first attempt and
  not a repeat

It then handed off with explicit instructions, including a caution it was not
told to give:

> If the response is ambiguous or times out, look up txn_100 before any retry.
> Don't resubmit if a record exists.

**13:29:05 — Executor carried out the transfer.** One call, `txn_id` unchanged.

**13:29:2x — Reconciler audited** and found the ledger consistent: `txn_100`
recorded exactly once, alice $4950.00, bob $5050.00, total unchanged at
$15,000.00.

**13:29:26 — Planner confirmed** the audit matched what it approved and closed
the request.

Elapsed, human message to completion: roughly 30 seconds, no human in the loop.

---

## The recovery path, tested by accident

At 13:31:54 the human sent a redundant instruction, believing the transfer had
not run:

> @executor Proceed with the approved transfer.

The Executor did not resubmit. It looked the transaction up first:

> The approved transfer has already been carried out, so I did not submit it
> again. I looked up `txn_100` just now. It is recorded as executed: $50.00 from
> alice to bob, timestamp 1790688545, with no error. Resubmitting would only
> return "duplicate" and change nothing.

This is the lost-response case from the Executor mandate — *do not assume
nothing happened; query the record* — firing against a genuinely ambiguous
instruction rather than a scripted test. It then restated the process
constraint: a further transfer needs a new approval and a new transaction id.

---

## Duplicate protection, three independent layers

The run exercised all three, at different levels of the stack:

1. **Planner, at validation.** In an earlier session with the same `txn_id`
   already recorded, it returned NOT APPROVED with the existing record as
   evidence, and no service call was made at all.
2. **Executor, before resubmitting.** Demonstrated above: looks up the record
   rather than trusting its own assumption about whether the work landed.
3. **C ledger, under the mutex.** `ledger_transfer()` returns
   `LEDGER_DUPLICATE_TXN` for an already-recorded id, and moves no money.

Each layer is sufficient on its own. The outer two save a round trip; the
innermost is the one that actually guarantees the money is safe.

---

## Conservation was checked, and by the right mechanism

The Reconciler reported the total unchanged at $15,000.00 — that is the sum of
account balances, from the stats call, not the `is_balanced` flag from
`ledger_verify_state`. `is_balanced` is true by construction and cannot fail
(see Known limitations in FACTORY.md). The agent-level audit therefore performs
a real conservation check that the C-level verification currently does not.

---

## Role separation held

Tool access enforces the mandates rather than merely describing them:

| Seat | Tools | Can move money |
|---|---|---|
| planner | service health, account lookup, transaction lookup | no |
| executor | service health, execute transfer, transaction lookup | yes |
| reconciler | verify, ledger overview, account and transaction lookup | no |

The Planner has no transfer tool, so an approval cannot become an execution by
mistake. The Reconciler is read-only, which is what makes its audit worth
trusting.

In an earlier session the human addressed the request to `@executor` instead of
`@planner`. The Executor refused to act — *"reached me without a Planner
approval, and I only act on approved work"* — and tagged the Planner itself.
The process held under a misrouted instruction.
