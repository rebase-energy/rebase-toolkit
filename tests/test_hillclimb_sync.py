"""The in-job side of a hosted hillclimb search: what the stub mirrors to
the bucket, how it applies control commands, and the run summary the
platform settles credits from. GCS is a dict behind `_gcs_bucket`."""

from __future__ import annotations

import json
import os
from pathlib import Path

from rebase import hillclimb


class FakeBlob:
    def __init__(self, store: dict[str, bytes], name: str):
        self.store, self.name = store, name

    def upload_from_filename(self, filename: str) -> None:
        self.store[self.name] = Path(filename).read_bytes()

    def upload_from_string(self, data, content_type: str | None = None) -> None:
        self.store[self.name] = data if isinstance(data, bytes) else data.encode()

    def download_as_bytes(self) -> bytes:
        return self.store[self.name]


class FakeClient:
    def __init__(self, store: dict[str, bytes]):
        self.store = store

    def list_blobs(self, bucket, prefix: str):
        return [FakeBlob(self.store, name) for name in sorted(self.store) if name.startswith(prefix)]


class FakeBucket:
    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.client = FakeClient(self.store)

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self.store, name)


def _run_tree(runs_dir: Path, *, state: str = "running", stream_bytes: int = 0) -> Path:
    run_dir = runs_dir / "20260909-120000-solar"
    search = run_dir / "searches" / "solar"
    candidate = search / "candidates" / "c001"
    for directory in (run_dir / "logs", run_dir / "knowledge", search / "best", search / "control", candidate):
        directory.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.yaml").write_text("run_id: x\n")
    (run_dir / "logs" / "01-solar.log").write_text("engine up\n")
    (run_dir / "knowledge" / "live--solar.yaml").write_text("cards: []\n")
    (search / "search.yaml").write_text("search_id: solar\n")
    (search / "status.json").write_text(json.dumps({"state": state, "cost_usd": 1.25, "selected": None}))
    (search / "journal.jsonl").write_text('{"kind": "candidate"}\n')
    (search / "best" / "solution.py").write_text("def get_model(): ...\n")
    (candidate / "notes.md").write_text("tried a thing\n")
    if stream_bytes:
        lines = [json.dumps({"i": i, "pad": "x" * 100}) for i in range(stream_bytes // 110 + 2)]
        (candidate / "agent_stream.jsonl").write_text("\n".join(lines) + "\n")
    # the engine links the dataset and problem into every candidate dir
    os.symlink(runs_dir.parent, candidate / "data")
    os.symlink(runs_dir.parent, candidate / "problem")
    return run_dir


def test_iter_sync_files_covers_the_viewer_inputs_and_skips_symlinks(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    _run_tree(runs_dir, stream_bytes=1000)
    files = {rel: tail for rel, _path, tail in hillclimb.iter_sync_files(runs_dir)}
    prefix = "20260909-120000-solar"
    assert f"{prefix}/run.yaml" in files
    assert f"{prefix}/logs/01-solar.log" in files
    assert f"{prefix}/knowledge/live--solar.yaml" in files
    assert f"{prefix}/searches/solar/status.json" in files
    assert f"{prefix}/searches/solar/journal.jsonl" in files
    assert f"{prefix}/searches/solar/best/solution.py" in files
    assert files[f"{prefix}/searches/solar/candidates/c001/notes.md"] is False
    assert files[f"{prefix}/searches/solar/candidates/c001/agent_stream.jsonl"] is True
    assert not any("/data" in rel or "/problem" in rel or "/control/" in rel for rel in files)


def test_tail_bytes_cuts_at_a_line_boundary(tmp_path: Path) -> None:
    path = tmp_path / "stream.jsonl"
    path.write_text("".join(f'{{"i": {i}}}\n' for i in range(100)))
    tail = hillclimb.tail_bytes(path, limit=50)
    assert len(tail) <= 50
    assert tail.startswith(b'{"i": ') and tail.endswith(b"}\n")
    assert all(json.loads(line) for line in tail.decode().splitlines())
    assert hillclimb.tail_bytes(path, limit=10_000) == path.read_bytes()


def test_state_sync_uploads_changes_only_and_tails_streams(tmp_path: Path, monkeypatch) -> None:
    bucket = FakeBucket()
    monkeypatch.setattr(hillclimb, "_gcs_bucket", lambda name: bucket)
    runs_dir = tmp_path / "runs"
    run_dir = _run_tree(runs_dir, stream_bytes=hillclimb.TAIL_BYTES * 2)
    sync = hillclimb._StateSync("hc-bucket", "abc123", runs_dir)

    first = sync.sync_once()
    assert first >= 8
    stream = bucket.store["hillclimb/abc123/20260909-120000-solar/searches/solar/candidates/c001/agent_stream.jsonl"]
    assert len(stream) <= hillclimb.TAIL_BYTES and stream.startswith(b'{"i"')
    assert sync.sync_once() == 0  # nothing changed

    status = run_dir / "searches" / "solar" / "status.json"
    status.write_text(json.dumps({"state": "running", "cost_usd": 2.5}))
    os.utime(status, ns=(status.stat().st_atime_ns, status.stat().st_mtime_ns + 1_000_000))
    assert sync.sync_once(status_only=True) == 1
    assert (
        json.loads(bucket.store["hillclimb/abc123/20260909-120000-solar/searches/solar/status.json"])["cost_usd"] == 2.5
    )

    sync.write_manifest(target="emflow://gefcom2014:solar", parallel_searches=2)
    manifest = json.loads(bucket.store["hillclimb/abc123/hosted.json"])
    assert manifest == {
        "bucket": "hc-bucket",
        "sync_id": "abc123",
        "prefix": "hillclimb/abc123",
        "target": "emflow://gefcom2014:solar",
        "parallel_searches": 2,
    }


def test_state_sync_delivers_commands_to_running_searches_once(tmp_path: Path, monkeypatch) -> None:
    bucket = FakeBucket()
    monkeypatch.setattr(hillclimb, "_gcs_bucket", lambda name: bucket)
    runs_dir = tmp_path / "runs"
    run_dir = _run_tree(runs_dir)
    parked = run_dir / "searches" / "solar-2"
    parked.mkdir()
    (parked / "status.json").write_text(json.dumps({"state": "parked"}))
    sync = hillclimb._StateSync("hc-bucket", "abc123", runs_dir)
    command = json.dumps({"action": "stop", "reason": "enough", "source": "cli"}).encode()
    bucket.store["hillclimb/abc123/control/20260909T120500.000000-stop.json"] = command
    bucket.store["hillclimb/abc123/control/README"] = b"not a command"

    assert sync.pull_control() == ["20260909T120500.000000-stop.json"]
    delivered = run_dir / "searches" / "solar" / "control" / "20260909T120500.000000-stop.json"
    assert delivered.read_bytes() == command
    assert not (parked / "control").exists()
    assert sync.pull_control() == []  # seen


def test_hosted_config_uses_the_engine_field_names() -> None:
    config = hillclimb.hosted_config(
        backend="dummy",
        model="sonnet",
        policy="gepa",
        parallel_operators=2,
        subscription=True,
        max_cost_usd=10.0,
        emflow_source="/src/emflow",
    )
    assert config["search"] == {"parallel_operators": 2, "machine_max_operators": 0, "policy": "gepa"}
    assert config["backend_auth"] == "subscription"
    assert config["budget"] == {"max_cost_usd": 10.0}
    assert config["emflow"] == {"source": "/src/emflow"}
    assert "parallel_agents" not in json.dumps(config)
    minimal = hillclimb.hosted_config(
        backend=None,
        model=None,
        policy=None,
        parallel_operators=0,
        subscription=False,
        max_cost_usd=0,
        emflow_source=None,
    )
    assert minimal["backend_auth"] == "api-key" and minimal["search"]["parallel_operators"] == 1
    assert "backend" not in minimal and "emflow" not in minimal


def test_summarize_run_aggregates_every_search(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = _run_tree(runs_dir, state="done")
    (run_dir / "searches" / "solar" / "status.json").write_text(
        json.dumps(
            {
                "state": "done",
                "cost_usd": 1.5,
                "selected": {"candidate_id": "c003", "val_score": 0.5, "holdout_score": 0.4},
            }
        )
    )
    second = run_dir / "searches" / "solar-2"
    second.mkdir()
    (second / "status.json").write_text(
        json.dumps(
            {
                "state": "parked",
                "cost_usd": 2.0,
                "selected": {"candidate_id": "c009", "val_score": 0.9, "holdout_score": 0.7},
                "last_error": "rate limited",
            }
        )
    )

    result = hillclimb.summarize_run(run_dir, target="emflow://gefcom2014:solar", sync_id="abc123", bucket="hc-bucket")
    assert result["state"] == "parked"
    assert result["cost_usd"] == 3.5
    assert result["selected"]["candidate_id"] == "c009" and result["selected"]["search_ref"].endswith("/solar-2")
    assert [entry["state"] for entry in result["searches"]] == ["done", "parked"]
    assert result["error"] == "rate limited"
    assert result["gcs_prefix"] == "hillclimb/abc123"

    lower = hillclimb.summarize_run(run_dir, target="t", sync_id=None, bucket=None, higher_is_better=False)
    assert lower["selected"]["candidate_id"] == "c003" and lower["gcs_prefix"] is None


def test_request_stop_everywhere_writes_engine_shaped_commands(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = _run_tree(runs_dir)
    assert hillclimb.request_stop_everywhere(runs_dir, reason="platform SIGTERM") == 1
    (command,) = list((run_dir / "searches" / "solar" / "control").glob("*.json"))
    assert command.name.endswith("-stop.json")
    payload = json.loads(command.read_text())
    assert payload["action"] == "stop" and payload["source"] == "agent" and payload["reason"] == "platform SIGTERM"
    assert not list((run_dir / "searches" / "solar" / "control").glob("*.tmp"))


def test_stub_template_forwards_every_parameter() -> None:
    for name in (
        "policy",
        "parallel_searches",
        "parallel_operators",
        "holdout",
        "seed_solution_code",
        "knowledge_context",
    ):
        assert name in hillclimb.STUB_SOURCE
    namespace: dict = {}
    exec(hillclimb.STUB_SOURCE, namespace)
    assert callable(namespace["run"])
