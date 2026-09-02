#!/usr/bin/env python3
"""
Web-based live dashboard for the SmartDialer system.
Run this and open http://localhost:5000 in your browser for real-time metrics.
"""
from __future__ import annotations
import atexit
import json
import os
import resource
import shutil
import tempfile
import threading
import time
from flask import Flask, render_template, jsonify
from smartdialer.campaign import Campaign
from smartdialer.providers import make_custom_provider
from smartdialer.models import DialMode

app = Flask(__name__)

# Global state
campaign = None
campaign_thread = None
state = {"running": False}
latest_snapshot = None
latest_metrics = None
_db_dir = None

def _cleanup_temp_db():
    """Registered with atexit (not just a try/finally in run_campaign)
    because Flask's dev server can swallow the SIGINT that Ctrl+C sends,
    so app.run() returns normally instead of raising KeyboardInterrupt --
    in that case the campaign thread (daemon=True) never gets to run its
    own cleanup, since daemon threads are torn down, not unwound, once
    the interpreter starts shutting down. atexit runs in the main thread
    during that shutdown regardless, so it's the one place this is
    guaranteed to fire. Without this, every run leaked its tmp dir."""
    if _db_dir:
        shutil.rmtree(_db_dir, ignore_errors=True)

atexit.register(_cleanup_temp_db)

def run_campaign():
    """Run the campaign in a background thread."""
    global campaign, latest_snapshot, latest_metrics, _db_dir

    _db_dir = tempfile.mkdtemp()
    db_path = os.path.join(_db_dir, "web_dashboard.db")
    _pid = os.getpid()
    _tid = threading.get_ident()
    print(f"[campaign] pid={_pid} tid={_tid} starting, db_path={db_path}", flush=True)
    try:
        provider = make_custom_provider(name="WebDashboard",
                                       time_scale=0.05,
                                       seed=42,
                                       answer_rate=0.35)
        campaign = Campaign(campaign_id="web_dashboard",
                           db_path=db_path,
                           mode=DialMode.PREDICTIVE,
                           provider=provider,
                           tick_interval=0.15,
                           num_event_workers=4)
        print(f"[campaign] pid={_pid} created, seeding...", flush=True)

        # Seed with agents and borrowers
        campaign.seed(num_agents=30, num_borrowers=500)
        print(f"[campaign] pid={_pid} seeded, starting background loops...", flush=True)

        # Start the background loops
        campaign.start()
        print(f"[campaign] pid={_pid} running, serving metrics now", flush=True)

        # Continuously update metrics
        start = time.time()
        tick = 0
        while state["running"] and (time.time() - start) < 300:
            report = campaign.report()
            snapshot = campaign.build_snapshot()

            # Build metrics dict
            counters = report["counters"]
            stats = report["stats"]
            latest_metrics = {
                "timestamp": time.time(),
                "calls_initiated": counters.get("calls_initiated", 0),
                "calls_answered": counters.get("calls_answered", 0),
                "calls_completed": counters.get("calls_completed", 0),
                "calls_abandoned": counters.get("calls_abandoned", 0),
                "calls_failed": counters.get("calls_failed", 0),
                "answer_rate": round(stats["answer_rate"], 3),
                "abandon_rate": round(stats["abandon_rate"], 3),
                "provider_health": round(report["provider_health"], 3),
                "agent_states": report["agent_counts"],
                "call_states": report["call_counts"],
                "available_agents": snapshot.available_agents,
                "ringing_unbound": snapshot.ringing_unbound_calls,
            }
            latest_snapshot = snapshot
            tick += 1
            if tick == 1:
                print(f"[campaign] pid={_pid} first metrics set: "
                      f"latest_metrics is None = {latest_metrics is None}", flush=True)
            elif tick % 25 == 0:
                # ru_maxrss: KB on Linux (Render), bytes on macOS -- diagnostic only.
                rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                print(f"[campaign] pid={_pid} tick={tick} alive, ru_maxrss={rss}", flush=True)
            time.sleep(0.2)
        print(f"[campaign] pid={_pid} loop exited normally after {tick} ticks "
              f"(state_running={state['running']})", flush=True)

        # Stop the campaign
        campaign.stop()
    except Exception as e:
        import sys
        import traceback
        print(f"Campaign error: {e}", flush=True)
        traceback.print_exc()
        sys.stderr.flush()
    finally:
        # Covers the case where this loop exits on its own (300s cap)
        # while the server keeps running; atexit above covers process exit.
        _cleanup_temp_db()

@app.route("/")
def index():
    return render_template("dashboard.html")

@app.route("/api/metrics")
def metrics():
    """Return current metrics as JSON."""
    if latest_metrics is None:
        return jsonify({
            "error": "Campaign not started",
            "debug": {
                "pid": os.getpid(),
                "campaign_is_none": campaign is None,
                "state_running": state["running"],
                "campaign_thread_alive": campaign_thread.is_alive() if campaign_thread else None,
            },
        }), 503
    return jsonify(latest_metrics)

@app.route("/api/agent-states")
def agent_states():
    """Return agent state distribution."""
    if latest_snapshot is None:
        return jsonify({}), 503
    return jsonify(latest_snapshot.agent_state_counts)

@app.route("/api/call-states")
def call_states():
    """Return call state distribution."""
    if latest_snapshot is None:
        return jsonify({}), 503
    return jsonify(latest_snapshot.call_state_counts)

_start_lock = threading.Lock()

def start_campaign_once():
    """Starts the background campaign thread in whichever process calls
    this -- idempotent, so it's safe to call from more than one place.

    This is NOT called at plain module-import time. gunicorn always
    imports the app once in its master process to validate it loads, and
    on at least this deployment target the worker inherits that import
    via fork() rather than re-importing fresh -- fork() only carries the
    calling thread into the child, so a thread started at import time
    ends up alive only in the master (which never serves HTTP requests),
    while the worker's copy of that thread is marked dead by Python's own
    post-fork bookkeeping, and its copy of module globals like `campaign`
    is frozen at whatever they were the instant before the fork. Confirmed
    in production logs: the campaign ran correctly for hundreds of ticks
    in the master while every request hit a worker reporting
    campaign=None, thread=dead, forever.

    Calling this from gunicorn's `post_fork` hook (see gunicorn.conf.py)
    instead guarantees it runs inside the actual process handling
    requests. The `__main__` block below still needs its own call for the
    plain `python3 web_dashboard.py` path, which has no fork involved."""
    global campaign_thread
    with _start_lock:
        if campaign_thread is not None:
            return
        state["running"] = True
        campaign_thread = threading.Thread(target=run_campaign, daemon=True)
        campaign_thread.start()

if __name__ == "__main__":
    print("\n" + "="*70)
    print("SmartDialer Web Dashboard")
    print("="*70)
    print("Starting campaign in background...")
    print("Press Ctrl+C to stop")
    print("="*70 + "\n")

    start_campaign_once()

    # Give campaign a moment to start
    time.sleep(1)

    port = int(os.environ.get("PORT", 9000))
    try:
        app.run(debug=False, host="0.0.0.0", port=port, use_reloader=False)
    except KeyboardInterrupt:
        print("\nShutting down...")
        state["running"] = False
        campaign_thread.join(timeout=2)
