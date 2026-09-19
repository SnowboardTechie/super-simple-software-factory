"""Session lifecycle: pin-or-create an adw_id, build the Run object.

`ensure(cfg, adw_id)` joins the session if it exists or creates it under
exactly that id (pinned ids for repeatable runs); omitted, a fresh id is
minted and printed so the next ADW can pick it up.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from .data_types import RunTargets, SSSFConfig
from .runner import Run
from .tracer import Tracer
from .utils import engineer_name, new_id


def _finalize_when_killed(run: Run) -> None:
    """A killed run still closes its own trace.

    Python's default SIGTERM handling exits without unwinding, so `just kill`
    (or any `kill <pid>`) would leave the session reading `running` forever and
    its process rows open — the trace would claim work is in flight that is
    already dead. Turning the signal into SystemExit both finalizes here and
    lets the phase context manager record the phase as failed on the way out.
    """
    def handler(signum, _frame):
        run.tracer.session_finish(run.adw_id, ok=False)   # also closes process rows
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handler)


def ensure(cfg: SSSFConfig, adw_id: str | None = None,
           targets: RunTargets | None = None) -> Run:
    adw_id = adw_id or new_id(8)
    targets = targets or RunTargets.resolve(cfg)
    # Both sinks hang off the run's STATE root, and `trace_db` is what keeps the
    # db beside the sessions it indexes — the visualizer derives `sessions/`
    # from the db's own directory, so separating the two points the UI at
    # nothing. Raises rather than silently escaping if the config disagrees.
    tracer = Tracer(targets.trace_db(cfg), targets.events_jsonl(adw_id))
    run = Run(cfg=cfg, adw_id=adw_id, tracer=tracer, engineer=engineer_name(),
              targets=targets)
    tracer.session_start(adw_id, run.engineer, adw_name=Path(sys.argv[0]).stem)
    # This process is the run. Record it before any phase opens, so a run that
    # hangs in its first agent call is still killable by adw_id.
    tracer.process_start(adw_id, "adw", "", os.getpid(),
                         " ".join([Path(sys.argv[0]).name, *sys.argv[1:]]))
    _finalize_when_killed(run)
    run.console.session_started(adw_id, run.engineer)
    return run
