from __future__ import annotations

import fnmatch
import gzip
import hashlib
import importlib.util
import inspect
import io
import json
import os
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import FunctionType
from typing import Any

from rebase.image import Image, ImageOperation

MAX_FILES = 25_000
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_BUNDLE_BYTES = 250 * 1024 * 1024
_BUILTIN_IGNORES = (
    ".git",
    ".git/**",
    ".venv",
    ".venv/**",
    "venv",
    "venv/**",
    "__pycache__",
    "**/__pycache__/**",
    ".pytest_cache",
    ".pytest_cache/**",
    ".mypy_cache",
    ".mypy_cache/**",
    ".ruff_cache",
    ".ruff_cache/**",
    ".env",
    "**/.env",
    ".DS_Store",
    "**/.DS_Store",
)


@dataclass(frozen=True)
class BundleFile:
    archive_path: str
    source_path: Path
    digest: str
    size: int
    mode: int


@dataclass(frozen=True)
class SourceBundle:
    digest: str
    manifest: dict[str, Any]
    archive: bytes
    entrypoint_module: str
    entrypoint_qualname: str

    @property
    def expanded_size(self) -> int:
        return int(self.manifest["expanded_size"])

    @property
    def file_count(self) -> int:
        return int(self.manifest["file_count"])


def build_source_bundle(fn: FunctionType, image: Image) -> SourceBundle:
    if image.python_version not in {"3.12", "3.13"}:
        raise ValueError("built Rebase images currently support Python 3.12 and 3.13")
    if "<locals>" in fn.__qualname__:
        raise ValueError("built-image targets must be module-level functions")
    source_file = inspect.getsourcefile(fn)
    if source_file is None:
        raise ValueError("built-image targets must be defined in an importable Python file")
    source_path = Path(source_file).resolve()
    if source_path.suffix != ".py" or not source_path.is_file():
        raise ValueError("built-image targets must be defined in a .py file")

    module_name, automatic_root, automatic_destination = _entrypoint_source(fn, source_path)
    files: dict[str, BundleFile] = {}
    runtime_mounts: list[dict[str, Any]] = []
    build_inputs: list[dict[str, Any]] = []
    recipe_operations: list[dict[str, Any]] = []

    _add_tree(
        files,
        automatic_root,
        automatic_destination,
        ignore=(),
    )
    runtime_mounts.append({"kind": "python", "archive_path": "python", "remote_path": None})

    for index, operation in enumerate(image.operations):
        if operation.op == "uv_pip_install":
            recipe_operations.append(operation.declaration())
        elif operation.op == "uv_sync":
            recipe_operations.append(_add_uv_project(files, operation, index, build_inputs))
        elif operation.op == "add_local_python_source":
            recipe_operations.append(_add_python_sources(files, operation, index, runtime_mounts, build_inputs))
        elif operation.op in {"add_local_file", "add_local_dir"}:
            recipe_operations.append(_add_local_path(files, operation, index, runtime_mounts, build_inputs))

    ordered_files = sorted(files.values(), key=lambda item: item.archive_path)
    expanded_size = sum(item.size for item in ordered_files)
    if len(ordered_files) > MAX_FILES:
        raise ValueError(f"source bundle contains {len(ordered_files)} files; maximum is {MAX_FILES}")
    if expanded_size > MAX_BUNDLE_BYTES:
        raise ValueError(f"source bundle expands to {expanded_size} bytes; maximum is {MAX_BUNDLE_BYTES} bytes")

    file_manifest = [
        {
            "path": item.archive_path,
            "digest": item.digest,
            "size": item.size,
            "mode": item.mode,
        }
        for item in ordered_files
    ]
    manifest_without_digest: dict[str, Any] = {
        "schema_version": 1,
        "entrypoint": {"module": module_name, "qualname": fn.__qualname__},
        "files": file_manifest,
        "file_count": len(file_manifest),
        "expanded_size": expanded_size,
        "runtime_mounts": _unique_dicts(runtime_mounts),
        "build_inputs": build_inputs,
        "image_recipe": {
            "schema_version": 1,
            "base": {"kind": image.kind, "python_version": image.python_version},
            "operations": recipe_operations,
        },
    }
    digest = "sha256:" + hashlib.sha256(_canonical(manifest_without_digest)).hexdigest()
    manifest = {**manifest_without_digest, "digest": digest}
    archive = _archive(ordered_files, manifest)
    return SourceBundle(digest, manifest, archive, module_name, fn.__qualname__)


