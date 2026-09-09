"""The client-side mirror of a hosted search: pulls changed objects through
the API into a hillclimb-dir layout, pushes the TUI's control files back."""

from __future__ import annotations

import json
from pathlib import Path

from rebase.hillclimb_mirror import Mirror, sync_id_of


class FakeClient:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.downloads: list[str] = []
        self.controls: list[dict] = []

    def list_hillclimb_objects(self, run_id):
        return {
            "run_id": run_id,
            "prefix": "hillclimb/abc/",
            "objects": [
                {"path": path, "size": len(body), "generation": gen} for path, (body, gen) in self.objects.items()
            ],
        }

    def get_hillclimb_object(self, run_id, path, *, etag=None):
        self.downloads.append(path)
        body, gen = self.objects[path]
        return body, f'"{gen}"'

    def send_hillclimb_control(self, run_id, *, action, candidate_id=None, reason="", source="cli"):
        self.controls.append(
            {"run_id": run_id, "action": action, "candidate_id": candidate_id, "reason": reason, "source": source}
        )
        return {"path": f"control/x-{action}.json"}


def test_pull_writes_a_hillclimb_dir_and_refetches_only_changed_generations(tmp_path: Path) -> None:
    client = FakeClient()
    client.objects = {
        "hosted.json": (json.dumps({"target": "emflow://x"}).encode(), "1"),
        "run-1/run.yaml": (b"run_id: run-1\n", "3"),
        "run-1/searches/solar/status.json": (json.dumps({"state": "running", "pid": 4242}).encode(), "7"),
        "control/20260909T1-stop.json": (b"{}", "9"),  # commands never come back down
    }
    mirror = Mirror(client, "run-uuid", sync_id="abc", root=tmp_path, poll_s=0.01, push_s=0.01)
    mirror.prepare()

    assert mirror.pull_once() == 3
    assert (mirror.hillclimb_dir / "config.yaml").exists()
    assert (mirror.runs_dir / "run-1" / "run.yaml").read_bytes() == b"run_id: run-1\n"
    assert json.loads((mirror.runs_dir / "run-1" / "searches" / "solar" / "status.json").read_text())["pid"] == 4242
    assert mirror.manifest == {"target": "emflow://x"}
    assert not (mirror.runs_dir / "control").exists()
    assert not list(mirror.runs_dir.rglob("*.mirror-tmp"))
    assert mirror.engine_env() == {"HILLCLIMB_DIR": str(mirror.hillclimb_dir), "HILLCLIMB_REMOTE_STATE": "1"}

    assert mirror.pull_once() == 0
    client.objects["run-1/searches/solar/status.json"] = (json.dumps({"state": "parked"}).encode(), "8")
    assert mirror.pull_once() == 1
    assert client.downloads.count("run-1/searches/solar/status.json") == 2
    assert client.downloads.count("run-1/run.yaml") == 1


def test_push_forwards_tui_commands_and_drains_them(tmp_path: Path) -> None:
    client = FakeClient()
    mirror = Mirror(client, "run-uuid", sync_id="abc", root=tmp_path)
    mirror.prepare()
    control = mirror.runs_dir / "run-1" / "searches" / "solar" / "control"
    control.mkdir(parents=True)
    (control / "20260909T120000.000001-prune-c004.json").write_text(
        json.dumps({"action": "prune", "candidate_id": "c004", "reason": "dead end", "source": "tui"})
    )
    (control / "20260909T120000.000002-stop.json").write_text(json.dumps({"action": "stop"}))
    (control / "garbage.json").write_text("{not json")

    assert mirror.push_once() == 2
    assert [c["action"] for c in client.controls] == ["prune", "stop"]
    assert client.controls[0]["candidate_id"] == "c004" and client.controls[0]["source"] == "tui"
    assert client.controls[1]["source"] == "tui"
    assert list(control.iterdir()) == []


def test_start_and_stop_run_the_threads(tmp_path: Path) -> None:
    client = FakeClient()
    client.objects = {"run-1/run.yaml": (b"x", "1")}
    mirror = Mirror(client, "run-uuid", sync_id="abc", root=tmp_path, poll_s=0.01, push_s=0.01)
    mirror.start()
    assert (mirror.runs_dir / "run-1" / "run.yaml").exists()
    mirror.stop()
    assert mirror.last_error is None


def test_sync_id_of_reads_run_parameters() -> None:
    assert sync_id_of({"id": "r", "parameters": {"sync_id": "abc"}}) == "abc"
    try:
        sync_id_of({"id": "r", "parameters": {}})
    except Exception as exc:  # noqa: BLE001
        assert "not a hillclimb search" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected an error")
