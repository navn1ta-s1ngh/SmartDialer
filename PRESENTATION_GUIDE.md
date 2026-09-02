# SmartDialer Presentation Guide

There are four ways to show how this system actually behaves rather than
just describe it: the live dashboard, the workflow inspector, direct SQL
against the campaign's own database, and the architecture diagrams in the
README. Each one shows a different layer of the same underlying fact — a
pacing engine that wants to dial aggressively, and a Safety Controller
that independently checks whether that's actually safe before any call
goes out.

## The live dashboard

Running `python3 web_dashboard.py` and opening http://localhost:9000
gives the most immediate view: a 30-agent campaign working through 500
borrowers in predictive mode, with metrics refreshing twice a second. The
number that matters most sits in the "Compliance risk" tile — the count
of calls where a borrower answered and no agent was free to take them.
With the Safety Controller active, that number stays at zero for the
entire run, however long it's left going. Turn the Safety Controller off
(the comparison further down shows exactly how) and the same pacing
engine, on the same seed and call volume, produces an abandon rate in
the high twenties to high thirties percent — which is the whole reason
this boundary exists in the first place.

Below the metrics tiles sit two horizontal bar charts, agent status and
call status, both kept in a fixed pipeline order rather than sorted
alphabetically, so a bar's position stays put and only its length moves
as counts change. The answer-rate figure is worth pointing at directly:
it's a live EWMA, and watching it for the first twenty or so calls shows
it visibly settling rather than sitting at some hardcoded constant.

## The workflow inspector

`python3 workflow_inspector.py`, run from the repo root while a campaign
is active (the dashboard or any of the simulation scripts), opens a small
menu against the same SQLite database the dashboard is writing to:
recent pacing decisions, an agent-state snapshot, a call-state snapshot,
aggregate Safety Controller decision counts, and a single borrower's full
journey through the system end to end.

Option 1 is usually the one worth showing first — ten rows of
`requested / approved / action / reason`, and in practice most of them
read REDUCE, each one naming the exact number that was too aggressive and
the exact capacity it got cut down to:

```
Time        Req  App Action              Reason
16:24:07    54   4   REDUCE              requested 54 exceeds safe unbound capacity 4
16:24:08    52   3   REDUCE              requested 52 exceeds safe unbound capacity 3
16:24:09    48   5   REDUCE              requested 48 exceeds safe unbound capacity 5
```

That's the pacing engine asking for something ambitious every 150ms, and
the Safety Controller answering back with a number derived from a fresh
read of actual agent availability — not from whatever the pacing engine
assumed.

## Reading the database directly

The campaign writes to a fresh temp-directory SQLite file every run
(`tempfile.mkdtemp()`), not a fixed path, so `sqlite3 /tmp/whatever.db`
won't work twice in a row. The inspector above finds it automatically;
to do it by hand instead:

```bash
lsof -p $(pgrep -f web_dashboard.py) 2>/dev/null | grep web_dashboard.db
sqlite3 <path-from-above>
```

The queries worth having ready:

```sql
-- pacing decisions, most recent first
SELECT ts, requested, approved, action, reason
FROM pacing_log ORDER BY ts DESC LIMIT 20;

-- the compliance metric itself -- should read 0
SELECT COUNT(*) FROM calls WHERE state = 'ABANDONED';

-- where every agent sits right now
SELECT state, COUNT(*) FROM agents GROUP BY state;

-- how often each Safety Controller action actually fires
SELECT action, COUNT(*) FROM pacing_log GROUP BY action ORDER BY COUNT(*) DESC;
```

## Architecture diagrams

