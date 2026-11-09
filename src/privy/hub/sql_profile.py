"""Opt-in, payload-free timings for SQL hub startup and cold reads."""

import logging
import os
import threading
import time
from contextlib import contextmanager

log = logging.getLogger("privy.hub")


class StartupProfile:
    def __init__(self, phase, *, enabled=None):
        self.enabled = os.environ.get("PRIVY_SQL_PROFILE") == "1" if enabled is None else enabled
        self.phase = phase
        self.counts = {}
        self.timings = {}

    def __enter__(self):
        if self.enabled:
            self.started = time.perf_counter()
            log.info(
                "startup_profile phase=%s event=start thread=%s mono_s=%.6f",
                self.phase,
                threading.get_ident(),
                self.started,
            )
        return self

    def __exit__(self, kind, error, traceback):
        if self.enabled:
            ended = time.perf_counter()
            counts = " ".join(f"{name}={value}" for name, value in sorted(self.counts.items()))
            timings = " ".join(
                f"{name}_count={count} {name}_total_ms={total:.3f} {name}_max_ms={maximum:.3f}"
                for name, (count, total, maximum) in sorted(self.timings.items())
            )
            log.info(
                "startup_profile phase=%s event=end thread=%s mono_s=%.6f duration_ms=%.3f "
                "outcome=%s error_type=%s %s %s",
                self.phase,
                threading.get_ident(),
                ended,
                (ended - self.started) * 1000,
                "error" if kind else "ok",
                kind.__name__ if kind else "none",
                counts,
                timings,
            )

    def count(self, name, value=1):
        if self.enabled:
            self.counts[name] = self.counts.get(name, 0) + value

    @contextmanager
    def stage(self, name):
        if not self.enabled:
            yield
            return
        started = time.perf_counter()
        log.info(
            "startup_profile phase=%s stage=%s event=start thread=%s mono_s=%.6f",
            self.phase,
            name,
            threading.get_ident(),
            started,
        )
        outcome, error_type = "ok", "none"
        try:
            yield
        except BaseException as exc:
            outcome, error_type = "error", type(exc).__name__
            raise
        finally:
            ended = time.perf_counter()
            duration = (ended - started) * 1000
            count, total, maximum = self.timings.get(name, (0, 0.0, 0.0))
            self.timings[name] = [count + 1, total + duration, max(maximum, duration)]
            log.info(
                "startup_profile phase=%s stage=%s event=end thread=%s mono_s=%.6f "
                "duration_ms=%.3f outcome=%s error_type=%s",
                self.phase,
                name,
                threading.get_ident(),
                ended,
                duration,
                outcome,
                error_type,
            )
