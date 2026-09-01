# SmartDialer Dashboard Demo Guide

## Quick Start

**Start it:** `python3 web_dashboard.py`, then open **http://localhost:9000**

The dashboard shows a **live 30-agent, 500-borrower predictive dialing
campaign** running in real time, with metrics refreshing every 500ms.

---

## What You're Watching

### The Pipeline in Action
```
Campaign (Pacing Loop + Event Workers)
    ↓
30 AVAILABLE AGENTS ←→ PREDICTIVE DIALER (places calls without binding agents yet)
    ↓
500 QUEUED BORROWERS → Provider Events → Safety Controller → Agent Binding
```

---

## Key Metrics to Watch

### 1. Compliance Risk (Abandoned Calls)
- **What it is:** Calls where a borrower answered but no agent was available
- **Target:** **ALWAYS 0** when the Safety Controller is active
- **Why it matters:** Direct compliance violation in collections
- **Location:** "Compliance risk" tile, top row

**What you'll see:** stays at **0** as the Safety Controller prevents overdial —
this is the entire point of the architecture.

---

### 2. Calls Initiated
- One tile shows the running total of calls started, across both modes.
- The progressive-vs-predictive split (e.g. `328 (progressive=52,
  predictive=276)`) is not broken out in the live dashboard — that
  breakdown is printed by `simulate.py`, which logs it explicitly per
  scenario. If you want to see it live, add `mode` to the dashboard's
  `/api/metrics` payload, or read `pacing_log` directly (see
  [SQL Queries](#sql-queries-raw-database-inspection) below).

---

### 3. Answer Rate (EWMA)
- Exponentially weighted moving average of how often borrowers answer.
- Adapts in real time from call outcomes; affects pacing directly — a
  lower answer rate means more dials are needed to fill the same capacity.

**What to watch:** typically 20-40% for this seed; watch it stabilize
after ~20-30 calls. If it drops suddenly, the Safety Controller becomes
more conservative.

---

### 4. Provider Health
- **100% = healthy** (all calls succeeding on the provider side)
- **< 55% → circuit breaker opens**, forcing a fallback to progressive
- Tracks provider reliability (timeouts, dropped calls) — **not**
  ordinary "borrower didn't answer" outcomes, which are a separate signal
  (see `ARCHITECTURE.md`'s design-tradeoffs section on why those are kept
  independent).

**What to watch:** usually near 100%; recovery from a dip requires 3+
consecutive healthy ticks (hysteresis) before predictive overdial resumes.

---

### 5. Available Agents
- Real-time count of agents ready to take calls.
- Agents cycle: `AVAILABLE → RESERVED → DIALING → CONNECTED → WRAP_UP → AVAILABLE`.
- Never exceeds 30 (total seeded agents). Lower availability makes the
  Safety Controller reduce dial requests.

---

### 6. In-Flight Unbound
- Predictive calls that have been answered but are still awaiting an
  agent bind. This is exactly the risk window the Safety Controller
  manages — if this ever exceeded safe capacity, you'd see abandonment.

**What to watch:** typically low single digits; spikes briefly when
several calls answer at once, then clears as agents bind.

---

## The Charts

Both charts are **horizontal bar charts**, one bar per state, always shown
in fixed pipeline order (not alphabetical) so a bar's position never
jumps around as counts change — only its length does. Each chart uses a
single accent color (its job is to compare magnitude across states in one
snapshot, not to tell states apart by hue), with the count labeled at the
tip of each bar and the full breakdown available on hover.

### Agent status
Where the 30 agents are right now, in pipeline order:

`OFFLINE → AVAILABLE → RESERVED → DIALING → CONNECTED → WRAP_UP → PAUSED`

**What to watch:**
- AVAILABLE should be the largest bar most of the time (agents working,
  not sitting idle) — roughly 15-25 out of 30.
- CONNECTED + WRAP_UP together show current utilization.
- OFFLINE should stay at 0 unless you're running the terminal dashboard's
  `d` key (force-drop agents) or `simulate.py`'s Scenario D.

### Call status
The lifecycle of every call currently tracked, in pipeline order:

`QUEUED → RESERVED → INITIATED → RINGING → ANSWERED → CONNECTED → COMPLETED → FAILED → CANCELLED → ABANDONED`

**What to watch:**
- COMPLETED grows steadily — the success metric.
- FAILED is normal and often the largest bar (most dials don't get
  answered at a realistic ~20-35% answer rate).
- **ABANDONED should stay at 0.** That's the Safety Controller's job —
  never let this bar have any height.

---

## Real-Time Workflow Demonstration

### Phase 1: Warmup (first ~10-15 seconds)
1. Dashboard loads → all tiles at 0.
2. Campaign starts seeding 30 agents / 500 borrowers.
3. First pacing decision: "how many should I dial?"
4. Safety Controller checks: approve / reduce / reject / fallback.
5. Calls start flowing; Calls Initiated rises, Available Agents drops as
   agents move into DIALING/CONNECTED.

### Phase 2: Steady state (~15-40 seconds)
- Pacing loop ticks every 150ms: decide how many to dial.
- Event workers drain provider events, transitioning calls.
- Every Safety Controller decision is logged to `pacing_log`.

**What to watch:** Calls Initiated keeps climbing, Answer Rate settles
(commonly ~20-35%), Available Agents oscillates in a dynamic equilibrium,
Calls Completed rises steadily, and **Compliance Risk stays at 0**.

### Phase 3: The Safety Controller's decisions

Each pacing tick produces one of four actions:

| Decision | Meaning |
|---|---|
| **APPROVE** | Request within safe capacity — dial as asked |
| **REDUCE** | Request too aggressive — dial less (most common in practice) |
| **REJECT** | No safe capacity right now — 0 dials |
| **FALLBACK_TO_PROGRESSIVE** | Unsafe for predictive — progressive-only until it recovers |

To see these live, run `python3 workflow_inspector.py` in a second
terminal alongside the dashboard (see below) — it reads the same
`pacing_log` table the dashboard's campaign is writing to.

### Phase 4: Failure scenarios (what-if)

To actually *see* these instead of just reading about them, run the
terminal dashboard (`python3 dashboard.py`) and use its live keys, or run
`python3 simulate.py --fast` for a scripted version of the same:

- **`o`** toggles a simulated provider outage → provider health drops,
  Safety forces fallback.
- **`c`** toggles an answer-rate crash (→ 8%) → pacing engine requests
  far more dials per expected connect; Safety keeps the approved count
  capped regardless.
- **`d`** drops 15 agents at once → Available Agents falls immediately;
  next tick's snapshot reflects it (no stale cache).

In every case: **Abandoned stays at 0.**

---

## Inspecting the backend while it runs

The dashboard's campaign writes to a SQLite database in a fresh temp
directory **every run** (`tempfile.mkdtemp()`), so there's no fixed path
like `/tmp/web_dashboard.db` to point a `sqlite3` shell at — it's
different every time you start `web_dashboard.py`, and it's deleted again
when the process exits. The easy way to inspect it without hunting down
that path yourself:

```bash
python3 workflow_inspector.py
```

This auto-discovers the active database (it searches recently-modified
`web_dashboard.db` files) and gives you a menu: recent pacing decisions,
agent/call state snapshots, Safety Controller decision stats, and a
single borrower's full workflow trace.

If you specifically want raw SQL, find the path first:

```bash
# macOS: tempfile.mkdtemp() lands under $TMPDIR, not /tmp
lsof -p $(pgrep -f web_dashboard.py) 2>/dev/null | grep web_dashboard.db
sqlite3 <path-from-above>
```

```sql
-- Abandoned calls (the compliance metric) -- should always be 0
SELECT COUNT(*) FROM calls WHERE state = 'ABANDONED';

-- Recent pacing decisions
SELECT ts, requested, approved, action, reason
FROM pacing_log ORDER BY ts DESC LIMIT 20;

-- Agent state distribution right now
SELECT state, COUNT(*) FROM agents GROUP BY state;
```

---

## The Proof (from `simulate.py`)

The dashboard is the live, visual version of the exact same claim
`simulate.py`'s bonus comparison makes with numbers (run it yourself —
`python3 simulate.py --fast` — exact figures vary run to run since the
simulation uses real threads and wall-clock timing, but the pattern
holds):

```
Safety ON:   answered=X, abandoned=0    (0% abandon rate)      ✅
Safety OFF:  answered=Y, abandoned=Z    (~25-40% abandon rate) ❌
```

Same pacing engine, same seed, same call volume — the only difference is
whether the Safety Controller is enforcing the boundary between "how
aggressively should we dial" and "is that actually safe."

---

## Troubleshooting

**Dashboard not loading?**
```bash
curl http://localhost:9000/api/metrics
# Should return JSON with current metrics
```

**Metrics stuck at 0 / a 503 from `/api/metrics`?**
The campaign takes a moment to seed and start — wait 2-3 seconds after
launch. If it persists, check the terminal `web_dashboard.py` is running
in for a traceback.

**Dashboard was running but now `/api/metrics` looks frozen?**
The demo campaign runs for a fixed 5-minute window per process
(`web_dashboard.py`'s own cap) — restart it (`python3 web_dashboard.py`)
for a fresh run.
