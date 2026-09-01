#!/usr/bin/env python3
"""
Interactive backend workflow inspector.
Shows live database state and pacing decisions.

Usage:
    python3 workflow_inspector.py
    
Then choose:
    1. Show live pacing decisions
    2. Show agent state snapshot
    3. Show call state distribution
    4. Show safety controller decisions
    5. Show complete workflow trace
"""

import sqlite3
import json
from datetime import datetime
from pathlib import Path
import tempfile

def find_db_path():
    """Find the active web_dashboard.db"""
    import subprocess
    # On macOS, tempfile creates in /var/folders, not /tmp
    for search_dir in ['/tmp', '/var/folders']:
        result = subprocess.run(['find', search_dir, '-name', 'web_dashboard.db', '-type', 'f', '-mmin', '-10'], 
                              capture_output=True, text=True, timeout=5)
        if result.stdout.strip():
            return result.stdout.strip().split('\n')[0]
    return None

def connect_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def print_section(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}\n")

def show_pacing_decisions(conn):
    """Show recent pacing decisions from the log"""
    print_section("Recent Pacing Decisions (Last 10)")
    
    cursor = conn.execute("""
        SELECT ts, requested, approved, action, reason 
        FROM pacing_log 
        ORDER BY ts DESC 
        LIMIT 10
    """)
    
    rows = cursor.fetchall()
    if not rows:
        print("No pacing decisions yet. Campaign still starting up.")
        return
    
    print(f"{'Time':<15} {'Req':>4} {'App':>4} {'Action':<20} {'Reason':<30}")
    print("-" * 90)
    for row in reversed(rows):
        ts = datetime.fromtimestamp(row['ts']).strftime('%H:%M:%S')
        requested = row['requested']
        approved = row['approved']
        action = row['action'][:20]
        reason = row['reason'][:30] if row['reason'] else "—"
        print(f"{ts:<15} {requested:>4} {approved:>4} {action:<20} {reason:<30}")
    
    print(f"\nInterpretation:")
    print(f"  'Req': Pacing engine's request (how many it WANTS to dial)")
    print(f"  'App': Safety controller's approval (how many it ALLOWS)")
    print(f"  'Action': APPROVE (safe), REDUCE (too aggressive), REJECT (no capacity), FALLBACK (unsafe for predictive)")

def show_agent_snapshot(conn):
    """Show current agent state distribution"""
    print_section("Agent State Snapshot (RIGHT NOW)")
    
    cursor = conn.execute("""
        SELECT state, COUNT(*) as count 
        FROM agents 
        GROUP BY state 
        ORDER BY CASE 
            WHEN state = 'AVAILABLE' THEN 1
            WHEN state = 'CONNECTED' THEN 2
            WHEN state = 'WRAP_UP' THEN 3
            WHEN state = 'DIALING' THEN 4
            WHEN state = 'RESERVED' THEN 5
            WHEN state = 'PAUSED' THEN 6
            WHEN state = 'OFFLINE' THEN 7
        END
    """)
    
    rows = cursor.fetchall()
    total = sum(r['count'] for r in rows)
    
    print(f"Total Agents: {total}\n")
    for row in rows:
        state = row['state']
        count = row['count']
        pct = (count / total * 100) if total > 0 else 0
        bar = "█" * int(pct / 5)
        print(f"  {state:<12} : {count:>2} agents {bar:<20} ({pct:>5.1f}%)")
    
    print(f"\nInterpretation:")
    print(f"  AVAILABLE: Ready for next call (should be ~15-25)")
    print(f"  CONNECTED: Actively talking (should be ~5-10)")
    print(f"  WRAP_UP: Post-call work (should be ~3-5)")
    print(f"  DIALING: Waiting for borrower to answer (should be ~0-2)")
    print(f"  RESERVED: Just claimed, ready to dial (should be ~0-1)")
    print(f"  OFFLINE: Crashed/disappeared (should be 0)")

def show_call_snapshot(conn):
    """Show current call state distribution"""
    print_section("Call State Snapshot (RIGHT NOW)")
    
    cursor = conn.execute("""
        SELECT state, COUNT(*) as count 
        FROM calls 
        GROUP BY state 
        ORDER BY count DESC
    """)
    
    rows = cursor.fetchall()
    total = sum(r['count'] for r in rows)
    
    print(f"Total Calls: {total}\n")
    for row in rows:
        state = row['state']
        count = row['count']
        pct = (count / total * 100) if total > 0 else 0
        bar = "█" * max(1, int(pct / 2))
        emoji = {
            'COMPLETED': '✅',
            'ABANDONED': '⚠️ ',
            'FAILED': '❌',
            'CONNECTED': '📞',
            'ANSWERED': '🔔',
            'RINGING': '📳',
        }.get(state, '  ')
        print(f"  {emoji} {state:<12} : {count:>3} {bar:<30} ({pct:>5.1f}%)")
    
    abandoned = next((r['count'] for r in rows if r['state'] == 'ABANDONED'), 0)
    print(f"\n✅ ABANDONED CALLS: {abandoned} (should be 0 - this is the compliance metric)")

