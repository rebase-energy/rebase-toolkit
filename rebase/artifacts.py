"""Run-scoped registration of durable output pointers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from rebase.client import Bucket, Client, RebaseWorkflowError
from rebase.runtime import _current_reporting_context, current_run
from rebase.tasks import _current_task_id

ArtifactDisposition = Literal["created", "reused"]


class ArtifactReportingError(RebaseWorkflowError):
    """The platform could not register a requested artifact."""


@dataclass
class Artifact:
    """A durable output pointer registered against the current hosted run."""

    name: str
    uri: str | None = None
    bucket: Bucket | str | None = None
    object_key: str | None = None
    key: str | None = None
    disposition: ArtifactDisposition = "created"
    media_type: str | None = None
    size_bytes: int | None = None
    version: str | None = None
    digest: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    client_token: str = field(default_factory=lambda: str(uuid4()))
    id: str | None = None

    def __post_init__(self) -> None:
        self.name = self._required_string(self.name, field_name="name")
        self.uri = self._optional_string(self.uri, field_name="uri")
        bucket_name = self.bucket.name if isinstance(self.bucket, Bucket) else self.bucket
        self.bucket = self._optional_string(bucket_name, field_name="bucket")
        self.object_key = self._optional_string(self.object_key, field_name="object_key")
        if self.uri is not None and not urlsplit(self.uri).scheme:
            raise ValueError("artifact uri must be an absolute URI with a scheme")
        if self.uri is not None and (self.bucket is not None or self.object_key is not None):
            raise ValueError("artifact accepts either uri or bucket plus object_key, not both")
        if self.uri is None and (self.bucket is None or self.object_key is None):
            raise ValueError("artifact requires uri or bucket plus object_key")
        self.key = self._optional_string(self.key, field_name="key")
        self.media_type = self._optional_string(self.media_type, field_name="media_type")
        self.version = self._optional_string(self.version, field_name="version")
        self.digest = self._optional_string(self.digest, field_name="digest")
        if self.disposition not in {"created", "reused"}:
            raise ValueError("artifact disposition must be 'created' or 'reused'")
        if self.size_bytes is not None and (
            not isinstance(self.size_bytes, int) or isinstance(self.size_bytes, bool) or self.size_bytes < 0
        ):
            raise ValueError("artifact size_bytes must be a non-negative integer or None")
        self.metadata = dict(self.metadata)
        try:
            json.dumps(self.metadata)
        except (TypeError, ValueError) as exc:
            raise TypeError("artifact metadata must be JSON-serializable") from exc

    @staticmethod
    def _required_string(value: str, *, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"artifact {field_name} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _optional_string(value: str | None, *, field_name: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"artifact {field_name} must be a non-empty string or None")
        return value.strip()

    def _register(self) -> None:
        run = current_run()
        if run is None:
            return
        reporting = _current_reporting_context()
        if reporting is None:
            raise ArtifactReportingError(f"run {run.run_id} has no artifact-reporting credential")
        payload: dict[str, Any] = {
            "name": self.name,
            "uri": self.uri,
            "key": self.key,
            "disposition": self.disposition,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "version": self.version,
            "digest": self.digest,
            "metadata": self.metadata,
            "client_token": self.client_token,
        }
        if self.bucket is not None:
            payload["bucket"] = self.bucket
            payload["object_key"] = self.object_key
        if run.step_run_id is not None:
            payload["step_run_id"] = run.step_run_id
        task_id = _current_task_id() or run.task_id
        if task_id is not None:
            payload["task_id"] = task_id
        try:
            created = Client(api_key=reporting.api_key, api_url=reporting.api_url).create_run_artifact(
                run.run_id,
                payload,
            )
        except Exception as exc:
            raise ArtifactReportingError(f"could not register artifact {self.name!r}: {exc}") from exc
        self.id = str(created["id"])


def artifact(
    name: str,
    *,
    uri: str | None = None,
    bucket: Bucket | str | None = None,
    object_key: str | None = None,
    key: str | None = None,
    disposition: ArtifactDisposition = "created",
    media_type: str | None = None,
    size_bytes: int | None = None,
    version: str | None = None,
    digest: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Artifact:
    """Register an external output URI against the current hosted run."""
    value = Artifact(
        name=name,
        uri=uri,
        bucket=bucket,
        object_key=object_key,
        key=key,
        disposition=disposition,
        media_type=media_type,
        size_bytes=size_bytes,
        version=version,
        digest=digest,
        metadata=dict(metadata or {}),
    )
    value._register()
    return value
