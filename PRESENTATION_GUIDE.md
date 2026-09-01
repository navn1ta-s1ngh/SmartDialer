# SmartDialer Backend Workflow Presentation Guide

## Quick Presentation Toolkit

You have **4 ways to show the backend workflow**:

---

## 1. **Live Dashboard** (Real-time, Visual) 🎨
**Best for:** Stakeholders, non-technical audience

```bash
# Already running on
http://localhost:9000
```

### What to show:
- Real-time metrics updating (refresh every 500ms)
- Agent status chart (horizontal bars, fixed pipeline order)
- Call status chart (horizontal bars, fixed pipeline order)
- Compliance risk / abandoned calls tile (must be **0**)
- Answer rate adaptation (EWMA)

### Narration:
```
"This dashboard shows a live 30-agent campaign processing 500 borrowers 
in predictive dialing mode. The key metric is 'Abandoned Calls' — see how 
it stays at zero? That's the Safety Controller working. Without it, this 
same pacing engine would create 38% abandoned calls."
```

---

## 2. **Workflow Inspector** (Backend Details, Text-based) 📊
**Best for:** Engineers, technical deep-dive

```bash
cd smartdialer   # repo root
python3 workflow_inspector.py
```

### Menu options:
```
1. Recent pacing decisions      → Shows APPROVE/REDUCE/REJECT/FALLBACK
2. Agent state snapshot         → Where are agents right now?
3. Call state snapshot          → Call distribution breakdown
4. Safety decisions (all-time)  → Statistics of controller decisions
5. Workflow trace               → One borrower's complete journey
```

### Sample output:
```
Recent Pacing Decisions (Last 10)
================================
Time        Req  App Action              Reason
16:24:07    54   4   REDUCE              requested 54 exceeds safe unbound capacity 4
16:24:08    52   3   REDUCE              requested 52 exceeds safe unbound capacity 3
16:24:09    48   5   REDUCE              requested 48 exceeds safe unbound capacity 5
```

---

## 3. **SQL Queries** (Raw Database Inspection) 🗄️
**Best for:** Compliance audits, detailed inspection

The campaign writes to a fresh temp-directory SQLite file every run
(`tempfile.mkdtemp()`), not a fixed path — easiest way in is
`python3 workflow_inspector.py`, which auto-discovers it. For raw SQL,
find the path first:

```bash
lsof -p $(pgrep -f web_dashboard.py) 2>/dev/null | grep web_dashboard.db
sqlite3 <path-from-above>
```

### Key queries:

**See pacing decisions:**
```sql
SELECT ts, requested, approved, action, reason 
FROM pacing_log 
ORDER BY ts DESC 
LIMIT 20;
```

**See abandoned calls (compliance metric):**
```sql
SELECT COUNT(*) as abandoned_count
FROM calls 
WHERE state = 'ABANDONED';
-- Result: 0 ✅
```

**See agent state RIGHT NOW:**
```sql
SELECT state, COUNT(*) as count 
FROM agents 
GROUP BY state;
```

**See agent utilization:**
```sql
SELECT 
  (SELECT COUNT(*) FROM agents WHERE state='CONNECTED') as talking,
  (SELECT COUNT(*) FROM agents WHERE state='AVAILABLE') as available,
  (SELECT COUNT(*) FROM agents WHERE state='WRAP_UP') as wrapping_up,
  (SELECT COUNT(*) FROM agents) as total;
```

**See Safety Controller decisions distribution:**
```sql
SELECT action, COUNT(*) as count 
FROM pacing_log 
GROUP BY action 
ORDER BY count DESC;
```

---

## 4. **Architecture Diagram** (Visual Workflow) 📐

