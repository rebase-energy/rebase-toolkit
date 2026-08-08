"""Find the local Python file that declares a Rebase project.

A project is a platform record: it carries a name and a remote repo coordinate, but
never a path on this machine. Deploy-time metadata records where the declaring file
sat at the last deploy, which goes stale the moment the file moves and is missing
entirely for files that were never deployed from here. So this module searches the
filesystem instead, which is always current.

The search is deliberately cheap enough to re-run on every request rather than
cached: a byte-level prefilter means only a handful of files are ever parsed.

Nothing here imports or executes candidate files. Declarations are read out of the
AST, because importing a module to ask its project's name would run arbitrary code
and require its dependencies to be installed.
"""

from __future__ import annotations

import ast
import os
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

#: Directory names never worth walking into. `pyvenv.cfg` catches the rest (see
#: `_is_virtualenv`) — users name virtualenvs `.venv313` or `sandbox` and a static
#: list alone misses those by tens of thousands of files.
PRUNED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        ".env",
        "node_modules",
        "__pycache__",
        "site-packages",
        "dist",
        "build",
        ".tox",
        ".nox",
        ".eggs",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".ipynb_checkpoints",
    }
)

#: Both needles are required: `project(` is not a substring of `Project(`. The
#: imperative `ensure_project(` contains `project(`, so it comes along for free.
PREFILTER_NEEDLES: tuple[bytes, ...] = (b"project(", b"Project(")

#: Callables that declare a project, by attribute/function name.
PROJECT_CALL_NAMES = frozenset({"project", "ensure_project", "Project"})

DEFAULT_MAX_FILE_BYTES = 2_000_000
DEFAULT_MAX_FILES = 20_000

#: Sentinel for "this imported name is never reassigned", so every call qualifies.
_NEVER_REBOUND = 1 << 62


@dataclass(frozen=True)
class ProjectDeclaration:
    """A `rb.project(...)` call resolved to a name and a place in a file."""

    project_name: str
    path: Path
    line: int
    column: int


@dataclass(frozen=True)
class LocateResult:
    project_name: str
    matches: tuple[ProjectDeclaration, ...] = ()
    roots: tuple[Path, ...] = ()
    files_scanned: int = 0
    files_parsed: int = 0
    #: Files that declare a project whose name is only knowable at runtime. Reported
    #: so a failed search can say *why* rather than just "nothing found".
    unresolved: tuple[Path, ...] = ()
    truncated: bool = False
    errors: tuple[str, ...] = field(default=())

    @property
    def status(self) -> Literal["found", "ambiguous", "not-found", "no-roots"]:
        if not self.roots:
            return "no-roots"
        if len(self.matches) == 1:
            return "found"
        if len(self.matches) > 1:
            return "ambiguous"
        return "not-found"


def _is_virtualenv(path: str) -> bool:
    return os.path.isfile(os.path.join(path, "pyvenv.cfg"))


def iter_python_files(
    roots: Iterable[Path],
    *,
    pruned: frozenset[str] = PRUNED_DIR_NAMES,
    on_error: Callable[[str], None] | None = None,
) -> Iterator[Path]:
    """Yield every `.py` file under `roots`, each real file exactly once.

    Overlapping and symlinked roots are common (a repo plus a subdirectory of it),
    so files are de-duplicated by real path rather than by the path used to reach
    them. Unreadable directories are reported through `on_error`, never raised.
    """
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            if on_error is not None:
                on_error(f"{root} is not a directory")
            continue

        def handle(error: OSError) -> None:
            if on_error is not None:
                on_error(f"{error.filename}: {error.strerror}")

        for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=handle):
            dirnames[:] = [
                name for name in dirnames if name not in pruned and not _is_virtualenv(os.path.join(dirpath, name))
            ]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                candidate = os.path.join(dirpath, filename)
                real = os.path.realpath(candidate)
                if real in seen:
                    continue
                seen.add(real)
                yield Path(candidate)


