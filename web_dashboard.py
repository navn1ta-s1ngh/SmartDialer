#!/usr/bin/env python3
"""
Web-based live dashboard for the SmartDialer system.
Run this and open http://localhost:5000 in your browser for real-time metrics.
"""
from __future__ import annotations
import atexit
import json
import os
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
        
        # Seed with agents and borrowers
        campaign.seed(num_agents=30, num_borrowers=500)
        
        # Start the background loops
        campaign.start()
        
        # Continuously update metrics
        start = time.time()
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
            time.sleep(0.2)
        
        # Stop the campaign
        campaign.stop()
    except Exception as e:
        import traceback
        print(f"Campaign error: {e}")
        traceback.print_exc()
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
        return jsonify({"error": "Campaign not started"}), 503
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

# Started at import time (not inside __main__) so the campaign also runs
# under a production WSGI server such as `gunicorn web_dashboard:app`,
# which imports this module directly and never executes __main__.
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

    # Give campaign a moment to start
    time.sleep(1)

    port = int(os.environ.get("PORT", 9000))
    try:
        app.run(debug=False, host="0.0.0.0", port=port, use_reloader=False)
    except KeyboardInterrupt:
        print("\nShutting down...")
        state["running"] = False
        campaign_thread.join(timeout=2)
