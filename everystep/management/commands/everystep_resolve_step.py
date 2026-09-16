"""Resolve a run blocked on an unsafe-to-repeat step.

A step marked ``unsafe_to_repeat=True`` that the worker started but could not
record (it died in the effect window) leaves the run in the ``blocked`` status
with a ``started`` step row. The effect may or may not have happened; only a
human who checked the external system can say. This command records that
decision and puts the run back in the queue:

- ``--result <json>``  the effect happened; store ``<json>`` as the step result.
- ``--error <message>`` the effect happened and failed; record a failure so the
  workflow's durable cleanup (``try/except``) runs on the next pass.
- ``--discard``        the effect did not happen; drop the started row so the
  step re-runs on the next claim.

Nothing here re-runs the step's side effect on its own; ``--discard`` is the
only option that lets the step execute again, and only because the human
asserted the effect never happened.
"""

import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from everystep import serde
from everystep.errors import StepFailure
from everystep.models import Step, Workflow


class Command(BaseCommand):
    help = (
        "Resolve a run blocked on an unsafe-to-repeat step: record the effect's "
        "result or failure, or discard the unrecorded attempt so the step re-runs."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "run_id",
            help="UUID of the blocked workflow run.",
        )
        parser.add_argument(
            "step_id",
            help="The dotpath step id of the started (unrecorded) step.",
        )
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            "--result",
            metavar="JSON",
            help="the effect happened; store this JSON value as the step result.",
        )
        group.add_argument(
            "--error",
            metavar="MESSAGE",
            help="the effect happened and failed; record this failure on the step.",
        )
        group.add_argument(
            "--discard",
            action="store_true",
            help="the effect did not happen; delete the started row so the step re-runs.",
        )

    def handle(self, run_id, step_id, result, error, discard, **options):
        try:
            run = Workflow.objects.get(id=run_id)
        except (Workflow.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
            raise CommandError(f"no workflow run with id {run_id!r}") from exc
        if run.status != Workflow.Status.BLOCKED:
            raise CommandError(
                f"run {run_id} is {run.status!r}, not blocked; only blocked runs "
                "can be resolved"
            )
        try:
            step = Step.objects.get(workflow=run, step_id=step_id)
        except Step.DoesNotExist as exc:
            raise CommandError(
                f"run {run_id} has no recorded step {step_id!r}"
            ) from exc
        if step.status != Step.Status.STARTED:
            raise CommandError(
                f"step {step_id!r} is {step.status!r}, not started; only a started "
                "step can be resolved"
            )

        with transaction.atomic():
            if result is not None:
                try:
                    value = json.loads(result)
                except json.JSONDecodeError as exc:
                    raise CommandError(f"--result is not valid JSON: {exc}") from exc
                try:
                    serde.dumps(value)
                except (TypeError, ValueError) as exc:
                    raise CommandError(f"--result is not storable: {exc}") from exc
                step.status = Step.Status.DONE
                step.result = value
                step.error = None
                step.save()
                note = "recorded the effect's result"
            elif error is not None:
                step.status = Step.Status.FAILED
                step.result = None
                step.error = serde.encode_exception(StepFailure(error))
                step.save()
                note = "recorded the effect's failure"
            else:
                step.delete()
                note = "discarded the unrecorded attempt"

            # The run is no longer waiting on a human: put it back in the queue
            # so any runner can claim it and resume from the recorded steps.
            run.status = Workflow.Status.SCHEDULED
            run.claimed_by = None
            run.error = None
            run.completed_at = None
            run.save()

        self.stdout.write(
            self.style.SUCCESS(
                f"run {run_id}: {note} for step {step_id!r}; run requeued as scheduled"
            )
        )
