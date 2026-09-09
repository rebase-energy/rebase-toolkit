"""Hillclimb searches on the Rebase Platform (`rebase hillclimb ...`).

A hosted search is an ordinary platform run: an ephemeral function on the
`cloud_run_jobs` backend with ``image_spec.runtime="hillclimb"`` whose source
is a generated stub calling :func:`hosted_search` (this module is baked into
the hillclimb job image). Inside the job the stub lays out a hillclimb dir,
runs the engine -- one search in-process, or a fleet of N engine processes
under one run -- mirrors the run dir's records to GCS on a timer, and applies
control commands (stop, prune) written to the same prefix.

State prefix: ``gs://<bucket>/hillclimb/<sync-id>/`` -- the bucket comes from
``REBASE_HILLCLIMB_ARTIFACTS_BUCKET`` in the job (injected by the platform);
the sync id is minted at submit and stored in the run's parameters. A laptop
never reads the bucket: the platform API serves the prefix
(``GET /runs/{id}/hillclimb/objects``) and takes control commands
(``POST /runs/{id}/hillclimb/control``), which is what the client-side helpers
below and :mod:`rebase.hillclimb_mirror` use.

Requires the ``hillclimb`` extra on the client (the engine's own TUIs render
the mirrored state): ``pip install rebase-toolkit[hillclimb]``.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

RUN_NAME_PREFIX = "hillclimb:"
GCS_ROOT = "hillclimb"
MANIFEST_NAME = "hosted.json"
CONTROL_DIR = "control"
#: status.json and run.yaml cadence: well inside the engine's 90 s heartbeat
#: window even with a slow viewer, so a live search never reads as crashed.
STATUS_SYNC_INTERVAL_S = 10
#: everything else (journal, streams, best/) -- only what changed is uploaded
SYNC_INTERVAL_S = 30
#: per-candidate streams and logs are synced as tails: the viewers show the
#: last few thousand characters, and an agent stream can run to megabytes
TAIL_BYTES = 256 * 1024
#: after the search budget, how long the stub waits for a fleet's engines
#: (holdout evaluation + distillation run past the budget) before killing them
FLEET_GRACE_S = 900
#: the workspace secret `rebase hillclimb start` attaches by default
DEFAULT_SECRET_NAME = "hillclimb"
AGENT_CREDENTIAL_ENVS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "HF_TOKEN")
PARALLEL_SEARCHES_ENV = "REBASE_HILLCLIMB_PARALLEL_SEARCHES"
PARALLEL_OPERATORS_ENV = "REBASE_HILLCLIMB_PARALLEL_OPERATORS"
DEFAULT_PARALLEL_OPERATORS = 3

STATUS_FILES = ("run.yaml",)
SEARCH_STATUS_FILES = ("status.json",)
SEARCH_FILES = ("search.yaml", "journal.jsonl", "knowledge-card.yaml", "mlebench-grade.json")
SEARCH_DIRS = ("best", "holdout-eval")
CANDIDATE_FILES = ("notes.md", "agent_stream.jsonl", "exec_stdout.log", "exec_stderr.log")
TAILED_FILES = ("agent_stream.jsonl", "exec_stdout.log", "exec_stderr.log")

LOCAL_CONFIG_TEMPLATE = """\
# Hillclimb workspace settings used by `rebase hillclimb`.
# CLI flags override these defaults.

model: sonnet

# search:
#   parallel_operators: 1
#   n_trials: 1

# holdout:
#   enabled: true
#   top_k: 5