def _module_level_strings(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"` bindings, last one winning.

    Only module level: a name used as a project's identity is a module constant in
    every real deployment file, and a function-local binding would not be in scope
    at the declaration anyway.
    """
    bindings: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        bindings[target.id] = node.value.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            bindings[node.target.id] = node.value.value
    return bindings


def _module_level_rebind_lines(tree: ast.Module) -> dict[str, int]:
    """The last line on which each module-level name still holds its imported value.

    Keyed to the *first* assignment, because that is where an imported name stops
    referring to the import. The assignment's end line is used rather than its start
    so that a call on the right-hand side — which is evaluated before the binding
    takes effect — still counts as the imported callable.
    """
    rebinds: dict[str, int] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id not in rebinds:
                rebinds[target.id] = node.end_lineno or node.lineno
    return rebinds


def _bare_call_names(tree: ast.Module) -> dict[str, int]:
    """Names that `from rebase import project` made callable, and until which line.

    The standard idiom rebinds the name it imported — `project = project(NAME)` —
    so the name cannot simply be trusted or distrusted for the whole file. The call
    on the right of that assignment is a declaration; every `project(...)` after it
    is a method on the returned object and must not be counted as a second one.
    """
    imported: dict[str, int] = {}
    rebinds = _module_level_rebind_lines(tree)
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if module != "rebase" and not module.startswith("rebase."):
            continue
        for alias in node.names:
            if alias.name in PROJECT_CALL_NAMES:
                name = alias.asname or alias.name
                imported[name] = rebinds.get(name, _NEVER_REBOUND)
    return imported


def _declaration_argument(node: ast.Call) -> ast.expr | None:
    if node.args:
        return node.args[0]
    for keyword in node.keywords:
        if keyword.arg == "name":
            return keyword.value
    return None


def declarations_in_source(source: str | bytes, path: Path) -> tuple[list[ProjectDeclaration], bool]:
    """Parse one file. Returns its declarations and whether any name was unresolvable.

    Bytes are passed straight to `ast.parse`, which honours PEP 263 encoding
    declarations; decoding first with a replacement character could turn valid
    source into a SyntaxError.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return [], False

    constants = _module_level_strings(tree)
    bare_names = _bare_call_names(tree)

    declarations: list[ProjectDeclaration] = []
    unresolved = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            # Permissive about the receiver on purpose: `rb`, `rebase`, `client` and
            # any local alias all appear in the wild, and the name check below is
            # what actually filters.
            if func.attr not in PROJECT_CALL_NAMES:
                continue
        elif isinstance(func, ast.Name):
            if node.lineno > bare_names.get(func.id, 0):
                continue
        else:
            continue

        argument = _declaration_argument(node)
        name: str | None = None
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            name = argument.value
        elif isinstance(argument, ast.Name):
            name = constants.get(argument.id)

        if name is None:
            # An f-string, an env lookup, a settings attribute. Recorded rather than
            # guessed at, so a fruitless search can explain itself.
            unresolved = True
            continue
        declarations.append(ProjectDeclaration(project_name=name, path=path, line=node.lineno, column=node.col_offset))

    return declarations, unresolved


def scan_file(path: Path, *, max_bytes: int = DEFAULT_MAX_FILE_BYTES) -> tuple[list[ProjectDeclaration], bool] | None:
    """Scan one file, or return None if the byte prefilter ruled it out unparsed."""
    try:
        if path.stat().st_size > max_bytes:
            return None
        blob = path.read_bytes()
    except OSError:
        return None
    if not any(needle in blob for needle in PREFILTER_NEEDLES):
        return None
    return declarations_in_source(blob, path)


def find_project_declarations(
    project_name: str,
    roots: Sequence[Path],
    *,
    max_files: int = DEFAULT_MAX_FILES,
) -> LocateResult:
    """Search `roots` for the file(s) declaring `project_name`."""
    errors: list[str] = []
    matches: list[ProjectDeclaration] = []
    unresolved: list[Path] = []
    files_scanned = 0
    files_parsed = 0
    truncated = False

    for path in iter_python_files(roots, on_error=errors.append):
        if files_scanned >= max_files:
            truncated = True
            break
        files_scanned += 1
        scanned = scan_file(path)
        if scanned is None:
            continue
        files_parsed += 1
        declarations, had_unresolved = scanned
        if had_unresolved:
            unresolved.append(path)
        hits = [item for item in declarations if item.project_name == project_name]
        if hits:
            # One file declaring the same project twice is a quirk, not a choice to
            # put to the user: keep the first declaration and move on.
            matches.append(min(hits, key=lambda item: item.line))

    matches.sort(key=lambda item: (str(item.path), item.line))
    return LocateResult(
        project_name=project_name,
        matches=tuple(matches),
        roots=tuple(roots),
        files_scanned=files_scanned,
        files_parsed=files_parsed,
        unresolved=tuple(unresolved),
        truncated=truncated,
        errors=tuple(errors),
    )


def describe_failure(result: LocateResult) -> str:
    """A message explaining a search that produced nothing usable.

    Shared by the TUI and the CLI so both say the same thing.
    """
    if result.status == "no-roots":
        return "no search paths are configured for this workspace. Add one with: rebase project search-path add <dir>"

    roots = ", ".join(str(root) for root in result.roots)
    parts = [
        f'no file declaring project "{result.project_name}" was found under {roots} '
        f"({result.files_scanned} Python files searched)."
    ]
    if result.unresolved:
        listed = ", ".join(str(path) for path in result.unresolved[:3])
        more = f" and {len(result.unresolved) - 3} more" if len(result.unresolved) > 3 else ""
        parts.append(
            f"{len(result.unresolved)} file(s) declare a project whose name is built at runtime: {listed}{more}."
        )
    if result.truncated:
        parts.append(f"Stopped after {result.files_scanned} files — narrow the search path.")
    parts.append("Projects created from the web app or with `rebase project create` have no declaring file.")
    return " ".join(parts)


def git_toplevel(
    cwd: Path,
    *,
    run_git: Callable[[list[str], Path], str | None] | None = None,
) -> Path | None:
    """The git repository root containing `cwd`, or None if there isn't one."""

    def default_run_git(args: list[str], directory: Path) -> str | None:
        # Imported lazily so this module stays importable without `requests`, which
        # keeps its tests free of the HTTP client's import cost.
        from rebase.client import _git

        return _git(args, cwd=directory)

    runner = run_git if run_git is not None else default_run_git
    root = runner(["rev-parse", "--show-toplevel"], cwd)
    if not root:
        return None
    return Path(root).resolve()


def is_risky_root(path: Path) -> bool:
    """Whether `path` is too broad to search: the home directory or a filesystem root.

    Registering one of these would turn every lookup into a scan of everything the
    user owns, so the TUI silently declines to record them and the CLI asks for
    --force.
    """
    resolved = Path(path).expanduser().resolve()
    if resolved == Path(resolved.anchor):
        return True
    try:
        return resolved == Path.home().resolve()
    except (OSError, RuntimeError):
        return False
