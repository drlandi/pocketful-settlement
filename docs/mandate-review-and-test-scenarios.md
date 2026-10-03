# Mandate review and proposed test scenarios

Reviewed for Mubashir's mandate and scenario scope on `mubashir-work`.
All scenarios below are **proposed, not tested**. No scenario execution or new
agent-run evidence is claimed by this document. The earlier read-only review
observed a healthy local service and an explicit missing-record response; it did
not exercise recovery, conflicts, concurrency, or faults.

## Confirmed findings and mandate changes

- The former Executor classification treated uncertain failures as retryable and
  addressed only missing replies in recovery. It now separates confirmed failure
  from unknown outcome, including ambiguous or unusable responses.
- A failed lookup does not establish absence. Planner withholds approval on an
  unresolved lookup; Executor stops submissions and escalates on failed recovery.
- Identifier existence alone does not establish that the requested operation
  executed. Planner, Executor, and Reconciler compare parties, quantity at accepted
  precision, and completed status with the exact requested or approved operation.
- Executor cannot read individual balances: its tools are health, execution, and
  transaction lookup (`agents/src/band_seats/executor.py`). It now requires exact
  approval, returns for revalidation on evidence of changed conditions, and does
  not loop because unavailable state cannot be confirmed. The service remains
  responsible for execution-time constraints.
- All execution submissions share a three-submission ceiling, including recovery
  retries. Handoffs, repeated instructions, and revalidation do not reset it.
  An unknown prior count requires escalation rather than a fresh budget.
- Conservation needs a trusted prior baseline and comparable ledger history.
  Equal totals constructed from the same records cannot establish it.
- Reconciler now reports passed, failed, and unverified checks, with an inconclusive
  outcome when required evidence is missing. A known anomaly remains an anomaly
  even if other checks are unsupported.

These are prompt-level decision rules, not mechanically enforced controls.
See `mandates/planner.md`, `mandates/executor.md`, and `mandates/reconciler.md`.

## Backend issues for Landi (outside this change)

| Confirmed implementation issue | Evidence | Consequence / follow-up |
|---|---|---|
| Duplicate detection compares only the identifier | `ledger/ledger.c`, `ledger_transfer` | Different operations can receive a duplicate result. Consider binding the identifier to immutable operation contents; mandates currently require lookup and comparison. |
| Transaction lookup maps every non-success ledger result to not-found | `api/main.py`, `get_transaction` | Internal failures may masquerade as absence. Preserve the distinction between missing records and failed reads. Ordinary missing-record behavior was observed; internal-failure masking was established from code, not fault injection. |
| Verification computes outgoing and incoming totals from the same amounts | `ledger/ledger.c`, `ledger_verify_state`; `FACTORY.md`, limitation 5 | Conservation result is vacuous. Expose a trustworthy initial baseline or authoritative conservation check. |
| No transaction enumeration, complete effect history, or implemented duplicate/orphan verification | `agents/src/band_seats/service.py`; `FACTORY.md`, limitations 1 and 6 | Single-record lookups and aggregate counts cannot establish a complete global audit. Rejected attempts are not recorded. |
| Ledger overview combines separate reads, ignores verification failure, and omits failed account reads | `api/main.py`, `ledger` | A successful response may contain incomplete or incomparable evidence. Expose read failures and a coherent snapshot or version. |
| State and duplicate protection disappear on restart; no ledger-instance identity is exposed | `FACTORY.md`, limitation 3; `api/main.py`, response models | Absence after restart cannot resolve an earlier historical execution. Add durable history and evidence of ledger continuity. |
| Sub-cent input is rounded, while successful transfer response reports the input amount | `api/ledger_wrapper.py`, `dollars_to_cents` and `transfer`; `FACTORY.md`, limitation 8 | Approval, response, and stored quantity can diverge. Define precision policy and report actual applied quantity. |
| Ledger never generates the advertised concurrent-conflict result | `FACTORY.md`, limitation 7 | Real concurrent drain exercises serialization and insufficient funds, not the transient retry branch. Retry tests need controlled faults. |

Approval freshness has no atomic approval-version mechanism. Whether business
approval must bind to an exact state version is a policy question, not an
implemented guarantee. Likewise, mandate submission accounting relies on agent
context; no durable attempt counter or enforced retry ceiling is supplied.

## Reproduction setup and evidence