def _entrypoint_source(fn: FunctionType, source_path: Path) -> tuple[str, Path, str]:
    package_dir = source_path.parent
    while (package_dir / "__init__.py").is_file():
        package_dir = package_dir.parent
    if package_dir != source_path.parent:
        top_package = next(part for part in source_path.relative_to(package_dir).parts)
        root = package_dir / top_package
        module = fn.__module__ if fn.__module__ != "__main__" else _module_from_relative(source_path, package_dir)
        return module, root, f"python/{top_package}"

    project_root = _project_root(source_path.parent)
    if fn.__module__ != "__main__":
        module_parts = fn.__module__.split(".")
        if not all(part.isidentifier() for part in module_parts):
            raise ValueError(f"cannot derive an importable module path from {fn.__module__!r}")
        return fn.__module__, source_path, f"python/{'/'.join(module_parts)}.py"
    try:
        relative = source_path.relative_to(project_root)
    except ValueError:
        relative = Path(source_path.name)
        project_root = source_path.parent
    module = fn.__module__ if fn.__module__ != "__main__" else _module_from_relative(source_path, project_root)
    return module, source_path, f"python/{relative.as_posix()}"


def _project_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists() or (candidate / "pyproject.toml").is_file():
            return candidate
    return Path.cwd().resolve()


def _module_from_relative(source_path: Path, root: Path) -> str:
    relative = source_path.relative_to(root).with_suffix("")
    if not all(part.isidentifier() for part in relative.parts):
        raise ValueError(f"cannot derive an importable module name from {relative}")
    return ".".join(relative.parts)


