from __future__ import annotations

import json
import re
import warnings
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

DEFAULT_PYTHON_VERSION = "3.13"
IMAGE_RECIPE_SCHEMA_VERSION = 1


def _is_pinned_dependency(package: str) -> bool:
    return "==" in package or re.search(r"git\+.*\.git@[a-f0-9]{7,40}(?:$|#)", package.strip().lower()) is not None


def _clean_packages(packages: tuple[str, ...]) -> tuple[str, ...]:
    cleaned = tuple(package.strip() for package in packages if package.strip())
    unpinned = [package for package in cleaned if not _is_pinned_dependency(package)]
    if unpinned:
        warnings.warn(
            "Unpinned Rebase function dependencies are allowed but make function versions less reproducible. "
            f"Prefer exact pins for: {', '.join(unpinned)}",
            stacklevel=3,
        )
    return cleaned


@dataclass(frozen=True)
class ImageOperation:
    op: Literal["uv_pip_install", "uv_sync", "add_local_file", "add_local_dir", "add_local_python_source"]
    values: tuple[str, ...] = ()
    local_path: str | None = None
    remote_path: str | None = None
    copy: bool = False
    ignore: tuple[str, ...] = ()
    frozen: bool = True
    groups: tuple[str, ...] = ()
    extras: tuple[str, ...] = ()
    uv_version: str | None = None

    def declaration(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "op": self.op,
                "values": list(self.values) or None,
                "local_path": self.local_path,
                "remote_path": self.remote_path,
                "copy": self.copy if self.op.startswith("add_local_") else None,
                "ignore": list(self.ignore) or None,
                "frozen": self.frozen if self.op == "uv_sync" else None,
                "groups": list(self.groups) or None,
                "extras": list(self.extras) or None,
                "uv_version": self.uv_version,
            }.items()
            if value is not None
        }


@dataclass(frozen=True)
class Image:
    """An immutable, Modal-style declaration of a Rebase Python image."""

    kind: str = "python"
    python_version: str = DEFAULT_PYTHON_VERSION
    operations: tuple[ImageOperation, ...] = ()

    def __post_init__(self) -> None:
        if self.kind != "python":
            raise ValueError("only python images are supported")
        if not self.python_version.strip():
            raise ValueError("python version cannot be empty")

    @classmethod
    def python(cls, version: str = DEFAULT_PYTHON_VERSION) -> Image:
        return cls(python_version=version.strip())

    def _append(self, operation: ImageOperation) -> Image:
        return replace(self, operations=(*self.operations, operation))

    def uv_pip_install(self, *packages: str, uv_version: str | None = None) -> Image:
        cleaned = _clean_packages(tuple(packages))
        if not cleaned:
            return self
        return self._append(ImageOperation("uv_pip_install", values=cleaned, uv_version=uv_version))

    def uv_sync(
        self,
        local_path: str | Path = ".",
        *,
        frozen: bool = True,
        groups: list[str] | tuple[str, ...] | None = None,
        extras: list[str] | tuple[str, ...] | None = None,
    ) -> Image:
        return self._append(
            ImageOperation(
                "uv_sync",
                local_path=str(local_path),
                frozen=frozen,
                groups=tuple(groups or ()),
                extras=tuple(extras or ()),
            )
        )

    def add_local_file(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        copy: bool = False,
    ) -> Image:
        destination = _remote_path(remote_path)
        if destination == "/":
            raise ValueError("add_local_file remote_path must name a file, not the filesystem root")
        return self._append(
            ImageOperation(
                "add_local_file",
                local_path=str(local_path),
                remote_path=destination,
                copy=copy,
            )
        )

    def add_local_dir(
        self,
        local_path: str | Path,
        remote_path: str,
        *,
        copy: bool = False,
        ignore: list[str] | tuple[str, ...] | None = None,
    ) -> Image:
        return self._append(
            ImageOperation(
                "add_local_dir",
                local_path=str(local_path),
                remote_path=_remote_path(remote_path),
                copy=copy,
                ignore=tuple(ignore or ()),
            )
        )

    def add_local_python_source(self, *module_names: str, copy: bool = False) -> Image:
        names = tuple(name.strip() for name in module_names if name.strip())
        if not names:
            raise ValueError("add_local_python_source requires at least one module name")
        return self._append(ImageOperation("add_local_python_source", values=names, copy=copy))

    @property
    def build_enabled(self) -> bool:
        return any(operation.op == "uv_sync" or operation.op.startswith("add_local_") for operation in self.operations)

    @property
    def uv_pip_packages(self) -> list[str]:
        return [
            value for operation in self.operations if operation.op == "uv_pip_install" for value in operation.values
        ]

    @property
    def uv_version(self) -> str | None:
        versions = [operation.uv_version for operation in self.operations if operation.uv_version is not None]
        return versions[-1] if versions else None

    def legacy_spec(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "python_version": self.python_version,
            "uv_pip_packages": self.uv_pip_packages,
            "uv_version": self.uv_version,
        }

    def to_dict(self) -> dict[str, Any]:
        if not self.build_enabled:
            return self.legacy_spec()
        return {
            "schema_version": IMAGE_RECIPE_SCHEMA_VERSION,
            "base": {"kind": self.kind, "python_version": self.python_version},
            "operations": [operation.declaration() for operation in self.operations],
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def _remote_path(value: str) -> str:
    path = value.strip()
    if not path.startswith("/"):
        raise ValueError("remote_path must be absolute")
    normalized = "/" + "/".join(part for part in path.split("/") if part)
    if any(part == ".." for part in path.split("/")):
        raise ValueError("remote_path cannot contain '..'")
    protected = (
        "/proc",
        "/sys",
        "/dev",
        "/opt/rebase",
        "/mnt/rebase",
        "/app/.venv",
        "/app/app",
        "/app/python",
    )
    if any(normalized == prefix or normalized.startswith(f"{prefix}/") for prefix in protected):
        raise ValueError(f"remote_path is reserved by Rebase: {normalized}")
    return normalized