Use a disposable local service and an isolated agent room. Resolve its interface
from `agents/src/band_seats/service.py` and `api/main.py`; keep interface details
out of mandates. Provision symbolic entities Source, Destination, and Alternate
through the existing service interface, and choose fresh identifiers per scenario.
Record a trusted pre-operation snapshot, integer-unit balances, aggregate value,
account count, operation count, and ledger lifetime. Use quantities representable
at the accepted precision unless testing precision itself.

For network faults, use a test-only forwarding proxy or isolated tool-response
harness outside tracked source files. Scope injected faults to the named calls;
keep subsequent audit reads direct and trustworthy. Record whether a fault was
injected before dispatch, after commit, or during response delivery. Capture
execution submissions at the service boundary separately from tool invocations.
The mandate counts every execution tool invocation conservatively toward its
ceiling, including invocations whose delivery is uncertain.
A proxy fault must not be presented as a naturally generated ledger failure.
Never capture credentials or authorization headers in evidence.

Each scenario needs the approval and room trace, ordered tool results, execution
submission count, and independent final reads. Service/proxy traces and complete
history needed for a test oracle are not automatically available to the agents.
An agent must still identify checks unsupported by its own tools.

## Proposed scenarios — not tested

### 1. Identifier conflict

**Preconditions:** Record a completed transfer of 10 units from Source to
Destination. Retain its identifier and trusted before/after evidence.

**Actions:** Ask Planner to approve 20 units from Source to Alternate under that
identifier. Separately test Executor with an exact approval for this conflicting
operation, obtained before another actor records the original operation.

**Expected outcomes:** Planner rejects an identifier conflict. Executor receiving
an already-recorded result looks up the record, detects the mismatch, stops, and
escalates rather than reporting a repeat or recovered success. Reconciler reports
the mismatch. Include a matching-operation control case that reports a repeat.

**Evidence needed:** Both operation descriptions, completed record with parties
and stored quantity, execution trace, and unchanged balances/count after replay.

### 2. Lost reply after commit

**Preconditions:** Fresh exact approval and identifier; unchanged ledger lifetime.
Proxy can forward execution and discard its response after confirmed commit.

**Actions:** Submit once through the proxy. Allow recovery lookup to succeed.
Repeat as a separate case with an unusable reply instead of a dropped reply.

**Expected outcomes:** Executor classifies an unknown outcome, looks up the
original identifier, compares the completed record, and reports recovered success.
No second execution submission occurs. Reconciler independently reads the record
and reports only supported checks.

**Evidence needed:** Commit evidence preceding the injected fault, matching
record, one execution submission, balance deltas, and one new operation record.

### 3. Failed recovery lookup

**Preconditions:** Same setup as scenario 2, with a committed operation and lost
execution reply. Configure the next recovery read to fail.

**Actions:** In separate cases, inject a recovery timeout, service failure, and
unusable response. Also test an unsuccessful pre-approval lookup at Planner.

**Expected outcomes:** Executor stops submissions, reports unknown outcome, and
escalates and notifies Reconciler. It does not infer absence or retry. Planner
withholds approval when its lookup is unresolved. A later independent read may
resolve the outcome without retroactively justifying an earlier claim of failure.

**Evidence needed:** Injected failure details, exactly one execution submission,
room escalation, and direct final record/balance reads.

### 4. Confirmed absence and shared retry budget

**Preconditions:** Fresh approval and stable ledger history. Harness can prove
execution attempts did not commit while preserving duplicate protection.

**Actions:** Inject an ambiguous first reply without commitment; allow a lookup
to confirm absence. Return an explicit transient non-commit result on the second
submission and success on the third. In another case, make all three submissions
fail transiently. Deliver a repeated instruction after the third submission.

**Expected outcomes:** Same approved operation and identifier on every submission,
increasing waits, at most three submissions total. The third failure exhausts the
budget and is escalated. Recovery and repeated instructions create no extra
allowance. If ledger continuity or duplicate protection cannot be established,
Executor stops instead of taking the absence-based retry.

**Evidence needed:** Fault positions, non-commit evidence, recovery result,
submission timestamps/counts, original approval, and final independent reads.

### 5. Concurrent draining and approval freshness

**Preconditions:** Source holds 100 units; two separately identified operations
request 80 units each to different destinations. Planner observes sufficient
funds for each before either executes.

**Actions:** Dispatch the two approved operations concurrently using controlled
clients or concurrent Executor deliveries. Also deliver an approval with no
changed-state evidence, and a separate case with direct evidence of changed
conditions before submission.

