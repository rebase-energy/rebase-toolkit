"""Hillclimb searches on the Rebase Platform (`rebase hillclimb ...`).

A search is an ordinary platform run: an ephemeral function on the
`cloud_run_jobs` backend with ``image_spec.runtime="hillclimb"`` whose source
is a generated stub calling :func:`hosted_search` (this module is baked into
the hillclimb job image). The stub runs `hillclimb.run_search`, mirrors the
search's on-disk state (status.json, journal.jsonl, best/) to GCS every ~30 s,
and applies stop commands written to the same prefix — so the CLI can watch
and control a hosted search exactly like `hillclimb watch` does locally.

State prefix: ``gs://<bucket>/hillclimb/<sync-id>/`` — the bucket comes from
``REBASE_HILLCLIMB_ARTIFACTS_BUCKET`` in the job (injected by the backend) and
from ``REBASE_HILLCLIMB_BUCKET`` / profile config on the client; the sync id
is generated at submit time and stored in the run's parameters.

Requires the ``hillclimb`` extra: ``pip install rebase-toolkit[hillclimb]``.

TODO(tui): dedicated Searches drill-down (runs → searches → candidate tree)
in rebase.tui, reading the synced state; hosted searches already appear in
the ordinary runs table (names are prefixed ``hillclimb:``).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

RUN_NAME_PREFIX = "hillclimb:"
GCS_ROOT = "hillclimb"
SYNC_INTERVAL_S = 30
SYNCED_FILES = ("run.yaml", "search.yaml", "status.json", "journal.jsonl", "knowledge_card.yaml")
SYNCED_DIRS = ("best",)

LOCAL_CONFIG_TEMPLATE = """\
# Hillclimb workspace settings used by `rebase hillclimb`.
# CLI flags override these defaults.

model: sonnet

# search:
#   parallel_agents: 1
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


def _bucket_name(explicit: str | None = None) -> str:
    bucket = (
        explicit or os.environ.get("REBASE_HILLCLIMB_ARTIFACTS_BUCKET") or os.environ.get("REBASE_HILLCLIMB_BUCKET")
    )
    if not bucket:
        raise RuntimeError("no artifacts bucket configured (REBASE_HILLCLIMB_BUCKET)")
    return bucket


def _gcs_bucket(name: str):
    try:
        from google.cloud import storage
    except ModuleNotFoundError as exc:
        raise RuntimeError("GCS sync needs google-cloud-storage: pip install 'rebase-toolkit[hillclimb]'") from exc
    return storage.Client().bucket(name)


def gcs_prefix(sync_id: str) -> str:
    return f"{GCS_ROOT}/{sync_id}"


# -- in-job side (runs inside the hillclimb job image) ---------------------------------


class _StateSync:
    """Mirror a run dir to GCS and pull control commands back, on a timer."""

    def __init__(self, bucket_name: str, sync_id: str, runs_dir: Path):
        self.bucket = _gcs_bucket(bucket_name)
        self.prefix = gcs_prefix(sync_id)
        self.runs_dir = runs_dir
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="hillclimb-gcs-sync")
        self._seen_commands: set[str] = set()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=SYNC_INTERVAL_S)
        self.sync_once()  # final flush

    def _loop(self) -> None:
        while not self._stop.wait(SYNC_INTERVAL_S):
            try:
                self.sync_once()
                self.pull_control()
            except Exception as exc:  # noqa: BLE001 — sync must never kill the search
                print(f"[gcs-sync] transient failure: {exc}")

    def sync_once(self) -> None:
        for run_dir in self.runs_dir.iterdir() if self.runs_dir.exists() else []:
            if not (run_dir / "run.yaml").exists():
                continue
            self._upload_if_exists(run_dir, run_dir / "run.yaml")
            searches = run_dir / "searches"
            for search_dir in searches.iterdir() if searches.exists() else []:
                for name in SYNCED_FILES:
                    self._upload_if_exists(run_dir, search_dir / name)
                for dirname in SYNCED_DIRS:
                    directory = search_dir / dirname
                    for path in directory.iterdir() if directory.exists() else []:
                        if path.is_file():
                            self._upload_if_exists(run_dir, path)

    def _upload_if_exists(self, run_dir: Path, path: Path) -> None:
        if not path.exists():
            return
        rel = path.relative_to(self.runs_dir)
        self.bucket.blob(f"{self.prefix}/{rel}").upload_from_filename(str(path))

    def pull_control(self) -> None:
        """Download new control commands (stop/prune JSON files) into each
        search's local control/ queue — the engine polls it between operators."""
        for blob in self.bucket.client.list_blobs(self.bucket, prefix=f"{self.prefix}/control/"):
            name = blob.name.rsplit("/", 1)[-1]
            if not name.endswith(".json") or name in self._seen_commands:
                continue
            self._seen_commands.add(name)
            payload = blob.download_as_bytes()
            for run_dir in self.runs_dir.iterdir():
                searches = run_dir / "searches"
                for search_dir in searches.iterdir() if searches.exists() else []:
                    control = search_dir / "control"
                    control.mkdir(exist_ok=True)
                    (control / name).write_bytes(payload)


