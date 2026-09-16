import json
import os
import time
import uuid as uuid_lib

from django.db import models

from everystep import serde

try:  # Python 3.14+
    uuid7 = uuid_lib.uuid7
except AttributeError:
    # Drop once requires-python is >=3.14 (Python 3.13 EOL Oct 2029).

    def uuid7() -> uuid_lib.UUID:
        ts_ms = int(time.time_ns() // 1_000_000) & 0xFFFFFFFFFFFF
        raw = bytearray(ts_ms.to_bytes(6, "big") + os.urandom(10))
        raw[6] = (raw[6] & 0x0F) | 0x70
        raw[8] = (raw[8] & 0x3F) | 0x80
        return uuid_lib.UUID(bytes=bytes(raw))


class EverystepJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        return serde.json_default(obj)


class EverystepJSONDecoder(json.JSONDecoder):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("object_hook", serde.json_object_hook)
        super().__init__(*args, **kwargs)


class EverystepJSONField(models.JSONField):
    """JSONField that (de)serializes with everystep's extended types."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("encoder", EverystepJSONEncoder)
        kwargs.setdefault("decoder", EverystepJSONDecoder)
        super().__init__(*args, **kwargs)


class Workflow(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled"
        RUNNING = "running"
        COMPLETED = "completed"
        FAILED = "failed"
        STOPPED = "stopped"
        BLOCKED = "blocked"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=300)
    args = EverystepJSONField(default=list)
    idempotency_key = models.CharField(max_length=255, null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.SCHEDULED, db_index=True
    )
    claimed_by = models.CharField(max_length=255, null=True, blank=True)
    result = EverystepJSONField(null=True, blank=True)
    error = EverystepJSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["name", "idempotency_key"],
                condition=models.Q(idempotency_key__isnull=False),
                name="unique_workflow_per_idempotency_key",
            ),
        ]

    def __str__(self):
        return f"Workflow<{self.name} {self.id} {self.status}>"


class Step(models.Model):
    class Status(models.TextChoices):
        DONE = "done"
        FAILED = "failed"
        STARTED = "started"

    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="steps")
    step_id = models.CharField(max_length=300)
    name = models.CharField(max_length=300)
    args = EverystepJSONField(default=list)
    kwargs = EverystepJSONField(default=dict)
    status = models.CharField(max_length=16, choices=Status.choices)
    result = EverystepJSONField(null=True, blank=True)
    error = EverystepJSONField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workflow", "step_id"], name="unique_step_per_workflow"),
        ]