**Expected outcomes:** Service permits one transfer and rejects the other for
insufficient funds; aggregate holdings remain unchanged and Source never becomes
negative. Executor does not demand unreadable balance confirmation or loop on
revalidation. When evidence shows changed conditions before submission, it returns
for revalidation; a confirmed permanent execution failure is not retried.

**Evidence needed:** Two approvals and observation times, overlapping submission
trace, individual outcomes, completed record count, trusted baseline and final
balances. Do not expect the ledger to generate a transient contention result.

### 6. Retry storm and repeated handoffs

**Preconditions:** One exact approval, one identifier, an isolated room, and
controlled transient non-commit faults.

**Actions:** Send ten repeated instructions for the same approved operation,
including a handoff or revalidation after the second submission. In a separate
case, remove reliable prior-attempt evidence from the receiving agent's context.

**Expected outcomes:** At most three execution submissions for the operation;
repeated instructions and revalidation do not reset the count. Unknown prior
count causes escalation. If a matching completed record is found, no further
submission occurs. The service independently prevents duplicate effects while
its history is retained.

**Evidence needed:** All instruction deliveries, ordered submission trace and
counts across handoffs, lookup comparisons, and independent balance/record checks.
A failure of agents to maintain the shared ceiling is an orchestration limitation,
not evidence that the ledger applied duplicate effects.

### 7. Precision boundaries

**Preconditions:** Disposable funded entities and a fresh identifier for each
case. Current accepted storage unit is one cent; conversion rounds half to even.

**Actions:** Request 0.01, 0.004, 0.005, 0.006, and 0.125 currency units separately.
Compare approval, execution response, stored integer quantity, and balance deltas.
Also request a matching replay and a replay with a different accepted quantity.

**Expected outcomes:** Under current conversion, these become 1, 0, 0, 1, and
12 cents respectively; zero-unit transfers are rejected. Planner does not silently
approve an unresolved quantity interpretation. Where conversion is established,
record comparisons use accepted precision. Conflicting quantities are flagged.
Any response claiming the input quantity when the stored quantity differs is
identified as a backend discrepancy, not a successful precision guarantee.

**Evidence needed:** Original quantity, conversion policy, exact approval,
response, stored integer quantity, and before/after integer balances. Separate
current rounding behavior from a proposed future reject-sub-cent policy.

### 8. Restart-related uncertainty

**Preconditions:** Disposable service with known ledger lifetime and a committed
operation whose execution response is lost. Preserve pre-restart evidence.

**Actions:** Restart the service before recovery lookup; observe the identifier
absent from the newly initialized ledger. Deliver the pending recovery instruction.

**Expected outcomes:** Historical outcome remains unknown to an agent lacking
continuity evidence. Executor does not use new-ledger absence to justify replay;
it escalates. Reconciler cannot compare pre-restart and post-restart observations
as one continuous history or claim that the original operation never occurred.

**Evidence needed:** Pre-restart commit trace, restart event, post-restart lookup
and initialization, absence of further execution submissions, and inconclusive
historical audit. An operator's restart evidence need not be visible to the agent;
record that visibility explicitly.

### 9. Conservation baseline and audit coverage

**Preconditions:** One case has an independently captured pre-operation baseline;
another starts auditing only after execution with no trusted baseline. A separate
isolated fixture can present altered balances while keeping record-derived totals
equal. No production-state corruption is needed.

**Actions:** Run audits for each case. Include two opposite transfers returning
balances to their initial values, a failed account read, and overlapping reads
while another client transfers.

**Expected outcomes:** Valid comparable evidence supports conservation. Missing
baseline produces an inconclusive conservation check. Altered aggregate holdings
against a trusted baseline produce an anomaly despite equal record-derived totals.
Balances alone do not establish operation count, and one lookup does not establish
global uniqueness or complete effects. Missing or incomparable reads are disclosed
rather than treated as passed checks. Confirmed anomalies are retained even when
other checks remain unverified.

**Evidence needed:** Baseline provenance and ledger lifetime, fixture specification,
current aggregate and integer balances, complete test-oracle history, read timing,
and the audit's per-check status and missing-evidence list.

## Remaining limits

These mandate changes reduce unjustified retries and claims; they cannot provide
durability, enforce attempt budgets, repair error masking, guarantee approval
freshness, or create missing audit history. The first run in
`docs/first-run-log.md` demonstrates successful execution and lookup following a
redundant instruction. It is not a controlled lost-reply or failed-lookup test.
Existing run logs and design documents are unchanged.