def show_safety_decisions(conn):
    """Show safety controller decision distribution"""
    print_section("Safety Controller Decisions (All Time)")
    
    cursor = conn.execute("""
        SELECT action, COUNT(*) as count 
        FROM pacing_log 
        GROUP BY action 
        ORDER BY count DESC
    """)
    
    rows = cursor.fetchall()
    total = sum(r['count'] for r in rows)
    
    print(f"Total decisions: {total}\n")
    
    action_colors = {
        'APPROVE': '🟢',
        'REDUCE': '🟡',
        'REJECT': '🔴',
        'FALLBACK_TO_PROGRESSIVE': '🔴',
    }
    
    for row in rows:
        action = row['action']
        count = row['count']
        pct = (count / total * 100) if total > 0 else 0
        emoji = action_colors.get(action, '  ')
        bar = "█" * max(1, int(pct / 3))
        print(f"  {emoji} {action:<25} : {count:>3} {bar:<30} ({pct:>5.1f}%)")
    
    print(f"\nInterpretation:")
    print(f"  APPROVE: Request within safe capacity → dial as asked")
    print(f"  REDUCE: Request too aggressive → dial less (most common)")
    print(f"  REJECT: No safe capacity → 0 dials (rare, provider issues)")
    print(f"  FALLBACK_TO_PROGRESSIVE: Unsafe for predictive → conservative mode")

def show_workflow_trace(conn):
    """Show a sample workflow: one borrower from queue to completion"""
    print_section("Complete Workflow Trace: One Borrower's Journey")
    
    # Get a completed call
    cursor = conn.execute("""
        SELECT call_id, borrower_id, mode, created_at, updated_at
        FROM calls 
        WHERE state = 'COMPLETED'
        LIMIT 1
    """)
    
    row = cursor.fetchone()
    if not row:
        print("No completed calls yet. Check back in 10 seconds.")
        return
    
    call_id = row['call_id']
    borrower_id = row['borrower_id']
    mode = row['mode']
    
    print(f"Borrower: {borrower_id}")
    print(f"Call ID: {call_id}")
    print(f"Mode: {mode} (Progressive = agent pre-bound, Predictive = agent bound after answer)")
    print(f"Duration: {(row['updated_at'] - row['created_at']):.2f}s\n")
    
    print("Timeline (what happened to this call):\n")
    
    # Get all state transitions for this call
    cursor = conn.execute("""
        SELECT rowid, state, event_key, applied_at 
        FROM call_events 
        WHERE call_id = ? 
        ORDER BY applied_at ASC
    """, (call_id,))
    
    events = cursor.fetchall()
    if not events:
        print("No event history available")
        return
    
    start_time = events[0]['applied_at'] if events else 0
    
    for i, event in enumerate(events, 1):
        elapsed = (event['applied_at'] - start_time) if start_time else 0
        state = event['state']
        print(f"  {i}. [{elapsed:>6.3f}s] → {state}")
    
    print(f"\nKey Points:")
    print(f"  • QUEUED → System claimed borrower from queue")
    print(f"  • INITIATED → Call handed to provider")
    print(f"  • RINGING → Borrower's phone is ringing")
    print(f"  • ANSWERED → Borrower picked up")
    print(f"  • CONNECTED → Agent + Borrower bridged (agent binding happened here if predictive)")
    print(f"  • COMPLETED → Conversation finished")

def main():
    print("\n" + "="*70)
    print("  SmartDialer Backend Workflow Inspector")
    print("="*70)
    
    # Find database
    db_path = find_db_path()
    if not db_path:
        print("\n❌ No active web_dashboard.db found.")
        print("   Make sure the web dashboard is running (http://localhost:9000)")
        return
    
    print(f"\n✅ Found database: {db_path}\n")
    
    try:
        conn = connect_db(db_path)
    except Exception as e:
        print(f"❌ Error connecting to database: {e}")
        return
    
    while True:
        print("\n" + "="*70)
        print("  What would you like to inspect?")
        print("="*70)
        print("\n  1. Recent pacing decisions (shows what Safety Controller approved)")
        print("  2. Agent state snapshot (where are agents right now?)")
        print("  3. Call state snapshot (call distribution)")
        print("  4. Safety controller decisions (all-time stats)")
        print("  5. Complete workflow trace (one borrower's journey)")
        print("  0. Exit")
        print()
        
        choice = input("  Enter choice (0-5): ").strip()
        
        try:
            if choice == '1':
                show_pacing_decisions(conn)
            elif choice == '2':
                show_agent_snapshot(conn)
            elif choice == '3':
                show_call_snapshot(conn)
            elif choice == '4':
                show_safety_decisions(conn)
            elif choice == '5':
                show_workflow_trace(conn)
            elif choice == '0':
                print("\nGoodbye!\n")
                break
            else:
                print("\n❌ Invalid choice. Please enter 0-5.")
        except Exception as e:
            print(f"\n❌ Error: {e}")
        
        input("\nPress Enter to continue...")

if __name__ == "__main__":
    main()
