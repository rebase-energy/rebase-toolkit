"""What a running workflow knows about itself.

The runner injects the ids of the run, and of the step inside it, into the
container's environment. That is the only thing connecting a `Function.map`
issued from inside a step back to the step that issued it: without it the
platform records a batch belonging to nothing and the run has no tasks to list.

Reading the environment on each call rather than caching it is deliberate — the
steps of one run share a process, so the step id changes underneath user code
between steps and a value captured at import would name the wrong one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Set by the runner around a whole run, and around each step of it.
RUN_ID_ENV = "REBASE_RUN_ID"
STEP_RUN_ID_ENV = "REBASE_STEP_RUN_ID"


@dataclass(frozen=True)
class RunContext:
    """The run, and the step of it, that this code is executing inside."""

    run_id: str
    step_run_id: str | None = None


def current_run() -> RunContext | None:
    """The run this code is part of, or None when it is not running on the platform.

    None is the ordinary answer on a laptop: a `Function.map` from a shell belongs
    to no run, and the platform accepts an unattributed batch for exactly that case.
    """
    run_id = os.environ.get(RUN_ID_ENV)
    if not run_id:
        return None
    step_run_id = os.environ.get(STEP_RUN_ID_ENV)
    return RunContext(run_id=run_id, step_run_id=step_run_id or None)
