#!/usr/bin/env python3
"""
Live terminal dashboard for the SmartDialer pacing/safety workflow.

Runs a real Campaign (same Pacing Engine / Safety Controller / Allocator /
Provider used by simulate.py and load_test.py -- nothing here is a fake
or a replay) and redraws the whole pipeline, agent/call state machines,
rolling EWMA stats, and the live Safety Controller decision feed several
times a second, in the terminal, using nothing but the stdlib `curses`
module. No server, no browser, no external services -- same "runs
anywhere with just Python" philosophy as the rest of this prototype.

Interactive keys let you inject the same failure conditions Scenario D
injects on a fixed schedule, on demand, so you can *watch* the Safety
Controller react in real time instead of reading about it afterward:

    d   force 15 agents offline right now (sudden availability drop)
    o   toggle a simulated provider outage
    c   toggle an answer-rate crash (drops to 8%)
    q   quit

Usage:
    python3 dashboard.py                  # predictive mode, 25 agents
    python3 dashboard.py --agents 60 --answer-rate 0.2 --mode progressive
"""
from __future__ import annotations
import argparse
import curses
import locale
import os
import time
from collections import deque

from smartdialer.campaign import Campaign
from smartdialer.providers import make_custom_provider
from smartdialer.models import DialMode

AGENT_STATES = ["OFFLINE", "AVAILABLE", "RESERVED", "DIALING", "CONNECTED", "WRAP_UP", "PAUSED"]
CALL_STATES = ["QUEUED", "RESERVED", "INITIATED", "RINGING", "ANSWERED", "CONNECTED",
               "COMPLETED", "FAILED", "CANCELLED", "ABANDONED"]

SPARK_CHARS = " ▁▂▃▄▅▆▇█"


def setup_colors():
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    curses.init_pair(1, curses.COLOR_GREEN, bg)
    curses.init_pair(2, curses.COLOR_YELLOW, bg)
    curses.init_pair(3, curses.COLOR_RED, bg)
    curses.init_pair(4, curses.COLOR_CYAN, bg)
    curses.init_pair(5, curses.COLOR_MAGENTA, bg)
    curses.init_pair(6, curses.COLOR_BLUE, bg)
    curses.init_pair(7, curses.COLOR_WHITE, bg)


def safe_addstr(win, y, x, s, attr=0):
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w or not s:
        return
    s = s[: max(0, w - x - 1)]
    if not s:
        return
    try:
        win.addstr(y, x, s, attr)
    except curses.error:
        pass


def sparkline(values, width: int) -> str:
    vals = list(values)[-width:]
    chars = []
    for v in vals:
        v = 0.0 if v is None else max(0.0, min(1.0, v))
        idx = int(round(v * (len(SPARK_CHARS) - 1)))
        chars.append(SPARK_CHARS[idx])
    line = "".join(chars)
    return line.rjust(width)


def bar(count: int, total: int, width: int) -> str:
    if total <= 0 or count <= 0:
        return ""
    n = max(1, round((count / total) * width)) if count > 0 else 0
    return "█" * min(n, width)


def action_color(action: str) -> int:
    return {
        "APPROVE": curses.color_pair(1),
        "REDUCE": curses.color_pair(2),
        "REJECT": curses.color_pair(3) | curses.A_BOLD,
        "FALLBACK_TO_PROGRESSIVE": curses.color_pair(3) | curses.A_BOLD,
    }.get(action, 0)


def agent_state_color(state: str) -> int:
    return {
        "OFFLINE": curses.color_pair(3),
        "AVAILABLE": curses.color_pair(1),
        "RESERVED": curses.color_pair(2),
        "DIALING": curses.color_pair(2),
        "CONNECTED": curses.color_pair(4),
        "WRAP_UP": curses.color_pair(5),
        "PAUSED": curses.color_pair(7),
    }.get(state, 0)


def call_state_color(state: str) -> int:
    return {
        "QUEUED": curses.color_pair(7),
        "RESERVED": curses.color_pair(2),
        "INITIATED": curses.color_pair(2),
        "RINGING": curses.color_pair(2),
        "ANSWERED": curses.color_pair(4),
        "CONNECTED": curses.color_pair(1),
        "COMPLETED": curses.color_pair(1),
        "FAILED": curses.color_pair(3),
        "CANCELLED": curses.color_pair(7),
        "ABANDONED": curses.color_pair(3) | curses.A_BOLD,
    }.get(state, 0)


