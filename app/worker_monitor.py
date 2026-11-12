from __future__ import annotations

from contextlib import contextmanager
import sys
from threading import get_ident, Timer
from time import monotonic
import traceback

from flask import current_app


SLOW_STEP_SECONDS = 120


@contextmanager
def worker_step(name):
    logger = current_app.logger
    thread_id = get_ident()
    started_at = monotonic()

    def report_slow_step():
        frame = sys._current_frames().get(thread_id)
        stack = "".join(traceback.format_stack(frame)) if frame is not None else ""
        logger.error(
            "worker_step_slow step=%s elapsed_seconds=%.1f stack=%s",
            name, monotonic() - started_at, stack,
        )

    timer = Timer(SLOW_STEP_SECONDS, report_slow_step)
    timer.daemon = True
    timer.start()
    try:
        yield
    except Exception:
        logger.exception("worker_step_failed step=%s", name)
        raise
    finally:
        timer.cancel()
