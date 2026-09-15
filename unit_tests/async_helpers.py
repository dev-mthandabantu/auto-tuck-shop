"""Shared helper for driving async code from sync tests.

Every test module here used an identical local copy of this, and all of them leaked a
Postgres connection per call.

Why: the helper builds a new event loop for each invocation, and asgiref binds its
thread_sensitive executor to the running loop. The executor is a single reused worker
thread, and the Django connection it opens lives in that thread. Django's `connections`
is thread-critical storage, so connections.close_all() called from the main thread (or
from any other thread) cannot see it. The Postgres session stays open and DROP DATABASE
fails at teardown with "database is being accessed by other users".

The fix is to close connections inside the same thread_sensitive executor that opened them,
while the loop is still alive.
"""

import asyncio

from asgiref.sync import sync_to_async
from django.db import connections


def run_async_test(coro):
    """Run `coro` on a dedicated event loop, closing DB connections it opened."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(sync_to_async(connections.close_all, thread_sensitive=True)())
        finally:
            loop.close()
            connections.close_all()