# learning:
#   enabled: true
#   max_cards: 3
#   live: true
"""


def _require_hillclimb():
    try:
        import hillclimb
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "hillclimb commands need the hillclimb extra: pip install 'rebase-toolkit[hillclimb]'"
        ) from exc
    return hillclimb


def init_local_workspace(directory: Path, *, force: bool = False) -> Path:
    """Initialize the on-disk state used by local ``rebase hillclimb`` runs."""
    root = directory.resolve()
    marker_dir = "hillclimb"
    marker_file = "config.yaml"
    existing = next(
        (candidate for candidate in (root, *root.parents) if (candidate / marker_dir / marker_file).exists()),
        None,
    )
    if existing is not None and not force:
        marker = existing / marker_dir / marker_file
        raise RuntimeError(f"already inside the Hillclimb workspace at {existing} ({marker} exists)")

    folder = root / marker_dir
    for subdirectory in ("knowledge", "problems", "runs", "specs"):
        path = folder / subdirectory
        path.mkdir(parents=True, exist_ok=True)
        (path / ".gitkeep").touch()
    (folder / marker_file).write_text(LOCAL_CONFIG_TEMPLATE)

    gitignore = root / ".gitignore"
    ignore_line = f"{marker_dir}/runs/"
    existing_ignore = gitignore.read_text() if gitignore.exists() else ""
    if ignore_line not in existing_ignore.splitlines():
        separator = "\n" if existing_ignore and not existing_ignore.endswith("\n") else ""
        gitignore.write_text(f"{existing_ignore}{separator}{ignore_line}\n")
    return root


def discover_emflow_problems(family: str | None = None) -> list[dict[str, str]]:
    """Return installed emflow registry targets without loading their datasets."""
    _require_hillclimb()
    try:
        import emflow as ef
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "problem discovery needs the hillclimb extra: pip install 'rebase-toolkit[hillclimb]'"
        ) from exc

    normalized_family = family.removeprefix("emflow://").rstrip(":") if family else None
    problems = []
    for name in sorted(ef.list_problems()):
        problem_family, separator, track = name.partition(":")
        if normalized_family and problem_family != normalized_family:
            continue
        problems.append(
            {
                "target": f"emflow://{name}",
                "family": problem_family,
                "track": track if separator else "—",
            }
        )
    return problems


def gcs_prefix(sync_id: str) -> str:
    return f"{GCS_ROOT}/{sync_id}"


# -- in-job side (runs inside the hillclimb job image) ---------------------------------


def _gcs_bucket(name: str):
    try:
        from google.cloud import storage
    except ModuleNotFoundError as exc:
        raise RuntimeError("GCS sync needs google-cloud-storage in the job image") from exc
    return storage.Client().bucket(name)


def iter_sync_files(runs_dir: Path) -> Iterator[tuple[str, Path, bool]]:
    """Every file of every run under ``runs_dir`` that a viewer needs, as
    (path relative to runs_dir, file, tail-only). Symlinks (the candidate
    dirs' ``data`` / ``problem`` links into the machine cache) are skipped:
    they point outside the run and would upload the dataset."""
    if not runs_dir.exists():
        return
    for run_dir in sorted(runs_dir.iterdir()):
        if run_dir.is_symlink() or not (run_dir / "run.yaml").is_file():
            continue
        yield from _files(runs_dir, run_dir, STATUS_FILES)
        yield from _dir_files(runs_dir, run_dir / "logs")
        yield from _dir_files(runs_dir, run_dir / "knowledge")
        searches = run_dir / "searches"
        for search_dir in sorted(searches.iterdir()) if searches.is_dir() else []:
            if search_dir.is_symlink():
                continue
            yield from _files(runs_dir, search_dir, SEARCH_STATUS_FILES + SEARCH_FILES)
            for dirname in SEARCH_DIRS:
                yield from _dir_files(runs_dir, search_dir / dirname)
            candidates = search_dir / "candidates"
            for candidate_dir in sorted(candidates.iterdir()) if candidates.is_dir() else []:
                if candidate_dir.is_symlink():
                    continue
                yield from _files(runs_dir, candidate_dir, CANDIDATE_FILES)


def _files(runs_dir: Path, directory: Path, names: tuple[str, ...]) -> Iterator[tuple[str, Path, bool]]:
    for name in names:
        path = directory / name
        if path.is_file() and not path.is_symlink():
            yield path.relative_to(runs_dir).as_posix(), path, name in TAILED_FILES


def _dir_files(runs_dir: Path, directory: Path) -> Iterator[tuple[str, Path, bool]]:
    if not directory.is_dir() or directory.is_symlink():
        return
    for path in sorted(directory.rglob("*")):
        if path.is_file() and not path.is_symlink():
            yield path.relative_to(runs_dir).as_posix(), path, False


def is_status_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return name in SEARCH_STATUS_FILES or name in STATUS_FILES


def tail_bytes(path: Path, limit: int = TAIL_BYTES) -> bytes:
    """The last ``limit`` bytes of a file, cut at a line boundary so a
    JSONL stream stays parseable."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size <= limit:
            return handle.read()
        handle.seek(size - limit)
        data = handle.read()
    newline = data.find(b"\n")
    return data[newline + 1 :] if newline >= 0 else data


class _StateSync:
    """Mirror a run dir to GCS and pull control commands back, on a timer.

    Two cadences on one thread: the status records every
    ``STATUS_SYNC_INTERVAL_S`` (and the control queue with them, so a stop
    lands within seconds), everything else every ``SYNC_INTERVAL_S``. A file
    is uploaded only when its (mtime, size) changed since the last upload;
    tailed files ship their last ``TAIL_BYTES``.
    """

    def __init__(self, bucket_name: str, sync_id: str, runs_dir: Path):
        self.bucket_name = bucket_name
        self.bucket = _gcs_bucket(bucket_name)
        self.prefix = gcs_prefix(sync_id)
        self.sync_id = sync_id
        self.runs_dir = runs_dir
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="hillclimb-gcs-sync")
        self._seen_commands: set[str] = set()
        self._uploaded: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()
        self._manifest: dict[str, Any] = {}

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=SYNC_INTERVAL_S)
        self.sync_once()  # final flush

    def _loop(self) -> None:
        tick = 0
        while not self._stop.wait(STATUS_SYNC_INTERVAL_S):
            tick += 1
            full = tick % max(1, SYNC_INTERVAL_S // STATUS_SYNC_INTERVAL_S) == 0
            try:
                self.sync_once(status_only=not full)
                self.pull_control()
            except Exception as exc:  # noqa: BLE001 — sync must never kill the search
                print(f"[gcs-sync] transient failure: {exc}")

    def sync_once(self, *, status_only: bool = False) -> int:
        """Upload what changed; returns how many objects went up."""
        uploaded = 0
        with self._lock:
            for rel, path, tail in iter_sync_files(self.runs_dir):
                if status_only and not is_status_file(rel):
                    continue
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue
                fingerprint = (stat.st_mtime_ns, stat.st_size)
                if self._uploaded.get(rel) == fingerprint:
                    continue
                blob = self.bucket.blob(f"{self.prefix}/{rel}")
                if tail:
                    blob.upload_from_string(tail_bytes(path))
                else:
                    blob.upload_from_filename(str(path))
                self._uploaded[rel] = fingerprint
                uploaded += 1
        return uploaded

    def write_manifest(self, **fields: Any) -> dict[str, Any]:
        """``hosted.json`` at the prefix root: what the job is, for readers
        that have only the run id. Fields accumulate across calls, so the
        finishing write keeps what the starting one recorded."""
        self._manifest.update(fields)
        payload = {"bucket": self.bucket_name, "sync_id": self.sync_id, "prefix": self.prefix, **self._manifest}
        self.bucket.blob(f"{self.prefix}/{MANIFEST_NAME}").upload_from_string(
            json.dumps(payload, sort_keys=True), content_type="application/json"
        )
        return payload

    def pull_control(self) -> list[str]:
        """Download new control commands (stop/prune JSON files) into every
        *running* search's local control/ queue -- the engine polls it
        between operators. Returns the names delivered this time."""
        delivered: list[str] = []
        for blob in self.bucket.client.list_blobs(self.bucket, prefix=f"{self.prefix}/{CONTROL_DIR}/"):
            name = blob.name.rsplit("/", 1)[-1]
            if not name.endswith(".json") or name in self._seen_commands:
                continue
            self._seen_commands.add(name)
            payload = blob.download_as_bytes()
            for control in running_control_dirs(self.runs_dir):
                control.mkdir(exist_ok=True)
                tmp = control / f"{name}.tmp"
                tmp.write_bytes(payload)
                os.replace(tmp, control / name)
            delivered.append(name)
        return delivered


def running_control_dirs(runs_dir: Path) -> list[Path]:
    """The control/ dir of every search whose own status says running; a
    parked or finished search must not collect commands it will never drain."""
    dirs: list[Path] = []
    if not runs_dir.exists():
        return dirs
    for status_path in sorted(runs_dir.glob("*/searches/*/status.json")):
        try:
            state = json.loads(status_path.read_text()).get("state")
        except (OSError, ValueError):
            continue
        if state == "running":
            dirs.append(status_path.parent / CONTROL_DIR)
    return dirs


def hosted_config(
    *,
    backend: str | None,
    model: str | None,
    policy: str | None,
    parallel_operators: int,
    subscription: bool,
    max_cost_usd: float,
    emflow_source: str | None,
) -> dict[str, Any]:
    """The hillclimb ``config.yaml`` a hosted container runs with. Written as
    a file rather than assigned onto a loaded Config so the engine's own
    loader validates it, and so child engines of a fleet read the same thing."""
    config: dict[str, Any] = {
        "backend_auth": "subscription" if subscription else "api-key",
        "search": {
            # hosted containers run one search (or one fleet) with several
            # operator workers; the machine-wide semaphore means nothing here
            "parallel_operators": max(1, int(parallel_operators)),
            "machine_max_operators": 0,
        },
        # agent-spend ceiling backing the platform's credit reservation: the
        # engine parks (resumable) when cumulative backend cost reaches it
        "budget": {"max_cost_usd": float(max_cost_usd)},
    }
    if backend:
        config["backend"] = backend
    if model:
        config["model"] = model
    if policy:
        config["search"]["policy"] = policy
    if emflow_source:
        # the image prebakes the emflow runtime venv keyed on this source;
        # matching it here makes the runtime hash hit the baked venv
        config["emflow"] = {"source": emflow_source}
    return config


def _dump_yaml(data: dict[str, Any]) -> str:
    try:
        import yaml
    except ModuleNotFoundError:  # pragma: no cover - the engine depends on pyyaml
        return json.dumps(data, indent=2)
    return yaml.safe_dump(data, sort_keys=True)


def _search_statuses(run_dir: Path) -> dict[str, dict[str, Any]]:
    statuses: dict[str, dict[str, Any]] = {}
    for status_path in sorted(run_dir.glob("searches/*/status.json")):
        try:
            statuses[status_path.parent.name] = json.loads(status_path.read_text())
        except (OSError, ValueError):
            continue
    return statuses


def _better(candidate: dict[str, Any], incumbent: dict[str, Any] | None, *, higher_is_better: bool) -> bool:
    if incumbent is None:
        return True
    for key in ("holdout_score", "val_score"):
        left, right = candidate.get(key), incumbent.get(key)
        if left is None or right is None:
            continue
        return left > right if higher_is_better else left < right
    return False


def summarize_run(
    run_dir: Path, *, target: str, sync_id: str | None, bucket: str | None, higher_is_better: bool = True
) -> dict[str, Any]:
    """The platform run's result for a hosted run dir: every search's state
    and cost from its last status record, the best selected candidate across
    them, and the summed agent cost the platform settles credits from."""
    statuses = _search_statuses(run_dir)
    searches: list[dict[str, Any]] = []
    selected: dict[str, Any] | None = None
    cost_usd = 0.0
    for search_id, status in statuses.items():
        try:
            search_cost = float(status.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            search_cost = 0.0
        cost_usd += search_cost
        chosen = status.get("selected") or None
        searches.append(
            {
                "ref": f"{run_dir.name}/{search_id}",
                "state": status.get("state", "unknown"),
                "cost_usd": search_cost,
                "candidates": status.get("candidates"),
                "best": status.get("best"),
                "selected": chosen,
                "error": status.get("last_error"),
            }
        )
        if chosen and _better(chosen, selected, higher_is_better=higher_is_better):
            selected = {**chosen, "search_ref": f"{run_dir.name}/{search_id}"}
    states = [entry["state"] for entry in searches]
    if not states:
        state = "failed"
    elif all(entry == "done" for entry in states):
        state = "done"
    elif "failed" in states:
        state = "failed"
    elif "parked" in states or "crashed" in states:
        state = "parked"
    elif "stopped" in states:
        state = "stopped"
    else:
        state = states[0]
    return {
        "state": state,
        "run_id": run_dir.name,
        "target": target,
        "sync_id": sync_id,
        "bucket": bucket,
        "gcs_prefix": gcs_prefix(sync_id) if bucket and sync_id else None,
        "cost_usd": round(cost_usd, 6),  # settled against the credit reservation
        "searches": searches,
        "selected": selected,
        "error": next((entry["error"] for entry in searches if entry["error"]), None),
    }


def request_stop_everywhere(runs_dir: Path, *, reason: str) -> int:
    """Queue a stop in every running search's control dir (the engine parks
    the search and its journal stays resumable). Used on SIGTERM, when the
    platform is about to kill the container."""
    written = 0
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f".{int((time.time() % 1) * 1_000_000):06d}"
    payload = json.dumps(
        {
            "action": "stop",
            "candidate_id": None,
            "reason": reason,
            "source": "agent",
            "requested_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        }
    ).encode("utf-8")
    for control in running_control_dirs(runs_dir):
        control.mkdir(exist_ok=True)
        tmp = control / f"{stamp}-stop.json.tmp"
        tmp.write_bytes(payload)
        os.replace(tmp, control / f"{stamp}-stop.json")
        written += 1
    return written


def hosted_search(
    target: str,
    budget_s: int,
    name: str | None = None,
    sync_id: str | None = None,
    model: str | None = None,
    backend: str | None = None,
    policy: str | None = None,
    parallel_searches: int = 1,
    parallel_operators: int = 0,
    holdout: bool = True,
    seed_solution_code: str | None = None,
    knowledge_context: str | None = None,
) -> dict[str, Any]:
    """Entry point of the generated search stub (runs inside the job image).

    Lays out a hillclimb dir under $HOME, writes the hosted config, runs one
    search in-process or a fleet of ``parallel_searches`` engines under one
    run, with GCS state sync throughout; returns the summary dict that
    becomes the platform run's result. Agent auth: subscription billing when
    CLAUDE_CODE_OAUTH_TOKEN was injected, else api-key."""
    hillclimb = _require_hillclimb()
    from hillclimb.config import Config

    home = Path(os.environ.get("HILLCLIMB_DIR") or Path(os.environ.get("HOME", "/hillclimb")) / "hillclimb")
    home.mkdir(parents=True, exist_ok=True)
    operators = int(parallel_operators or os.environ.get(PARALLEL_OPERATORS_ENV) or DEFAULT_PARALLEL_OPERATORS)
    searches = max(1, int(parallel_searches or os.environ.get(PARALLEL_SEARCHES_ENV) or 1))
    emflow_source = "/src/emflow" if Path("/src/emflow").exists() else None
    (home / "config.yaml").write_text(
        _dump_yaml(
            hosted_config(
                backend=backend,
                model=model,
                policy=policy,
                parallel_operators=operators,
                subscription=bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
                max_cost_usd=float(os.environ.get("REBASE_HILLCLIMB_MAX_COST_USD", "0") or 0),
                emflow_source=emflow_source,
            )
        )
    )
    os.environ["HILLCLIMB_DIR"] = str(home)
    config = Config.load()
    if not holdout:
        config.holdout.enabled = False
    runs_dir = config.paths.runs_dir
    runs_dir.mkdir(parents=True, exist_ok=True)

    seed_path = None
    if seed_solution_code:
        # incumbent model shipped as a run parameter; scored as the floor
        # candidate the search must beat
        seed_path = home / "seed_solution.py"
        seed_path.write_text(seed_solution_code)
    knowledge_path = None
    if knowledge_context:
        knowledge_path = home / "knowledge_context.md"
        knowledge_path.write_text(knowledge_context)

    bucket = os.environ.get("REBASE_HILLCLIMB_ARTIFACTS_BUCKET")
    sync: _StateSync | None = None
    if bucket and sync_id:
        sync = _StateSync(bucket, sync_id, runs_dir)
        sync.write_manifest(
            target=target,
            run_name=name or target,
            parallel_searches=searches,
            parallel_operators=operators,
            budget_s=int(budget_s),
            started_at=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            credential_source=os.environ.get("REBASE_HILLCLIMB_CREDENTIAL_SOURCE"),
        )
        sync.start()

    terminating = threading.Event()
    fleet = None

    def on_sigterm(signum, frame):  # noqa: ARG001 — signal handler signature
        # The platform is about to kill the container (timeout or cancel).
        # Park every search cleanly and flush the records before it does.
        terminating.set()
        request_stop_everywhere(runs_dir, reason="platform SIGTERM (job timeout or cancel)")
        if fleet is not None:
            fleet.terminate(grace_s=5.0)
        if sync is not None:
            with contextlib.suppress(Exception):
                sync.sync_once(status_only=True)
        if fleet is None:
            raise SystemExit(143)

    signal.signal(signal.SIGTERM, on_sigterm)

    run_dir: Path | None = None
    error: str | None = None
    try:
        if searches == 1:
            outcome = hillclimb.run_search(
                target,
                budget_s=budget_s,
                name=name,
                config=config,
                holdout=holdout,
                log=print,
                seed_from=seed_path,
                knowledge_context=knowledge_context or None,
            )
            run_dir = outcome.run_dir
            error = outcome.error
        else:
            fleet = hillclimb.api.run_fleet(
                target,
                config=config,
                parallel_searches=searches,
                run_name=name,
                budget=int(budget_s),
                backend=backend,
                model=model,
                policy=policy,
                parallel_operators=operators,
                holdout=holdout,
                seed_from=seed_path,
                knowledge_context_file=knowledge_path,
                log=print,
            )
            run_dir = fleet.run_dir
            print(f"Fleet {fleet.run_id}: {searches} engines x {operators} operators")
            exits = fleet.wait(poll_s=5.0, deadline_s=float(budget_s) + FLEET_GRACE_S)
            if fleet.alive():
                print(f"[fleet] {len(fleet.alive())} engine(s) still running past the budget grace; terminating")
                request_stop_everywhere(runs_dir, reason="budget grace exceeded")
                fleet.terminate()
            failed = [pid for pid, code in exits.items() if code not in (0, 2, None)]
            if failed:
                error = f"{len(failed)} engine(s) exited with an error; see logs/"
    finally:
        if sync is not None:
            sync.stop()

    assert run_dir is not None
    result = summarize_run(run_dir, target=target, sync_id=sync_id, bucket=bucket)
    if error and not result.get("error"):
        result["error"] = error
    if sync is not None:
        try:
            sync.write_manifest(
                target=target,
                finished_at=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                state=result["state"],
            )
        except Exception as exc:  # noqa: BLE001 — a manifest failure must not fail the run
            print(f"[gcs-sync] manifest update failed: {exc}")
    return result


_STUB_TEMPLATE = '''\
"""Generated hillclimb search stub — submitted by `rebase hillclimb start`."""


def run(target: str, budget_s: int, name: str, sync_id: str, model: str = "",
        backend: str = "", policy: str = "", parallel_searches: int = 1,
        parallel_operators: int = 0, holdout: bool = True,
        seed_solution_code: str = "", knowledge_context: str = ""):
    from rebase.hillclimb import hosted_search

    return hosted_search(target, budget_s=budget_s, name=name,
                         sync_id=sync_id, model=model or None,
                         backend=backend or None, policy=policy or None,
                         parallel_searches=parallel_searches,
                         parallel_operators=parallel_operators,
                         holdout=holdout,
                         seed_solution_code=seed_solution_code or None,
                         knowledge_context=knowledge_context or None)
'''


# -- client side ------------------------------------------------------------------------


def resolve_agent_secrets(client, name: str | None) -> tuple[str | None, dict[str, str]]:
    """The ``secrets`` map to attach to a hosted search: the env-name ->
    secret-ref entries of workspace secret ``name`` (explicit, or the
    conventional ``hillclimb`` bundle when it exists) for the agent
    credentials. Returns (bundle name used, refs); (None, {}) means the
    deployment's own credentials apply."""
    from .client import Secret

    if name is None:
        names = {str(entry.get("name")) for entry in client.list_secrets()}
        if DEFAULT_SECRET_NAME not in names:
            return None, {}
        name = DEFAULT_SECRET_NAME
    refs = Secret.from_name(name).resolve(client)
    picked = {key: ref for key, ref in refs.items() if key in AGENT_CREDENTIAL_ENVS}
    if not picked:
        raise RuntimeError(
            f"workspace secret {name!r} carries none of {', '.join(AGENT_CREDENTIAL_ENVS)}; "
            f"create it with: rebase secret create {name} CLAUDE_CODE_OAUTH_TOKEN=-"
        )
    return name, picked


def start_hosted_search(
    client,
    target: str,
    *,
    budget_s: int,
    name: str | None = None,
    project: str = "hillclimb",
    model: str | None = None,
    backend: str | None = None,
    policy: str | None = None,
    parallel_searches: int = 1,
    parallel_operators: int | None = None,
    holdout: bool = True,
    seed_solution_code: str | None = None,
    knowledge_context: str | None = None,
    secrets: dict[str, str] | None = None,
):
    """Submit a hosted search as an ephemeral cloud_run_jobs run. Returns the
    platform Run handle (poll with `rebase hillclimb status`, watch with
    `rebase hillclimb watch`)."""
    sync_id = uuid.uuid4().hex[:12]
    run_name = f"{RUN_NAME_PREFIX}{name or target}"
    searches = max(1, int(parallel_searches))
    operators = int(parallel_operators or DEFAULT_PARALLEL_OPERATORS)
    return client.run_ephemeral(
        target_type="function",
        project=project,
        name=run_name,
        source_code=_STUB_TEMPLATE,
        entrypoint="run",
        parameters={
            "target": target,
            "budget_s": int(budget_s),
            "name": name or target,
            "sync_id": sync_id,
            "model": model or "",
            "backend": backend or "",
            "policy": policy or "",
            "parallel_searches": searches,
            "parallel_operators": operators,
            "holdout": holdout,
            "seed_solution_code": seed_solution_code or "",
            "knowledge_context": knowledge_context or "",
        },
        # the platform sizes the job (and its credit reservation) from these
        env={PARALLEL_SEARCHES_ENV: str(searches), PARALLEL_OPERATORS_ENV: str(operators)},
        secrets=dict(secrets or {}),
        mode="job",
        image_spec={"runtime": "hillclimb"},
    )


def run_local_search(
    target: str,
    *,
    budget_s: int,
    name: str | None = None,
    model: str | None = None,
    backend: str | None = None,
    holdout: bool = True,
    log=print,
):
    """`--local` mode: same command surface, search runs on this machine with
    the user's own hillclimb config (subscription agent auth, local runs/)."""
    hillclimb = _require_hillclimb()
    return hillclimb.run_search(
        target,
        budget_s=budget_s,
        name=name,
        model=model,
        backend=backend,
        holdout=holdout,
        log=log,
    )


def search_ref_of(path: str) -> str | None:
    """``<run>/<search>`` for an object path under the prefix, else None."""
    parts = path.split("/")
    if len(parts) >= 4 and parts[1] == "searches":
        return f"{parts[0]}/{parts[2]}"
    return None


def read_hosted_state(client, run_id: str) -> dict[str, Any]:
    """Every search's synced status.json, keyed by ``<run>/<search>``."""
    statuses: dict[str, Any] = {}
    for entry in client.list_hillclimb_objects(run_id).get("objects", []):
        path = str(entry.get("path", ""))
        ref = search_ref_of(path)
        if ref is None or not path.endswith("/status.json"):
            continue
        body, _etag = client.get_hillclimb_object(run_id, path)
        if body:
            try:
                statuses[ref] = json.loads(body)
            except ValueError:
                continue
    return statuses


def request_hosted_stop(client, run_id: str, *, reason: str = "requested via rebase CLI") -> str:
    """Queue a graceful stop: the in-job sync loop delivers it to every
    running search's control queue within one status interval."""
    response = client.send_hillclimb_control(run_id, action="stop", reason=reason)
    return str(response.get("path", ""))


def fetch_best_solution(client, run_id: str, dest: Path) -> list[Path]:
    """Download every search's best/solution.py for promotion into the
    workspace repo. Returns the written paths."""
    written: list[Path] = []
    for entry in client.list_hillclimb_objects(run_id).get("objects", []):
        path = str(entry.get("path", ""))
        parts = path.split("/")
        if len(parts) >= 5 and parts[1] == "searches" and parts[-2] == "best" and parts[-1] == "solution.py":
            body, _etag = client.get_hillclimb_object(run_id, path)
            if not body:
                continue
            search_id = parts[2]
            out = dest / f"{search_id.replace(':', '_').replace('-', '_')}.py"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(body)
            written.append(out)
    return written


def promote_local(run_ref: str, dest: Path, runs_dir: Path | None = None) -> list[Path]:
    """Local-run counterpart of :func:`fetch_best_solution`: copy every
    search's ``best/solution.py`` from a run under ``runs/`` into ``dest``.

    ``run_ref`` is a run id from ``runs/`` (e.g. ``20260705-203512-grid-...``),
    a unique substring of one, or ``latest``.
    """
    if runs_dir is None:
        # resolve through the workspace marker, like the engine itself
        from hillclimb.config import Config

        runs_dir = Config.load().paths.runs_dir
    runs_dir = Path(runs_dir)
    candidates = sorted(d for d in runs_dir.iterdir() if d.is_dir()) if runs_dir.exists() else []
    if not candidates:
        raise RuntimeError(f"no local runs under {runs_dir.resolve()}")
    if run_ref == "latest":
        matches = candidates[-1:]
    else:
        matches = [d for d in candidates if d.name == run_ref] or [d for d in candidates if run_ref in d.name]
    if not matches:
        raise RuntimeError(f"no local run matching {run_ref!r} under {runs_dir.resolve()}")
    if len(matches) > 1:
        names = ", ".join(d.name for d in matches)
        raise RuntimeError(f"run ref {run_ref!r} is ambiguous: {names}")

    run_dir = matches[-1]
    written: list[Path] = []
    for solution in sorted(run_dir.glob("searches/*/best/solution.py")):
        search_id = solution.parents[1].name
        out = dest / f"{search_id.replace(':', '_').replace('-', '_')}.py"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(solution.read_bytes())
        written.append(out)
    if not written:
        raise RuntimeError(
            f"run {run_dir.name} has no searches with a best/solution.py — did the search select a candidate?"
        )
    return written


def format_hosted_status(statuses: dict[str, Any]) -> str:
    if not statuses:
        return "no synced state yet (the first status lands ~10s after the engine starts)"
    lines = []
    for ref, status in sorted(statuses.items()):
        candidates = status.get("candidates", {})
        best = status.get("best") or {}
        selected = status.get("selected") or {}
        cost = status.get("cost_usd") or 0.0
        lines.append(
            f"{ref}: {status.get('state', '?')}  "
            f"candidates={candidates.get('total', 0)} ({candidates.get('ok', 0)} ok)  "
            f"best={best.get('val_score', '-')}  "
            f"selected={selected.get('candidate_id', '-')}  "
            f"budget_left={int(status.get('budget', {}).get('remaining_s', 0))}s  "
            f"cost=${cost:.2f}"
        )
    return "\n".join(lines)


STUB_SOURCE = _STUB_TEMPLATE  # exported for tests