def hosted_search(
    target: str,
    budget_s: int,
    name: str | None = None,
    sync_id: str | None = None,
    model: str | None = None,
    backend: str | None = None,
    holdout: bool = True,
    seed_solution_code: str | None = None,
    knowledge_context: str | None = None,
) -> dict[str, Any]:
    """Entry point of the generated search stub (runs inside the job image).

    Runs one hillclimb search with platform-appropriate config and GCS state
    sync; returns a summary dict that becomes the platform run's result.
    Agent auth: subscription billing when CLAUDE_CODE_OAUTH_TOKEN was injected
    (Secret Manager), else api-key."""
    hillclimb = _require_hillclimb()
    from hillclimb.config import Config

    home = Path(os.environ.get("HILLCLIMB_HOME", "/hillclimb"))
    config = Config()
    config.backend_auth = "subscription" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else "api-key"
    # hosted containers run one search with several operator workers; the
    # machine-wide semaphore is meaningless inside a single-search container
    config.search.parallel_agents = int(os.environ.get("REBASE_HILLCLIMB_PARALLEL_AGENTS", "3"))
    config.search.machine_max_agents = 0
    # agent-spend ceiling backing the platform's credit reservation: the
    # engine parks (resumable) when cumulative backend cost reaches it
    config.budget.max_cost_usd = float(os.environ.get("REBASE_HILLCLIMB_MAX_COST_USD", "0"))
    if backend:
        config.backend = backend
    if model:
        config.model = model
    config.paths.runs_dir = home / "runs"
    # venv paths stay None → the hash-keyed machine-cache venv, which the
    # image prebakes with HILLCLIMB_CACHE_DIR + the vendored emflow source;
    # matching the source here makes the runtime hash hit the baked venv
    if Path("/src/emflow").exists():
        config.emflow.source = "/src/emflow"
    config.paths.runs_dir.mkdir(parents=True, exist_ok=True)

    sync: _StateSync | None = None
    bucket = os.environ.get("REBASE_HILLCLIMB_ARTIFACTS_BUCKET")
    if bucket and sync_id:
        sync = _StateSync(bucket, sync_id, config.paths.runs_dir)
        sync.start()
    seed_path = None
    if seed_solution_code:
        # incumbent model shipped as a run parameter; scored as the floor
        # candidate the search must beat
        seed_path = home / "seed_solution.py"
        seed_path.write_text(seed_solution_code)
    try:
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
    finally:
        if sync is not None:
            sync.stop()

    cost_usd = 0.0
    try:
        from hillclimb.status import read_status

        final_status = read_status(outcome.search_dir)
        if final_status is not None:
            cost_usd = final_status.cost_usd
    except Exception:  # noqa: BLE001 — cost reporting must never fail a run
        pass

    selected = outcome.selected
    return {
        "state": outcome.state,
        "ref": outcome.ref,
        "cost_usd": cost_usd,  # settled against the credit reservation
        "target": target,
        "sync_id": sync_id,
        "gcs_prefix": gcs_prefix(sync_id) if bucket and sync_id else None,
        "selected": (
            {
                "candidate_id": selected.candidate_id,
                "operator": selected.operator,  # "seed" = incumbent not beaten
                "val_score": selected.val_score,
                "holdout_score": selected.holdout_score,
                "summary": selected.summary,
            }
            if selected is not None
            else None
        ),
        "error": outcome.error,
    }


_STUB_TEMPLATE = '''\
"""Generated hillclimb search stub — submitted by `rebase hillclimb start`."""


def run(target: str, budget_s: int, name: str, sync_id: str, model: str = "",
        backend: str = "", holdout: bool = True, seed_solution_code: str = "",
        knowledge_context: str = ""):
    from rebase.hillclimb import hosted_search

    return hosted_search(target, budget_s=budget_s, name=name,
                         sync_id=sync_id, model=model or None,
                         backend=backend or None,
                         holdout=holdout,
                         seed_solution_code=seed_solution_code or None,
                         knowledge_context=knowledge_context or None)
'''


