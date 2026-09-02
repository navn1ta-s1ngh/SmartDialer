# Dashboard Walkthrough

`python3 web_dashboard.py`, then http://localhost:9000. What comes up is
a real Campaign — 30 agents, 500 borrowers, predictive mode — running in
a background thread, with the page polling `/api/metrics` twice a second.
Nothing on the page is simulated separately from what the engine is
actually doing; it's the same `Campaign`/`PacingEngine`/`SafetyController`
stack that `simulate.py` and `load_test.py` exercise, just watched live
instead of summarized after the fact.

## What the pipeline is doing underneath it

```
Campaign (Pacing Loop + Event Workers)
    ↓
30 AVAILABLE AGENTS ←→ PREDICTIVE DIALER (places calls without binding agents yet)
    ↓
500 QUEUED BORROWERS → Provider Events → Safety Controller → Agent Binding
```

Predictive dialing places calls before an agent is committed to them —
that's the entire point of the mode, and also the entire source of risk:
if a borrower answers and nobody's free, that's an abandoned connected
call. The dashboard's job is to make that risk, and the mechanism that
prevents it, visible while it's happening rather than just claimed
afterward.

## The tiles, and what each one is actually reporting

**Compliance risk** counts calls where a borrower answered and no agent
was available to take them. With the Safety Controller running, this sits
at zero indefinitely — not because abandonment can't happen in principle,
but because the controller is specifically computing, every 150ms, how
many unbound calls are safe to have in flight and refusing to authorize
more than that.

**Calls initiated** is a running total across both dialing modes. It
doesn't split progressive from predictive on this page — that breakdown
(e.g. `328 initiated, progressive=52 predictive=276`) only shows up in
`simulate.py`'s console output, which logs it explicitly per scenario. If
that split matters for a given demo, the honest way to get it live is to
add `mode` to the `/api/metrics` payload, or read `pacing_log` directly.

**Answer rate** is a live EWMA, not a fixed number — it starts jumping
around on a small sample in the first several calls and visibly settles
somewhere in the 20-40% range for this seed by around the twentieth or
thirtieth call. A real, sudden shift in it (the terminal dashboard's `c`
key simulates exactly this) makes the pacing engine ask for more dials
per expected connect on the next tick, since the formula uses this same
number.

**Provider health** tracks the telecom provider's own reliability —
timeouts, dropped calls — deliberately excluding ordinary "nobody picked
up" outcomes, which is a separate signal (there's a note on why in
`ARCHITECTURE.md`'s design-tradeoffs section: conflating the two used to
cause a low-answer-rate campaign to wrongly look like a broken provider).
Below 55%, the circuit breaker opens and forces a fallback to
progressive-only dialing; getting back to predictive mode after that
requires several consecutive healthy ticks, not just one.

**Available agents** moves as agents cycle through
`AVAILABLE → RESERVED → DIALING → CONNECTED → WRAP_UP → AVAILABLE`. It
never exceeds 30, and when it drops, the Safety Controller's next
snapshot reflects that immediately — there's no cache lag built in
anywhere in this loop.

**In-flight unbound** is the number of predictive calls sitting in the
exact risk window described above — answered, but not yet bound to an
agent. It's usually low single digits and briefly spikes when several
calls happen to answer at once; watching it clear back down as agents
bind is the mechanism working, not luck.

## The two charts

Both are ordinary horizontal bar charts, one bar per state, held in a
fixed pipeline order rather than sorted alphabetically — a bar's position
on the page never changes as counts change, only its length does. That
was a deliberate fix: an earlier version sorted state labels
alphabetically and only rendered states with a nonzero count, which meant
bars would appear, disappear, and reshuffle position on every tick. Each
chart uses one accent color rather than a different hue per state, since
the point here is comparing magnitude across a snapshot, not
distinguishing series.