def _add_uv_project(
    files: dict[str, BundleFile],
    operation: ImageOperation,
    index: int,
    build_inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    root = Path(operation.local_path or ".").expanduser().resolve()
    pyproject = root / "pyproject.toml"
    lockfile = root / "uv.lock"
    if not pyproject.is_file() or not lockfile.is_file():
        raise ValueError(f"uv_sync requires pyproject.toml and uv.lock in {root}")
    prefix = f"build/uv/{index}"
    _add_file(files, pyproject, f"{prefix}/pyproject.toml")
    _add_file(files, lockfile, f"{prefix}/uv.lock")
    item = {"kind": "uv_project", "archive_path": prefix}
    build_inputs.append(item)
    return {
        "op": "uv_sync",
        "input": prefix,
        "frozen": operation.frozen,
        "groups": list(operation.groups),
        "extras": list(operation.extras),
    }


def _add_python_sources(
    files: dict[str, BundleFile],
    operation: ImageOperation,
    index: int,
    runtime_mounts: list[dict[str, Any]],
    build_inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    resolved: list[dict[str, str]] = []
    for module_name in operation.values:
        spec = importlib.util.find_spec(module_name)
        if spec is None:
            raise ValueError(f"cannot find local Python module {module_name!r}")
        top_name = module_name.split(".", 1)[0]
        top_spec = importlib.util.find_spec(top_name)
        if top_spec is None:
            raise ValueError(f"cannot find top-level Python package {top_name!r}")
        if top_spec.submodule_search_locations:
            locations = list(top_spec.submodule_search_locations)
            if len(locations) != 1:
                raise ValueError(f"namespace package {top_name!r} has multiple roots; use add_local_dir instead")
            source = Path(locations[0]).resolve()
            destination = f"{'build/python' if operation.copy else 'python'}/{top_name}"
        elif top_spec.origin:
            source = Path(top_spec.origin).resolve()
            destination = f"{'build/python' if operation.copy else 'python'}/{top_name}.py"
        else:
            raise ValueError(f"module {top_name!r} has no local filesystem source")
        if "site-packages" in source.parts or "dist-packages" in source.parts:
            raise ValueError(f"{module_name!r} is installed; declare it with uv_sync or uv_pip_install")
        before = len(files)
        _add_tree(files, source, destination, ignore=())
        if len(files) == before and not any(path.startswith(f"{destination}/") for path in files):
            raise ValueError(f"local Python source is empty: {source}")
        resolved.append({"module": module_name, "archive_path": destination})

    if operation.copy:
        build_inputs.extend({"kind": "python", **item} for item in resolved)
    else:
        runtime_mounts.append({"kind": "python", "archive_path": "python", "remote_path": None})
    return {"op": operation.op, "copy": operation.copy, "sources": resolved}


def _add_local_path(
    files: dict[str, BundleFile],
    operation: ImageOperation,
    index: int,
    runtime_mounts: list[dict[str, Any]],
    build_inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    source = Path(operation.local_path or "").expanduser().resolve()
    expected_dir = operation.op == "add_local_dir"
    if expected_dir and not source.is_dir():
        raise ValueError(f"local directory does not exist: {source}")
    if not expected_dir and not source.is_file():
        raise ValueError(f"local file does not exist: {source}")
    prefix = f"{'build/rootfs' if operation.copy else 'runtime/rootfs'}/{index}"
    destination = operation.remote_path or "/"
    before = len(files)
    _add_tree(files, source, prefix, ignore=operation.ignore)
    if len(files) == before:
        raise ValueError(f"local source has no included files: {source}")
    item = {
        "kind": "dir" if expected_dir else "file",
        "archive_path": prefix,
        "remote_path": destination,
    }
    (build_inputs if operation.copy else runtime_mounts).append(item)
    return {"op": operation.op, "copy": operation.copy, **item}


def _add_tree(
    files: dict[str, BundleFile],
    source: Path,
    destination: str,
    *,
    ignore: tuple[str, ...],
) -> None:
    if source.is_symlink():
        raise ValueError(f"symlinks are not supported in source bundles: {source}")
    if source.is_file():
        _add_file(files, source, destination)
        return
    if not source.is_dir():
        raise ValueError(f"local source does not exist: {source}")
    patterns = (
        *_BUILTIN_IGNORES,
        *_ignore_file(source / ".gitignore"),
        *_ignore_file(source / ".rebaseignore"),
        *ignore,
    )
    for root, directories, names in os.walk(source):
        root_path = Path(root)
        relative_root = root_path.relative_to(source)
        included_directories: list[str] = []
        for directory in sorted(directories):
            path = root_path / directory
            relative = (relative_root / directory).as_posix()
            if _ignored(relative, patterns):
                continue
            if path.is_symlink():
                raise ValueError(f"symlinks are not supported in source bundles: {path}")
            included_directories.append(directory)
        directories[:] = included_directories
        for name in sorted(names):
            path = root_path / name
            relative = (relative_root / name).as_posix()
            if _ignored(relative, patterns):
                continue
            if path.is_symlink():
                raise ValueError(f"symlinks are not supported in source bundles: {path}")
            _add_file(files, path, f"{destination.rstrip('/')}/{relative}")


def _add_file(files: dict[str, BundleFile], source: Path, archive_path: str) -> None:
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"source bundle entries must be regular files: {source}")
    path = PurePosixPath(archive_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"invalid source bundle path: {archive_path}")
    size = source.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(f"source file {source} is {size} bytes; maximum is {MAX_FILE_BYTES} bytes")
    digest = "sha256:" + _file_digest(source)
    normalized = path.as_posix()
    item = BundleFile(normalized, source, digest, size, 0o755 if os.access(source, os.X_OK) else 0o644)
    existing = files.get(normalized)
    if existing is not None and existing.digest != item.digest:
        raise ValueError(f"source declarations collide at {normalized}")
    files[normalized] = item


def _ignored(path: str, patterns: Iterable[str]) -> bool:
    ignored = False
    for raw_pattern in patterns:
        pattern = raw_pattern.strip()
        if not pattern or pattern.startswith("#"):
            continue
        negate = pattern.startswith("!")
        pattern = pattern[1:] if negate else pattern
        pattern = pattern.lstrip("/").rstrip("/")
        matched = fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, f"**/{pattern}")
        if matched:
            ignored = not negate
    return ignored


def _ignore_file(path: Path) -> tuple[str, ...]:
    if not path.is_file():
        return ()
    return tuple(path.read_text(encoding="utf-8").splitlines())


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive(files: list[BundleFile], manifest: dict[str, Any]) -> bytes:
    output = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=output, mode="wb", mtime=0, filename="") as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        manifest_bytes = _canonical(manifest)
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_info.size = len(manifest_bytes)
        manifest_info.mode = 0o644
        manifest_info.mtime = 0
        manifest_info.uid = manifest_info.gid = 0
        manifest_info.uname = manifest_info.gname = ""
        archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
        for item in files:
            info = tarfile.TarInfo(item.archive_path)
            info.size = item.size
            info.mode = item.mode
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with item.source_path.open("rb") as handle:
                archive.addfile(info, handle)
    return output.getvalue()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _unique_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[bytes] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = _canonical(item)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result
