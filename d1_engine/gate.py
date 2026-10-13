"""Let D1 step aside while another stage needs the GPU for a short, latency-critical burst.

D1 keeps the GPU saturated with long int8-GEMM / attention kernels for ~2s a photo. D0b,
run alongside it in the pipelined worker, is a chain of thousands of short dependent
kernels; even on a high-priority stream each one waits for an SM behind D1's blocks, and
D0b took 1.1s a photo instead of the 0.35s it takes on an idle GPU. Measured with D1 alone
as the load: D0b 0.45s -> 1.14s, the GIL not involved (a probe thread re-took it within
0.13ms at p90), D3 alone barely mattered (0.45s -> 0.53s).

While hold() is active anywhere in the process, D1 stops issuing work at its next
transformer-block boundary and resumes when the last holder leaves. Pacer keeps D1 at most
LOOKAHEAD blocks ahead of the GPU at all times, so when a holder arrives only those few
blocks (~15ms of work) are still queued. Only scheduling changes: D1 computes exactly the
same numbers, and on its own it still never lets the GPU run dry (a block takes ~7ms on the
GPU and ~0.2ms to issue).
"""
import threading
from contextlib import contextmanager

import torch

LOOKAHEAD = 2

_cond = threading.Condition()
_holders = 0


@contextmanager
def hold():
    """D1 pauses at its next block boundary for as long as this is active."""
    global _holders
    with _cond:
        _holders += 1
    try:
        yield
    finally:
        with _cond:
            _holders -= 1
            if _holders == 0:
                _cond.notify_all()


def wait_open():
    """Return once nobody holds the gate (immediately in the common case)."""
    if _holders:
        with _cond:
            while _holders:
                _cond.wait()


class Pacer:
    """Call tick() before issuing each unit of GPU work (a transformer block): it waits until
    the GPU has caught up to within LOOKAHEAD units, then for the gate to be open."""

    def __init__(self):
        self._events = []

    def tick(self):
        if not torch.cuda.is_available():
            return
        if len(self._events) > LOOKAHEAD:
            self._events.pop(0).synchronize()           # releases the GIL while it waits
        wait_open()
        ev = torch.cuda.Event()
        ev.record(torch.cuda.current_stream())
        self._events.append(ev)
