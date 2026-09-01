# Architecture Decision Document

A short record of the decisions behind this prototype, what each one buys,
and what it costs. Written to be defensible in a live technical discussion,
not just a design doc that sounds good on paper.

## Why this stack?

**Python, stdlib `sqlite3`, no frameworks.** The assignment explicitly says
there's no correct stack, and to avoid technology added "just because it
sounds impressive." The interesting engineering problem in this assignment
— proving two workers can't double-book an agent, proving duplicate/out-of-
order provider events can't corrupt call state, proving a predictive
pacing engine can't bypass a safety boundary — is entirely about *state
transition correctness under concurrency*, not about which language or
database is fashionable. Python's `threading` module gives real OS-level
concurrency for I/O-bound work like this (the GIL is not a limiting factor
here — nothing is CPU-bound), and `sqlite3` is in the standard library,
so the whole prototype runs with zero external services and zero setup
friction, which matters for "should be easy for another engineer to run
locally."

## Why this architecture?

The five-stage pipeline (`Campaign → Pacing → Safety → Allocator →
Provider`) mirrors the assignment's own diagram directly, but the choice
that matters is **making the safety boundary a structural fact, not a
convention**: `pacing.py` doesn't import `providers.py` at all, and neither
pacing engine holds a reference to a `TelecomProvider` or a
`CallAllocator`. This is verified by a test that inspects the class's
`vars()` and source (`test_pacing_engines_have_no_access_to_a_provider_or_allocator`).
A code reviewer doesn't have to trust that nobody wired a shortcut in —
there is no import path through which one could exist.

The Safety Controller's independence is enforced the same way: it takes
a `Snapshot` built fresh from the actual stores at evaluation time, and a
test (`test_safety_controller_ignores_pacing_engines_own_explanation`)
proves it ignores whatever numbers the pacing engine's own `explanation`
dict claims. Trust nothing the upstream layer says about system state;
only trust what you queried yourself.

## Why this concurrency strategy?

Considered and rejected: a Python-level `threading.Lock` (or `asyncio`
lock) around agent reservation. It would pass every test in this
repository, because every "worker" in this prototype happens to run as a
thread inside one process. But the assignment is explicit that the real
system has "Worker 1, Worker 2, Worker 3 ... Worker N" as potentially
separate processes/machines — an in-process lock provides zero protection
there. So the guarantee had to live in the one thing every worker
necessarily shares: the database. A single atomic conditional `UPDATE ...
WHERE state='AVAILABLE'` is correct regardless of whether the caller is a
thread, a process, or a machine across the network, because the
database — not application code — is what serializes it. This is the same
mental model as `SELECT ... FOR UPDATE` or an optimistic version column in
Postgres; SQLite just makes it nearly free to demonstrate correctly
because of its whole-database writer serialization.

The same pattern is applied uniformly: agent reservation, borrower
claiming, call-state transitions, and the predictive "instant bind" of an
agent to an answered call are all single atomic `UPDATE ... WHERE`
statements. One mechanism, four use sites, instead of four different
concurrency tricks that would each need separate reasoning about
correctness.

## Why this pacing algorithm?

Rejected: any form of ML. The brief explicitly says not to build one, and
for a system whose entire point is a compliance-relevant safety property,
an opaque model would be actively worse — "why did the system decide to
initiate X calls?" needs a walkable answer, not a weight matrix.

The formula (`pacing.py`) is deliberately built from quantities a
collections-ops person would already recognize: how many agents are free,
how many calls in flight are likely to need one soon (based on a recent,
adaptive answer rate — not a fixed historical number, so a real shift is
reflected within a handful of calls via EWMA), how many dials it takes to
net one connect at that answer rate, and two multiplicative dampeners
(provider health, a safety margin that widens automatically with
abandonment). Every intermediate value is written into
`PacingDecisionRequest.explanation`, specifically so a pacing decision can
be reconstructed step by step after the fact rather than re-derived by
guessing.

The formula also **does not need to be trusted** — it's a request, not an
authorization. This is what let the pacing math stay simple: it doesn't
need its own hard safety clamps, because the Safety Controller is the
actual enforcement point. Keeping "how aggressive should we be" and "is
that actually safe" as two separately-reasoned questions, computed by two
different components from two different (but both freshly-queried)
snapshots, is the core design decision this whole assignment is testing
for.

## Why this persistence approach?

