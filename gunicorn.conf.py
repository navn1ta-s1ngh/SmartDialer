"""
Starts the dashboard's background campaign thread inside each gunicorn
worker specifically, instead of relying on module-import-time code.

Why this file exists: gunicorn always imports the WSGI app once in its
master process to validate it loads. On at least one real deployment
target (confirmed in production logs), the worker process inherited that
already-imported module via fork() rather than re-importing it fresh --
fork() only carries the *calling* thread into the child, so a background
thread started at import time ends up alive only in the master (which
never serves HTTP requests), while the worker's copy of that thread is
marked dead by Python's own post-fork bookkeeping, and any module globals
that thread would have set are frozen at whatever they were the instant
before the fork. The result: a campaign that runs correctly forever, in
a process nothing ever talks to.

`post_fork` runs once inside each worker, after it forks and before it
starts serving requests -- the correct place for exactly this.
"""


def post_fork(server, worker):
    import web_dashboard
    web_dashboard.start_campaign_once()