def draw(stdscr, campaign, report, history, start_time, outage_on, crash_on,
         mode_label, provider_name):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    if h < 24 or w < 90:
        safe_addstr(stdscr, 0, 0, "Resize terminal to at least 90x24 to view the dashboard "
                                   f"(currently {w}x{h}).")
        stdscr.refresh()
        return

    elapsed = time.time() - start_time
    title = (f" SMARTDIALER — LIVE WORKFLOW DASHBOARD   mode={mode_label}  "
             f"provider={provider_name}  borrowers_left={report['queued_borrowers']}  "
             f"t={elapsed:6.1f}s ")
    safe_addstr(stdscr, 0, 0, title.ljust(w), curses.color_pair(4) | curses.A_BOLD | curses.A_REVERSE)
    y = 2

    # -- pipeline: Campaign -> Pacing -> Safety -> Allocator -> Provider --
    log_tail = campaign.recent_pacing_log(1)
    last = log_tail[-1] if log_tail else None
    ac = report["agent_counts"]
    total_agents = sum(ac.values())
    stages = [
        ("Campaign", f"avail {ac.get('AVAILABLE', 0)}/{total_agents}", 0),
        ("Pacing Engine", (f"req={last['requested']}" if last else "req=-"), 0),
        ("Safety Ctrl", (last["action"] if last else "-"),
         action_color(last["action"]) if last else 0),
        ("Allocator", (f"dialed={last['approved']}" if last else "dialed=-"), 0),
        ("Provider", f"health={report['provider_health']:.2f}" + (" [OUTAGE]" if outage_on else ""),
         curses.color_pair(3) | curses.A_BOLD if (outage_on or report["circuit_open"])
         else curses.color_pair(1)),
    ]
    cellw = max(15, (w - 6) // len(stages))
    sep = " ──▶ "
    line1 = ""
    for i, (label, _, _) in enumerate(stages):
        line1 += label.center(cellw) + (sep if i < len(stages) - 1 else "")
    safe_addstr(stdscr, y, 0, line1, curses.A_BOLD)
    y += 1
    x = 0
    for i, (_, val, color) in enumerate(stages):
        safe_addstr(stdscr, y, x, val.center(cellw), color)
        x += cellw + len(sep)
    y += 2

    if last:
        safe_addstr(stdscr, y, 0, ("reason: " + last["reason"])[: w - 1], curses.A_DIM)
    y += 2

    # -- agent / call state panels, side by side --
    left_w = w // 2 - 2
    right_x = w // 2 + 1
    safe_addstr(stdscr, y, 0, "AGENT STATES", curses.color_pair(4) | curses.A_BOLD)
    safe_addstr(stdscr, y, right_x, "CALL STATES", curses.color_pair(4) | curses.A_BOLD)
    y += 1
    cc = report["call_counts"]
    total_calls = sum(cc.values())
    bar_w = max(6, left_w - 22)
    rows = max(len(AGENT_STATES), len(CALL_STATES))
    for i in range(rows):
        ry = y + i
        if i < len(AGENT_STATES):
            st = AGENT_STATES[i]
            cnt = ac.get(st, 0)
            line = f"{st:<10} {cnt:>5}  {bar(cnt, total_agents, bar_w)}"
            safe_addstr(stdscr, ry, 0, line[:left_w], agent_state_color(st))
        if i < len(CALL_STATES):
            st = CALL_STATES[i]
            cnt = cc.get(st, 0)
            marker = "  <-- compliance risk" if st == "ABANDONED" else ""
            line = f"{st:<10} {cnt:>5}  {bar(cnt, total_calls, bar_w)}{marker}"
            safe_addstr(stdscr, ry, right_x, line[: w - right_x - 1], call_state_color(st))
    y += rows + 2

    # -- rolling stats --
    safe_addstr(stdscr, y, 0, "ROLLING STATS (EWMA)", curses.color_pair(4) | curses.A_BOLD)
    y += 1
    spark_w = max(20, w - 42)
    stats = report["stats"]
    safe_addstr(stdscr, y, 0,
                f"answer rate      {stats['answer_rate']:.3f}  {sparkline(history['answer_rate'], spark_w)}",
                curses.color_pair(1))
    y += 1
    ab_color = curses.color_pair(3) | curses.A_BOLD if stats["abandon_rate"] > 0.03 else curses.color_pair(1)
    safe_addstr(stdscr, y, 0,
                f"abandon rate     {stats['abandon_rate']:.3f}  {sparkline(history['abandon_rate'], spark_w)}",
                ab_color)
    y += 1
    ph_color = curses.color_pair(3) | curses.A_BOLD if report["provider_health"] < 0.55 else curses.color_pair(1)
    safe_addstr(stdscr, y, 0,
                f"provider health  {report['provider_health']:.3f}  {sparkline(history['provider_health'], spark_w)}",
                ph_color)
    y += 2

    # -- live safety controller decision feed --
    safe_addstr(stdscr, y, 0, "SAFETY CONTROLLER DECISION FEED (live)", curses.color_pair(4) | curses.A_BOLD)
    y += 1
    feed_rows = max(1, h - y - 4)
    feed = campaign.recent_pacing_log(feed_rows)
    for i, entry in enumerate(feed):
        t = entry["ts"] - start_time
        line = (f"t+{t:7.1f}s  req={entry['requested']:>4} approved={entry['approved']:>4}  "
                f"{entry['action']:<24} {entry['reason']}")
        safe_addstr(stdscr, y + i, 0, line[: w - 1], action_color(entry["action"]))
    y += feed_rows + 1

    # -- footer --
    counters = report["counters"]
    summary = (f"initiated={counters.get('calls_initiated', 0)} "
               f"answered={counters.get('calls_answered', 0)} "
               f"completed={cc.get('COMPLETED', 0)} "
               f"failed={counters.get('calls_failed', 0)} "
               f"abandoned={counters.get('calls_abandoned', 0)}")
    safe_addstr(stdscr, y, 0, summary[: w - 1], curses.A_BOLD)
    y += 1
    toggles = f"[outage: {'ON' if outage_on else 'off'}]  [answer-rate crash: {'ON' if crash_on else 'off'}]"
    safe_addstr(stdscr, y, 0, toggles[: w - 1], curses.color_pair(3) if (outage_on or crash_on) else 0)
    y += 1
    keys = "keys:  [d] drop 15 agents   [o] toggle provider outage   [c] toggle answer-rate crash   [q] quit"
    safe_addstr(stdscr, min(y, h - 1), 0, keys[: w - 1], curses.A_DIM)

    stdscr.refresh()


def run_dashboard(stdscr, args):
    for suffix in ("", "-wal", "-shm"):
        p = args.db + suffix
        if os.path.exists(p):
            os.remove(p)

    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.nodelay(True)
    setup_colors()

    provider = make_custom_provider(
        "Dashboard-Provider", time_scale=args.time_scale, seed=args.seed,
        answer_rate=args.answer_rate, avg_talk_time=args.avg_talk_time,
        base_failure_rate=0.05, duplicate_rate=0.03, reorder_rate=0.03,
    )
    original_answer_rate = provider.base_answer_rate

    mode = DialMode.PREDICTIVE if args.mode == "predictive" else DialMode.PROGRESSIVE
    campaign = Campaign("dashboard", args.db, mode, provider,
                         tick_interval=args.tick_interval, num_event_workers=6)
    campaign.seed(num_agents=args.agents, num_borrowers=args.borrowers)
    campaign.start()
    start_time = time.time()

    history = {k: deque(maxlen=300) for k in ("answer_rate", "abandon_rate", "provider_health")}
    outage_on = False
    crash_on = False

    try:
        while True:
            frame_start = time.time()

            key = stdscr.getch()
            while key != -1:
                if key in (ord("q"), ord("Q"), 27):
                    return
                elif key in (ord("d"), ord("D")):
                    campaign.simulate_agents_disappear(15)
                elif key in (ord("o"), ord("O")):
                    outage_on = not outage_on
                    provider.set_outage(outage_on)
                elif key in (ord("c"), ord("C")):
                    crash_on = not crash_on
                    provider.base_answer_rate = 0.08 if crash_on else original_answer_rate
                key = stdscr.getch()

            report = campaign.report()
            history["answer_rate"].append(report["stats"]["answer_rate"])
            history["abandon_rate"].append(report["stats"]["abandon_rate"])
            history["provider_health"].append(report["provider_health"])

            draw(stdscr, campaign, report, history, start_time, outage_on, crash_on,
                 args.mode.upper(), provider.name)

            frame_elapsed = time.time() - frame_start
            time.sleep(max(0.02, 0.15 - frame_elapsed))
    finally:
        campaign.stop(drain_seconds=0.2)


def main():
    ap = argparse.ArgumentParser(
        description="Live terminal dashboard for the SmartDialer pacing/safety workflow")
    ap.add_argument("--agents", type=int, default=25)
    ap.add_argument("--borrowers", type=int, default=20000)
    ap.add_argument("--answer-rate", type=float, default=0.35)
    ap.add_argument("--avg-talk-time", type=float, default=90.0)
    ap.add_argument("--tick-interval", type=float, default=0.12)
    ap.add_argument("--time-scale", type=float, default=0.02)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--db", default="/tmp/sim_dashboard.db")
    ap.add_argument("--mode", choices=["predictive", "progressive"], default="predictive")
    args = ap.parse_args()

    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(run_dashboard, args)


if __name__ == "__main__":
    main()