SQLite for the prototype, explicitly not "the answer" for production. It
was chosen because the write-serialization behavior that makes atomic
conditional updates trivially correct in a single-file database is the
*same conceptual guarantee* Postgres gives via row-level MVCC locking —
so the prototype's correctness pattern transfers directly, while its
performance ceiling (documented honestly via `load_test.py`, not assumed)
does not. See the [Scaling section](#how-would-it-evolve-to-production-scale)
below for exactly where that ceiling is and what replaces it.

One concrete, real bug this surfaced during development: the first version
of the candidate-selection query (`SELECT agent_id FROM agents WHERE
state=? ORDER BY updated_at LIMIT 1`) had no composite index, so SQLite
built a temporary B-tree to sort every matching row on every single
reservation attempt. At 10,000 agents this alone caused a **6x throughput
regression** (measured: ~2,700 → ~460 attempts/sec) — worse than anything
caused by actual write contention. `EXPLAIN QUERY PLAN` confirmed the
`TEMP B-TREE` step; adding a composite index on `(state, updated_at)`
(matching the query's exact filter+order shape) fixed it, and throughput
went flat across 100/1,000/10,000 agents afterward (~2,500-3,300
attempts/sec). This is worth stating plainly in a technical discussion:
**the first "distributed systems bottleneck" symptom I found during load
testing was actually a missing index, not a concurrency problem** — and
that's a more honest and more useful lesson than pretending the
architecture was bottleneck-free from the first draft.

## What does the design make easier?

- **Reasoning about correctness.** Every contended write is a single
  statement with an explicit `WHERE` clause naming the exact precondition.
  There's no multi-step "read, decide, write" sequence anywhere in the hot
  paths that could be interrupted mid-way by another thread.
- **Testing concurrency for real**, not simulating it. `threading.Barrier`
  plus real threads against a real (if small) database means the
  concurrency tests exercise actual OS scheduling non-determinism, not a
  mocked-out illusion of a race.
- **Running the whole thing locally with nothing installed** beyond Python
  and `pytest`. No Docker, no external services, no network.
- **Auditing pacing decisions after the fact.** Every decision (request,
  approval, action, full reasoning) is written to a `pacing_log` table, so
  "why did you dial 4 instead of the 12 requested at 14:32:07" always has
  a persisted, queryable answer.

## What does it make harder?

- **True multi-process/multi-machine demonstration.** The prototype's
  "workers" are threads in one Python process sharing one SQLite file.
  The correctness *pattern* generalizes to real separate processes against
  Postgres, but this repository doesn't literally spin up N processes to
  prove it — that's named directly as a "what I'd change with another
  week" item rather than glossed over.
- **Very high sustained throughput.** SQLite's single-writer model is a
  real ceiling, honestly measured rather than assumed away (see
  `load_test.py`). A production system needs the swap described below
  before it could sustain thousands of reservations/sec.
- **Wall-clock-perfect simulation determinism.** Real threads with scaled
  real-time sleeps give genuine concurrency but not byte-identical runs
  every time (thread scheduling varies). Outcome *distributions* are
  seeded and reproducible; exact tick-by-tick numbers are not guaranteed
  identical between runs. A discrete-event virtual clock would fix this at
  the cost of no longer exercising real concurrent scheduling.

## How would it evolve to production scale?

In order of what would actually be done, cheapest/highest-leverage first:

1. **Audit every hot query with `EXPLAIN QUERY PLAN` (or Postgres
   equivalent) before assuming a concurrency fix is needed.** The index
   bug above was fixed in minutes and produced a bigger improvement than
   any architectural change would have at this scale.
2. **Swap SQLite for Postgres**, keeping the exact same
   `UPDATE ... WHERE <precondition>` pattern, upgraded to
   `SELECT ... FOR UPDATE SKIP LOCKED` for the candidate-selection queries
   specifically (lets concurrent transactions each grab a different free
   row without even blocking on the SELECT — strictly better than this
   prototype's optimistic retry-on-lost-race loop under very high
   contention).
3. **Shard the agent pool** (by agent hash, team, or queue) once a single
   Postgres primary's write throughput becomes the ceiling, so contention
   domains are independent and throughput scales with shard count.
4. **Move provider-event ingestion onto a real broker** (Kafka/SQS/etc.)
   partitioned by `call_id`, so event processing scales horizontally across
   worker processes instead of being capped by one process's thread pool
   and in-memory `queue.Queue`.
5. **Split the pacing/safety loop out as its own service** consuming
   snapshots from a read-replica or streaming aggregate, so it can run on
   its own schedule independent of allocator/event-worker scaling.

None of these steps change the *correctness argument* from this prototype
— they change where the same guarantee is enforced (one atomic conditional
write per contended resource) and how many of those enforcement points can
run in parallel.