# -- client side ------------------------------------------------------------------------


def start_hosted_search(
    client,
    target: str,
    *,
    budget_s: int,
    name: str | None = None,
    project: str = "hillclimb",
    model: str | None = None,
    backend: str | None = None,
    holdout: bool = True,
    seed_solution_code: str | None = None,
    knowledge_context: str | None = None,
):
    """Submit a hosted search as an ephemeral cloud_run_jobs run. Returns the
    platform Run handle (poll with `rebase run get`, watch state via GCS)."""
    sync_id = uuid.uuid4().hex[:12]
    run_name = f"{RUN_NAME_PREFIX}{name or target}"
    return client.run_ephemeral(
        target_type="function",
        project=project,
        name=run_name,
        source_code=_STUB_TEMPLATE,
        entrypoint="run",
        parameters={
            "target": target,
            "budget_s": budget_s,
            "name": name or target,
            "sync_id": sync_id,
            "model": model or "",
            "backend": backend or "",
            "holdout": holdout,
            "seed_solution_code": seed_solution_code or "",
            "knowledge_context": knowledge_context or "",
        },
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


def read_hosted_state(sync_id: str, bucket: str | None = None) -> dict[str, Any]:
    """Fetch the synced status.json files for a hosted search."""
    gcs = _gcs_bucket(_bucket_name(bucket))
    prefix = gcs_prefix(sync_id)
    statuses: dict[str, Any] = {}
    for blob in gcs.client.list_blobs(gcs, prefix=prefix):
        if blob.name.endswith("status.json"):
            search_ref = "/".join(blob.name[len(prefix) + 1 :].split("/")[:3])
            statuses[search_ref] = json.loads(blob.download_as_bytes())
    return statuses


def request_hosted_stop(sync_id: str, bucket: str | None = None) -> str:
    """Queue a graceful stop: the in-job sync loop delivers it to every
    search's control queue within one sync interval."""
    gcs = _gcs_bucket(_bucket_name(bucket))
    stamp = time.strftime("%Y%m%dT%H%M%S")
    name = f"{gcs_prefix(sync_id)}/control/{stamp}-stop.json"
    gcs.blob(name).upload_from_string(
        json.dumps({"action": "stop", "reason": "requested via rebase CLI", "source": "cli"})
    )
    return name


def promote_local(run_ref: str, dest: Path, runs_dir: Path | None = None) -> list[Path]:
    """Local-run counterpart of :func:`fetch_best_solution`: copy every
    search's ``best/solution.py`` from a run under ``runs/`` into ``dest``.

    ``run_ref`` is a run id from ``runs/`` (e.g. ``20260705-203512-grid-...``),
    a unique substring of one, or ``latest``.
    """
    if runs_dir is None:
        # resolve through the workspace marker, like the engine itself
        from hillclimb.config import Config

        runs_dir = Config.load(require_workspace=False).paths.runs_dir
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


def fetch_best_solution(sync_id: str, dest: Path, bucket: str | None = None) -> list[Path]:
    """Download every search's best/solution.py for promotion into the
    workspace repo. Returns the written paths."""
    gcs = _gcs_bucket(_bucket_name(bucket))
    prefix = gcs_prefix(sync_id)
    written: list[Path] = []
    for blob in gcs.client.list_blobs(gcs, prefix=prefix):
        parts = blob.name[len(prefix) + 1 :].split("/")
        if len(parts) >= 4 and parts[-2] == "best" and parts[-1] == "solution.py":
            search_id = parts[2]
            out = dest / f"{search_id.replace(':', '_').replace('-', '_')}.py"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(blob.download_as_bytes())
            written.append(out)
    return written


def read_best_solution_text(sync_id: str, bucket: str | None = None) -> str | None:
    """The first synced best/solution.py as text (single-search runs), or
    None when nothing has synced."""
    gcs = _gcs_bucket(_bucket_name(bucket))
    prefix = gcs_prefix(sync_id)
    for blob in gcs.client.list_blobs(gcs, prefix=prefix):
        parts = blob.name[len(prefix) + 1 :].split("/")
        if len(parts) >= 4 and parts[-2] == "best" and parts[-1] == "solution.py":
            return blob.download_as_bytes().decode("utf-8")
    return None


def format_hosted_status(statuses: dict[str, Any]) -> str:
    if not statuses:
        return "no synced state yet (first sync lands ~30s after the job starts)"
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
