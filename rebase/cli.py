from __future__ import annotations

import argparse
import importlib.util
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

from rebase.client import Function, Project, RebaseWorkflowError, Step, Workflow


def _load_module(path: Path) -> ModuleType:
    resolved_path = path.expanduser().resolve()
    if not resolved_path.exists():
        raise RebaseWorkflowError(f"file not found: {path}")
    if not resolved_path.is_file():
        raise RebaseWorkflowError(f"deploy target must be a Python file: {path}")

    module_name = f"_rebase_deploy_{abs(hash(resolved_path))}"
    spec = importlib.util.spec_from_file_location(module_name, resolved_path)
    if spec is None or spec.loader is None:
        raise RebaseWorkflowError(f"could not load Python file: {path}")

    module = importlib.util.module_from_spec(spec)
    parent = str(resolved_path.parent)
    sys.path.insert(0, parent)
    try:
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(parent)
    return module


def _unique_named_objects(module: ModuleType, types: tuple[type, ...]) -> list[tuple[str, Any]]:
    seen: set[int] = set()
    objects: list[tuple[str, Any]] = []
    for name, value in vars(module).items():
        if name.startswith("_") or not isinstance(value, types):
            continue
        object_id = id(value)
        if object_id in seen:
            continue
        seen.add(object_id)
        objects.append((name, value))
    return objects


def deploy_file(path: str | Path, *, object_names: Iterable[str] | None = None) -> list[tuple[str, str, str | None]]:
    module = _load_module(Path(path))
    selected_names = set(object_names or [])

    all_projects = _unique_named_objects(module, (Project,))
    projects = all_projects
    if selected_names:
        projects = [
            (name, project)
            for name, project in projects
            if name in selected_names or project.name in selected_names
        ]

    deployed: list[tuple[str, str, str | None]] = []
    if projects:
        for name, project in projects:
            project.deploy()
            deployed.append(("project", project.name or name, project.id))
        return deployed
    if all_projects and selected_names:
        raise RebaseWorkflowError(f"No matching Rebase project found for: {', '.join(sorted(selected_names))}")

    deployables = _unique_named_objects(module, (Workflow, Function))
    deployables = [(name, item) for name, item in deployables if not isinstance(item, Step)]
    if selected_names:
        deployables = [
            (name, item)
            for name, item in deployables
            if name in selected_names or getattr(item, "name", None) in selected_names
        ]
    if not deployables:
        raise RebaseWorkflowError(
            "No deployable Rebase objects found. Define a top-level rb.Project, rb.Workflow, or rb.Function."
        )

    for name, deployable in deployables:
        deployable.deploy()
        deployed.append((deployable.__class__.__name__.lower(), deployable.name or name, deployable.id))
    return deployed


def _deploy(args: argparse.Namespace) -> int:
    deployed = deploy_file(args.file, object_names=args.name)
    for target_type, name, target_id in deployed:
        suffix = f" ({target_id})" if target_id else ""
        print(f"Deployed {target_type} {name}{suffix}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rebase")
    subparsers = parser.add_subparsers(dest="command", required=True)

    deploy = subparsers.add_parser("deploy", help="Deploy Rebase objects from a Python file.")
    deploy.add_argument("file", help="Python file containing a top-level Rebase Project, Workflow, or Function.")
    deploy.add_argument(
        "--name",
        action="append",
        help="Deploy only the top-level variable name or Rebase target name. Can be passed more than once.",
    )
    deploy.set_defaults(func=_deploy)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RebaseWorkflowError as exc:
        parser.exit(1, f"rebase: error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