See [README.md §2](README.md#2-architecture) for the pipeline diagram and
the agent/call state machines, and [ARCHITECTURE.md](ARCHITECTURE.md) for
the design rationale behind each piece.

---

## Presentation Flow (5-10 minutes)

### **0-2 Minutes: Introduce the Problem**
```
"Collections call centers want to maximize utilization but face a 
compliance risk: if you dial too aggressively (predictive), a borrower 
might answer and have nobody to talk to — an abandoned connected call.

This system proves you can dial aggressively AND guarantee zero 
abandonment with a hard safety boundary."
```

### **2-4 Minutes: Show the Architecture**
Point to the pipeline:
```
Campaign (30 agents, 500 borrowers)
    ↓
Pacing Engine: "I want to dial 54 calls"
    ↓
Safety Controller: "I've checked capacity. I approve 4."
    ↓
Call Allocator: "Dialing 4 calls"
    ↓
Provider (Real telecom events)
    ↓
Event Workers: Processing call outcomes
    ↓
Back to Pacing (loop, every 150ms)
```

### **4-7 Minutes: Show the Dashboard**
Open http://localhost:9000, watch metrics:
- Calls initiated (controlled by Safety)
- Answer rate (adapts in real-time)
- Available agents (dynamic capacity)
- **Abandoned calls = 0** ✅

Say:
```
"Watch these metrics. We've been running for ~3 minutes, and see:
- 200+ calls initiated
- 40+ answered
- 0 abandoned

If we removed the Safety Controller, at 200 calls initiated with this 
answer rate, we'd have ~15-20 calls answered with nowhere to route them.
But we have 0. The Safety Controller prevented it."
```

### **7-10 Minutes: Show the Backend**
Open workflow inspector, choose option 1 (recent pacing decisions):
```
16:24:07    54   4   REDUCE              requested 54 exceeds safe capacity
16:24:08    52   3   REDUCE              requested 52 exceeds safe capacity
```

Say:
```
"Here's what's happening behind the scenes. Every 150ms:

1. Pacing engine computes: 'Given current utilization, answer rate, 
   and margin, I want to dial THIS many'
   
2. Safety controller says: 'But we only have THIS many agents available 
   times our overdial factor. REDUCE your request.'
   
3. We dial the approved amount.

This decision happens every tick. And the result: zero abandoned calls."
```

---

## Key Metrics to Highlight

| Metric | What It Shows | Target |
|--------|---|---|
| **Abandoned Calls** | Compliance risk | **0** |
| **Answer Rate** | How often borrowers answer | ~20-35% |
| **Calls Initiated** | Throughput (Safety-gated) | Growing |
| **Calls Completed** | Success rate | Steady climb |
| **Available Agents** | Current capacity | 15-25 (dynamic) |
| **Safety Decisions** | REDUCE (most), APPROVE, REJECT, FALLBACK | Stats matter |

---

## Before-and-After Comparison

**What we show in simulation:**
```
Safety ON:   answered=25, abandoned=0 (0% abandon rate)      ✅
Safety OFF:  answered=44, abandoned=17 (38.6% abandon rate)  ❌
```

**Narrative:**
```
"Same pacing engine. Same seed. Same call volume. Same answer rate.

The only difference: is the Safety Controller running?

With it: 25 answered, 0 abandoned. Perfect compliance.
Without it: 44 answered, 17 abandoned. Compliance violation.

The Safety Controller is the difference."
```

---

## Technical Questions You Can Answer

**Q: How does it prevent double-booking agents?**
A: Atomic DB operations. `UPDATE agents SET state='DIALING' WHERE state='AVAILABLE' AND agent_id=?` — only one worker can win this race.

**Q: What if events arrive out-of-order?**
A: Call state machine validates transitions. `RINGING → ANSWERED` is allowed. `COMPLETED → RINGING` is rejected.

**Q: How does it adapt to answer rate?**
A: EWMA (exponentially weighted moving average). Every call outcome updates it. Formula: `rate = 0.85*rate + 0.15*outcome`.

**Q: What if the provider goes down?**
A: Provider health score drops. Safety Controller's `overdial_factor *= provider_health`. Circuit breaker opens at health < 55%, forces fallback to progressive.

---

## Tools Available

| Tool | Purpose | Command |
|---|---|---|
| **Dashboard** | Real-time viz | `http://localhost:9000` |
| **Inspector** | Backend inspection | `python3 workflow_inspector.py` |
| **SQLite** | Raw queries | `sqlite3 <path>` — path varies per run, see §3 above |
| **Logs** | Decision history | See pacing_log table |
| **Dashboard deep-dive** | Metric-by-metric walkthrough | `DASHBOARD_DEMO.md` |

---

## Failure Scenarios (Bonus Demo)

If you want to show **how the system responds to failures**:

```bash
# See simulate.py for these scenarios
python3 simulate.py --fast

# Scenario D shows:
# - 15 agents suddenly disappear
# - Provider outage
# - Answer rate crashes
# - Result: Still 0 abandoned calls (Safety adapts)
```

---

## Presentation Checklist

- [ ] Dashboard running on http://localhost:9000
- [ ] Browser showing live metrics
- [ ] Have `workflow_inspector.py` ready in a second terminal
- [ ] Know the 4 Safety Controller decisions: APPROVE, REDUCE, REJECT, FALLBACK_TO_PROGRESSIVE
- [ ] Memorize the key stat: **abandoned = 0** (always, with Safety on)
- [ ] Have the `lsof`/`sqlite3` fallback ready in case the inspector isn't handy (§3)
- [ ] Know the answer-rate EWMA mechanism (smooths ~85% history + ~15% new outcome, by default)
- [ ] Understand agent state transitions: `AVAILABLE → RESERVED → DIALING → CONNECTED → WRAP_UP → AVAILABLE`

---

## Talking Points by Audience

**For executives / non-technical:**
1. "This system prevents compliance violations without sacrificing throughput."
2. "Abandoned call rate: 0%, with the Safety Controller on."
3. "The exact same pacing engine produces a ~25-40% abandon rate without it."
4. "The live dashboard shows this running, not just claimed."

**For engineers:**
1. Pacing engine has zero import path to the provider or allocator — verified by a test, not just a convention.
2. Every contended write is a single atomic `UPDATE ... WHERE <precondition>`.
3. Out-of-order/duplicate provider events are handled by idempotency-key dedup + transition-table validation.
4. EWMA for adaptive answer rate; hysteresis (3+ healthy ticks) before predictive overdial resumes after a fallback.
5. Concurrent event workers share one SQLite file — no distributed consensus needed at this scale.

**For compliance/legal:**
1. Zero abandoned calls means zero violations of that specific rule, by construction.
2. Every pacing decision is logged to `pacing_log` — requested, approved, action, reason.
3. Safety thresholds are enforced atomically from a snapshot queried fresh each tick, not cached.
4. Automatic fallback to progressive-only dialing if the provider degrades or abandonment starts climbing.

---

## Bottom Line

The presentation is simply:

1. **Show the dashboard** (visual proof)
2. **Explain the pipeline** (architecture)
3. **Show pacing decisions** (backend logic)
4. **Highlight abandoned=0** (compliance win)
5. **Compare with/without Safety** (the value)

That's it. The system speaks for itself.