Agent status runs `OFFLINE → AVAILABLE → RESERVED → DIALING → CONNECTED →
WRAP_UP → PAUSED`. In a healthy run, AVAILABLE is usually the tallest bar
— somewhere around 15 to 25 of the 30 — with CONNECTED and WRAP_UP
together showing current utilization. OFFLINE stays at zero unless
something has deliberately forced agents offline (the terminal
dashboard's `d` key, or Scenario D in `simulate.py`).

Call status runs the full lifecycle: `QUEUED → RESERVED → INITIATED →
RINGING → ANSWERED → CONNECTED → COMPLETED → FAILED → CANCELLED →
ABANDONED`. FAILED is often the tallest bar in this chart, and that's
expected — most dials don't get answered at a realistic 20-35% answer
rate, and a failed dial (no answer, provider error) is not itself a
problem. The one bar that's supposed to stay flat at zero for the entire
run, no matter how long it runs, is ABANDONED.

## What a full run actually looks like, start to finish

The first ten to fifteen seconds are warmup: the dashboard comes up with
every tile at zero, the campaign finishes seeding, the first pacing tick
fires against a snapshot that's almost entirely idle agents, and calls
start flowing. Available agents drops as some move into DIALING and
CONNECTED, and the answer-rate EWMA is still noisy on a small sample.

From roughly fifteen to forty seconds in, it settles into steady state:
the pacing loop keeps ticking every 150ms, event workers keep draining
provider callbacks into call-state transitions, and every Safety
Controller decision lands in `pacing_log`. Calls initiated keeps
climbing, the answer rate holds roughly steady, available agents
oscillates rather than trending to an extreme, calls completed rises
steadily, and compliance risk stays exactly where it started: zero.

Watching `python3 workflow_inspector.py` alongside the dashboard (it
reads the same database the campaign is writing to) shows the mechanism
behind that number directly — a stream of `requested / approved / action
/ reason` rows, most of them REDUCE, each naming the exact request that
was too aggressive and the exact capacity it got cut to.

Forcing a failure makes the same behavior visible under stress instead of
just at steady state. The terminal dashboard (`python3 dashboard.py`)
exposes `o` (toggle a provider outage), `c` (crash the answer rate to
8%), and `d` (drop 15 agents at once) as live keys; `simulate.py --fast`
runs the scripted version of the same scenarios. In every case, the next
tick's snapshot reflects the new reality immediately, the pacing request
changes in response, and abandoned calls still doesn't move.

## Looking at the database directly

The campaign writes to a SQLite file in a fresh temp directory every run
(`tempfile.mkdtemp()`) — there's no fixed path like `/tmp/web_dashboard.db`
to rely on, and it's removed again when the process exits. The workflow
inspector finds it automatically:

```bash
python3 workflow_inspector.py
```

Doing it by hand instead:

```bash
# macOS: tempfile.mkdtemp() lands under $TMPDIR, not /tmp
lsof -p $(pgrep -f web_dashboard.py) 2>/dev/null | grep web_dashboard.db
sqlite3 <path-from-above>
```

```sql
-- the compliance metric itself -- should always read 0
SELECT COUNT(*) FROM calls WHERE state = 'ABANDONED';

-- recent pacing decisions
SELECT ts, requested, approved, action, reason
FROM pacing_log ORDER BY ts DESC LIMIT 20;

-- agent state distribution right now
SELECT state, COUNT(*) FROM agents GROUP BY state;
```

## The claim this is all backing up

`simulate.py` runs the same comparison the dashboard demonstrates live,
but isolates it to one variable: the same pacing engine, same seed, same
call volume, run once with the Safety Controller enforcing its boundary
and once with it removed. One real run looked like this (exact figures
shift between runs — real threads, wall-clock timing — but the shape
holds):

```
safety_on:       answered=25  abandoned=0   (0% abandon rate)
safety_reckless: answered=44  abandoned=17  (38.6% abandon rate)
```

The pacing engine didn't change between those two runs. The only
difference is whether something downstream double-checked its numbers
before dialing — which is exactly what the dashboard's compliance-risk
tile is a live window into.

## If something looks wrong

If `/api/metrics` won't come up at all:
```bash
curl http://localhost:9000/api/metrics
```
should return JSON. A `503` for the first few seconds after launch is
normal — the campaign is still seeding — but if it persists, whatever
started `web_dashboard.py` should have a traceback printed to its
terminal.

If the numbers look frozen rather than absent: the demo campaign caps
itself at a five-minute window per process, by design, so the fix is
just restarting it (`python3 web_dashboard.py`) for a fresh run.
