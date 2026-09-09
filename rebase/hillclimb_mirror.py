"""A local mirror of a hosted hillclimb search, fed by the platform API.

`rebase hillclimb watch <run-id>` (and chart / tree / graph) runs the engine's
own TUI on a copy of the search's records. The copy lives under
``~/.cache/rebase/hillclimb/<sync-id>/hillclimb/`` laid out exactly like a
hillclimb dir (``config.yaml`` marker + ``runs/<run>/...``), so the engine's
loaders and viewers need no changes: `HILLCLIMB_DIR` points at it, and
`HILLCLIMB_REMOTE_STATE=1` tells the engine that the pids in the records
belong to another machine (liveness then rests on the heartbeat).

Two threads keep it live while a viewer is open:

* the puller lists the run's objects through the API every ``poll_s`` seconds
  and downloads the ones whose generation changed, writing each atomically so
  a viewer never reads half a status.json;
* the pusher watches the mirror's ``control/`` dirs for the command files the
  TUI writes (stop / prune) and forwards them to the API, which queues them
  for the job; the local file is removed as the engine's own ``drain_commands``
  would, so the TUI does not see them as still pending.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .client import RebaseWorkflowError

MIRROR_CONFIG = "# mirror of a hosted hillclimb search; see rebase.hillclimb_mirror\n"
CONTROL_DIR = "control"
MANIFEST_NAME = "hosted.json"
REMOTE_STATE_ENV = "HILLCLIMB_REMOTE_STATE"


def default_mirror_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "rebase" / "hillclimb"


class Mirror:
    """Mirror one hosted run's synced state into a local hillclimb dir."""

    def __init__(
        self,
        client: Any,
        run_id: str,
        *,
        sync_id: str | None = None,
        root: Path | None = None,
        poll_s: float = 10.0,
        push_s: float = 2.0,
        log: Callable[[str], None] | None = None,
    ):
        self.client = client
        self.run_id = run_id
        self.sync_id = sync_id or run_id
        self.root = (root or default_mirror_root()) / self.sync_id
        self.hillclimb_dir = self.root / "hillclimb"
        self.runs_dir = self.hillclimb_dir / "runs"
        self.poll_s = poll_s
        self.push_s = push_s
        self.log = log or (lambda _message: None)
        self._generations: dict[str, str] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self.manifest: dict[str, Any] = {}

    # -- layout -------------------------------------------------------------------

    def prepare(self) -> Path:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        marker = self.hillclimb_dir / "config.yaml"
        if not marker.exists():
            marker.write_text(MIRROR_CONFIG)
        return self.hillclimb_dir

    def engine_env(self) -> dict[str, str]:
        """The environment the engine's viewers run with to read this mirror."""
        return {"HILLCLIMB_DIR": str(self.hillclimb_dir), REMOTE_STATE_ENV: "1"}

    def apply_engine_env(self) -> None:
        os.environ.update(self.engine_env())

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> Path:
        """Pull once (blocking, so the viewer opens on real records), then
        keep pulling and pushing in the background."""
        self.prepare()
        self.pull_once()
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._pull_loop, daemon=True, name="hillclimb-mirror-pull"),
            threading.Thread(target=self._push_loop, daemon=True, name="hillclimb-mirror-push"),
        ]
        for thread in self._threads:
            thread.start()
        return self.hillclimb_dir

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=max(self.poll_s, self.push_s) + 1)
        self._threads = []
        try:
            self.push_once()  # a stop pressed just before quitting still goes out
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)

    def _pull_loop(self) -> None:
        while not self._stop.wait(self.poll_s):
            try:
                self.pull_once()
            except Exception as exc:  # noqa: BLE001 — the viewer keeps its last records
                self.last_error = str(exc)
                self.log(f"[mirror] pull failed: {exc}")

    def _push_loop(self) -> None:
        while not self._stop.wait(self.push_s):
            try:
                self.push_once()
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                self.log(f"[mirror] control push failed: {exc}")

    # -- transfer ----------------------------------------------------------------------

    def pull_once(self) -> int:
        """Download every object whose generation changed. Returns the count."""
        listing = self.client.list_hillclimb_objects(self.run_id)
        downloaded = 0
        with self._lock:
            for entry in listing.get("objects", []):
                path = str(entry.get("path") or "")
                if not path or path.startswith(f"{CONTROL_DIR}/") or ".." in path.split("/"):
                    continue
                generation = str(entry.get("generation") or "")
                if generation and self._generations.get(path) == generation:
                    continue
                body, etag = self.client.get_hillclimb_object(self.run_id, path)
                if body is None:
                    continue
                if path == MANIFEST_NAME:
                    try:
                        self.manifest = json.loads(body)
                    except ValueError:
                        self.manifest = {}
                else:
                    self._write(self.runs_dir / path, body)
                self._generations[path] = generation or _bare(etag)
                downloaded += 1
        return downloaded

    def _write(self, target: Path, body: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".mirror-tmp")
        tmp.write_bytes(body)
        os.replace(tmp, target)

    def pending_commands(self) -> list[Path]:
        return sorted(self.runs_dir.glob(f"*/searches/*/{CONTROL_DIR}/*.json"))

    def push_once(self) -> int:
        """Forward the TUI's queued control files to the platform. Returns the count."""
        pushed = 0
        for path in self.pending_commands():
            try:
                payload = json.loads(path.read_text())
            except (OSError, ValueError):
                path.unlink(missing_ok=True)
                continue
            action = payload.get("action")
            if action not in ("stop", "prune"):
                path.unlink(missing_ok=True)
                continue
            self.client.send_hillclimb_control(
                self.run_id,
                action=action,
                candidate_id=payload.get("candidate_id") or None,
                reason=str(payload.get("reason") or ""),
                source=str(payload.get("source") or "tui"),
            )
            path.unlink(missing_ok=True)
            pushed += 1
        return pushed


def _bare(etag: str | None) -> str:
    return (etag or "").strip().strip('"')


def sync_id_of(run: dict[str, Any]) -> str:
    sync_id = (run.get("parameters") or {}).get("sync_id")
    if not sync_id:
        raise RebaseWorkflowError(f"run {run.get('id')} is not a hillclimb search (no sync_id)")
    return str(sync_id)
