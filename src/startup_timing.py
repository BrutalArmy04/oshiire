"""Lightweight startup phase timing.

Each mark() records a startup phase and prints one line to stdout -- which, under
pythonw (the desktop launch), app.py has redirected to data/oshiire.log, so the
breakdown lands in the log where a slow start can be diagnosed after the fact. On
a console (`python src/review.py`) it just prints there.

Pure stdlib, no gradio: it is imported FIRST in review.py so the clock (_t0)
starts before the heavy imports it measures. Instrumentation only -- a logging
failure never propagates, and the numbers are wall-clock monotonic seconds.

    [startup] imports        +1.84s  (t+ 1.84s)
    [startup] manifest load  +0.02s  (t+ 1.86s)
    ...
"""
import time

_t0 = time.monotonic()
_last = _t0
_phases = []  # list of (phase, delta_s, total_s)


def reset(t0=None):
    """Restart the clock and clear recorded phases (a fresh start, or a test)."""
    global _t0, _last, _phases
    _t0 = time.monotonic() if t0 is None else t0
    _last = _t0
    _phases = []


def mark(phase, log=print):
    """Record `phase` with the time since the previous mark and since t0, log one
    line, and return (delta, total). Never raises -- timing must not break
    startup."""
    global _last
    now = time.monotonic()
    delta, total = now - _last, now - _t0
    _last = now
    _phases.append((phase, delta, total))
    try:
        log(f"[startup] {phase:<13} +{delta:6.2f}s  (t+{total:6.2f}s)")
    except Exception:
        pass
    return delta, total


def phases():
    """The recorded phases so far, as a list of (phase, delta_s, total_s)."""
    return list(_phases)


def format_breakdown():
    """A multi-line summary of every recorded phase plus the total, for the end
    of a startup."""
    if not _phases:
        return "[startup] no phases recorded"
    width = max(len(p) for p, _, _ in _phases)
    lines = [f"  {p:<{width}}  {d:6.2f}s" for p, d, _ in _phases]
    lines.append(f"  {'TOTAL':<{width}}  {_phases[-1][2]:6.2f}s")
    return "[startup] breakdown:\n" + "\n".join(lines)
