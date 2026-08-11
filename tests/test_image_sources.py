from __future__ import annotations

import importlib
import io
import sys
import tarfile
from pathlib import Path

import pytest

from rebase import Image
from rebase.source_bundle import build_source_bundle


def _module_function(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    package_name = f"bundle_pkg_{tmp_path.name.replace('-', '_')}"
    package = tmp_path / package_name
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "ignored.txt").write_text("ignore me", encoding="utf-8")
    (package / ".rebaseignore").write_text("ignored.txt\n", encoding="utf-8")
    (package / "job.py").write_text(
        "def run(value=1):\n    return {'value': value}\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    module = importlib.import_module(f"{package_name}.job")
    return module.run, package_name


def test_image_builder_is_immutable_and_legacy_pip_stays_legacy() -> None:
    base = Image.python("3.13")
    dependencies = base.uv_pip_install("httpx==0.28.1")

    assert base.operations == ()
    assert dependencies is not base
    assert dependencies.build_enabled is False
    assert dependencies.to_dict() == {
        "kind": "python",
        "python_version": "3.13",
        "uv_pip_packages": ["httpx==0.28.1"],
        "uv_version": None,
    }


def test_source_bundle_is_deterministic_and_captures_defining_package(tmp_path, monkeypatch) -> None:
    fn, package_name = _module_function(tmp_path, monkeypatch)
    data = tmp_path / "settings.json"
    data.write_text('{"enabled": true}', encoding="utf-8")
    image = Image.python("3.13").add_local_file(data, "/workspace/settings.json")

    first = build_source_bundle(fn, image)
    second = build_source_bundle(fn, image)

    assert first.digest == second.digest
    assert first.archive == second.archive
    assert first.entrypoint_module == f"{package_name}.job"
    assert first.entrypoint_qualname == "run"
    paths = {item["path"] for item in first.manifest["files"]}
    assert f"python/{package_name}/job.py" in paths
    assert f"python/{package_name}/ignored.txt" not in paths
    assert "runtime/rootfs/0" in paths
    assert {
        "kind": "file",
        "archive_path": "runtime/rootfs/0",
        "remote_path": "/workspace/settings.json",
    } in first.manifest["runtime_mounts"]

    with tarfile.open(fileobj=io.BytesIO(first.archive), mode="r:gz") as archive:
        assert {member.name for member in archive.getmembers()} == {"manifest.json", *paths}


def test_uv_sync_and_copy_sources_are_build_inputs(tmp_path, monkeypatch) -> None:
    fn, _ = _module_function(tmp_path, monkeypatch)
    project = tmp_path / "uv-project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='example'\nversion='1.0.0'\n", encoding="utf-8")
    (project / "uv.lock").write_text("version = 1\nrevision = 3\n", encoding="utf-8")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "model.bin").write_bytes(b"model")
    image = Image.python("3.12").uv_sync(project).add_local_dir(assets, "/workspace/assets", copy=True)

    bundle = build_source_bundle(fn, image)

    assert {item["path"] for item in bundle.manifest["files"]} >= {
        "build/uv/0/pyproject.toml",
        "build/uv/0/uv.lock",
        "build/rootfs/1/model.bin",
    }
    assert [operation["op"] for operation in bundle.manifest["image_recipe"]["operations"]] == [
        "uv_sync",
        "add_local_dir",
    ]


def test_source_paths_are_validated(tmp_path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        Image.python().add_local_dir(tmp_path, "relative")
    with pytest.raises(ValueError, match="reserved"):
        Image.python().add_local_file(tmp_path / "x", "/mnt/rebase/x")
    with pytest.raises(ValueError, match="filesystem root"):
        Image.python().add_local_file(tmp_path / "x", "/")


def test_built_images_reject_unsupported_python(tmp_path, monkeypatch) -> None:
    fn, _ = _module_function(tmp_path, monkeypatch)
    image = Image.python("3.11").add_local_dir(tmp_path, "/workspace/source")

    with pytest.raises(ValueError, match="3.12 and 3.13"):
        build_source_bundle(fn, image)

    sys.modules.pop(fn.__module__, None)