These live in the README rather than repeated here — [§2](README.md#2-architecture)
has the pipeline diagram and the agent/call state machines, and
[ARCHITECTURE.md](ARCHITECTURE.md) has the reasoning behind each piece
(why SQLite, why this concurrency pattern, why this pacing formula).

## Walking through it end to end

Started cold, the sequence is always the same. The campaign seeds 30
agents and 500 borrowers, the pacing loop fires its first tick, and the
Safety Controller evaluates that first request against a snapshot that's
mostly idle agents — so early decisions tend toward APPROVE. Within the
first ten or fifteen seconds, calls are flowing, the answer-rate EWMA is
still jumping around on a small sample, and available agents starts
dropping as some move into DIALING or CONNECTED.

By the fifteen-to-forty-second mark it settles into a steady state: the
pacing loop keeps ticking every 150ms, event workers keep draining
provider callbacks into call-state transitions, and the decision log
fills up mostly with REDUCE — the pacing engine consistently wants more
than the Safety Controller will allow, and gets cut down rather than
outright rejected. Calls initiated keeps climbing, the answer rate
settles somewhere around 20-35% for this seed, available agents
oscillates instead of trending toward zero or thirty, and — the one
number that's supposed to never move — abandoned calls stays at 0.

The four actions the Safety Controller can take, in order of how often
they actually show up in a normal run:

| Decision | What it means |
|---|---|
| REDUCE | Request was too aggressive — dial fewer than asked (the common case) |
| APPROVE | Request was already within safe capacity — dial exactly as asked |
| REJECT | No safe capacity right now — zero dials this tick |
| FALLBACK_TO_PROGRESSIVE | Unsafe for predictive dialing — progressive-only until conditions recover |

Forcing a failure scenario makes the same mechanism visible from a
different angle. `python3 dashboard.py` (the terminal version) exposes
three keys live: `o` toggles a simulated provider outage, `c` crashes the
answer rate to 8%, `d` drops 15 agents at once. `python3 simulate.py`
runs the scripted equivalent of all of this — Scenario D specifically
chains an agent drop, a provider outage, and an answer-rate crash back to
back. In every case the same thing happens: available capacity changes,
the next tick's snapshot reflects it immediately (there's no cache to go
stale), the pacing engine's request changes accordingly, and abandoned
calls still doesn't move.

## The comparison that makes the point

`simulate.py` includes a bonus run that isolates exactly one variable:
the same pacing engine, the same seed, the same call volume, run once
with the Safety Controller enforcing its boundary and once with it
removed. A real run looked like this (exact numbers shift run to run —
this uses real threads and wall-clock timing — but the shape holds):

```
safety_on:       answered=25  abandoned=0   (0% abandon rate)
safety_reckless: answered=44  abandoned=17  (38.6% abandon rate)
```

Nothing about the pacing engine changed between those two runs. The only
difference is whether something downstream double-checked its work
before dialing.

## Things worth knowing off the top of your head

How it stops two workers from double-booking the same agent: a single
atomic conditional update, `UPDATE agents SET state='DIALING' WHERE
state='AVAILABLE' AND agent_id=?` — of two threads racing on the same
row, exactly one matches the WHERE clause and wins.

How it handles events arriving out of order: the call state machine only
accepts legal forward transitions. `RINGING → ANSWERED` goes through;
`COMPLETED → RINGING` is rejected outright, so a stale or duplicate event
can't rewind a call that's already finished.

How the answer rate adapts: it's an EWMA, decay 0.85 by default — every
outcome nudges it, so a real shift in behavior shows up within a handful
of calls rather than being diluted by weeks of history.

What happens if the provider degrades: its health score drops, the
Safety Controller's overdial factor gets multiplied down toward zero
along with it, and once health falls under 55% the circuit breaker opens
and forces a fallback to progressive-only dialing until several
consecutive healthy ticks earn back predictive mode.

## Reference

| Tool | Purpose |
|---|---|
| `python3 web_dashboard.py` → http://localhost:9000 | live visual dashboard |
| `python3 workflow_inspector.py` | interactive backend inspection |
| `sqlite3 <path>` (see above for finding it) | raw queries against the live database |
| `DASHBOARD_DEMO.md` | metric-by-metric walkthrough of the dashboard specifically |
| `python3 simulate.py [--fast]` | scripted scenarios A-D, chaos, and the safety on/off comparison |

The short version of all of this: the dashboard shows it happening live,
the inspector shows why each decision was made, the database backs both
of those up with a persisted audit trail, and the before/after comparison
proves the Safety Controller — not the pacing engine — is what's actually
holding the compliance guarantee up.
