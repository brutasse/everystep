class EverystepError(Exception):
    """Base class for everystep errors."""


class SimulatedCrash(EverystepError):
    """Raised from a test fault handler to simulate a process death mid-step.

    The runner re-raises it without writing any further state, leaving the
    workflow exactly as it would be if the worker had died.
    """


class StepFailure(EverystepError):
    """A recorded step failure whose original exception type is unavailable."""


class WorkflowCodeError(EverystepError):
    """A workflow body diverged from its previously recorded step identities."""


class DrainOrphan(EverystepError):
    """Raised at a step boundary when the runner is draining after a stop signal.

    The step in flight at the signal finishes and is recorded; no new step
    starts. The worker requeues the workflow so any runner can claim it and
    resume it from the recorded steps.
    """


class Terminal(EverystepError):
    """Raised from a step to stop the workflow for a known reason.

    The step in flight finishes and is recorded; no new step starts and the
    engine will not retry the workflow. It ends in the `stopped` status with
    the reason and payload recorded on it, so the caller can take over (for
    example, re-scheduling with a different resource). Not a failure: it is
    never reported to Sentry.
    """

    def __init__(self, reason, payload=None):
        self.reason = reason
        self.payload = payload
        super().__init__(reason, payload)

    def __str__(self):
        return str(self.reason)
