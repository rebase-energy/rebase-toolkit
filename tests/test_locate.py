from __future__ import annotations

import textwrap
from pathlib import Path

from rebase.locate import (
    LocateResult,
    describe_failure,
    find_project_declarations,
    git_toplevel,
    is_risky_root,
)


def write(path: Path, source: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    return path


def test_locate_resolves_a_module_level_constant_used_as_the_project_name(tmp_path: Path) -> None:
    """The shape every real deploy file uses: the name is a constant, not a literal,
    so a text search for `rb.project("epex")` would find nothing."""
    target = write(
        tmp_path / "deploy" / "epex_bid_curves.py",
        """
        import rebase as rb

        PROJECT_NAME = "epex"

        project = rb.project(
            PROJECT_NAME,
            description="EPEX SPOT day-ahead aggregated bid curve archive.",
        )
        """,
    )

    result = find_project_declarations("epex", [tmp_path])

    assert result.status == "found"
    assert result.matches[0].path == target
    # The cursor belongs on the call, not on the argument a line below it.
    assert result.matches[0].line == 5
    assert result.matches[0].column == 10


def test_locate_matches_a_string_literal_passed_positionally_or_by_keyword(tmp_path: Path) -> None:
    write(tmp_path / "a" / "positional.py", 'import rebase as rb\nrb.project("alpha")\n')
    write(tmp_path / "b" / "keyword.py", 'import rebase as rb\nrb.project(name="beta")\n')

    assert find_project_declarations("alpha", [tmp_path]).status == "found"
    assert find_project_declarations("beta", [tmp_path]).status == "found"


def test_locate_matches_ensure_project_and_direct_project_construction(tmp_path: Path) -> None:
    """`Project(` is why the prefilter needs two needles: `project(` is not in it."""
    write(tmp_path / "imperative.py", 'import rebase as rb\nclient = rb.Client()\nclient.ensure_project("alpha")\n')
    write(tmp_path / "constructed.py", 'import rebase as rb\nrb.Project("beta")\n')

    assert find_project_declarations("alpha", [tmp_path]).status == "found"
    assert find_project_declarations("beta", [tmp_path]).status == "found"


def test_locate_accepts_a_bare_project_call_only_when_imported_from_rebase(tmp_path: Path) -> None:
    write(tmp_path / "imported.py", 'from rebase import project\nproject("alpha")\n')
    write(
        tmp_path / "unrelated.py",
        """
        def project(name):
            return name

        project("alpha")
        """,
    )

    result = find_project_declarations("alpha", [tmp_path])

    assert result.status == "found"
    assert result.matches[0].path.name == "imported.py"


def test_locate_ignores_a_bare_project_call_after_the_name_is_rebound(tmp_path: Path) -> None:
    """`project = rb.project(...)` then `project.workflow(...)` is the standard idiom;
    the rebound name must not count as a second declaration."""
    write(
        tmp_path / "deploy.py",
        """
        from rebase import project

        PROJECT_NAME = "alpha"
        project = project(PROJECT_NAME)
        project("alpha")
        """,
    )

    result = find_project_declarations("alpha", [tmp_path])

    assert result.status == "found"
    assert result.matches[0].line == 4


def test_locate_reports_computed_names_as_unresolved_instead_of_guessing(tmp_path: Path) -> None:
    computed = write(
        tmp_path / "computed.py",
        """
        import os

        import rebase as rb

        rb.project(os.environ["PROJECT"])
        rb.project(f"{os.environ['ENV']}-alpha")
        """,
    )

    result = find_project_declarations("alpha", [tmp_path])

    assert result.status == "not-found"
    assert result.unresolved == (computed,)
    assert "built at runtime" in describe_failure(result)


def test_locate_takes_the_last_module_level_binding_of_a_reassigned_constant(tmp_path: Path) -> None:
    write(
        tmp_path / "deploy.py",
        """
        import rebase as rb

        PROJECT_NAME = "old"
        PROJECT_NAME = "new"

        rb.project(PROJECT_NAME)
        """,
    )

    assert find_project_declarations("new", [tmp_path]).status == "found"
    assert find_project_declarations("old", [tmp_path]).status == "not-found"


def test_locate_prunes_virtualenvs_node_modules_and_directories_with_pyvenv_cfg(tmp_path: Path) -> None:
    declaration = 'import rebase as rb\nrb.project("alpha")\n'
    write(tmp_path / ".venv" / "lib" / "vendored.py", declaration)
    write(tmp_path / "node_modules" / "pkg" / "vendored.py", declaration)
    write(tmp_path / "sandbox" / "vendored.py", declaration)
    (tmp_path / "sandbox" / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    real = write(tmp_path / "src" / "deploy.py", declaration)

    result = find_project_declarations("alpha", [tmp_path])

    assert result.status == "found"
    assert result.matches[0].path == real
    assert result.files_scanned == 1


def test_locate_parses_only_the_files_that_survive_the_byte_prefilter(tmp_path: Path) -> None:
    """The prefilter is what makes a full-tree search cheap enough to never cache."""
    write(tmp_path / "declares.py", 'import rebase as rb\nrb.project("alpha")\n')
    for index in range(5):
        write(tmp_path / f"decoy_{index}.py", "import os\n\nVALUE = os.getcwd()\n")

    result = find_project_declarations("alpha", [tmp_path])

    assert result.files_scanned == 6
    assert result.files_parsed == 1


def test_locate_records_unparsable_files_without_failing_the_search(tmp_path: Path) -> None:
    write(tmp_path / "broken.py", "def project(:\n")
    write(tmp_path / "good.py", 'import rebase as rb\nrb.project("alpha")\n')

    result = find_project_declarations("alpha", [tmp_path])

    assert result.status == "found"
    assert result.matches[0].path.name == "good.py"


def test_locate_skips_files_larger_than_the_size_cap(tmp_path: Path) -> None:
    from rebase.locate import scan_file

    big = write(tmp_path / "big.py", 'import rebase as rb\nrb.project("alpha")\n')

    assert scan_file(big, max_bytes=10) is None
    assert scan_file(big) is not None


def test_locate_reports_every_declaring_file_when_more_than_one_matches(tmp_path: Path) -> None:
    declaration = 'import rebase as rb\nrb.project("alpha")\n'
    first = write(tmp_path / "a_first.py", declaration)
    second = write(tmp_path / "b_second.py", declaration)

    result = find_project_declarations("alpha", [tmp_path])

    assert result.status == "ambiguous"
    assert [match.path for match in result.matches] == [first, second]


def test_locate_reports_one_match_per_file_when_a_file_declares_the_project_twice(tmp_path: Path) -> None:
    write(tmp_path / "twice.py", 'import rebase as rb\nrb.project("alpha")\nrb.project("alpha")\n')

    result = find_project_declarations("alpha", [tmp_path])

    assert result.status == "found"
    assert result.matches[0].line == 2


def test_locate_counts_a_file_once_when_search_roots_overlap(tmp_path: Path) -> None:
    write(tmp_path / "sub" / "deploy.py", 'import rebase as rb\nrb.project("alpha")\n')

    result = find_project_declarations("alpha", [tmp_path, tmp_path / "sub"])

    assert result.status == "found"
    assert result.files_scanned == 1


def test_locate_reports_no_roots_when_the_workspace_has_none() -> None:
    result = find_project_declarations("alpha", [])

    assert result.status == "no-roots"
    assert "rebase project search-path add" in describe_failure(result)


def test_locate_reports_missing_search_roots_as_errors_not_exceptions(tmp_path: Path) -> None:
    result = find_project_declarations("alpha", [tmp_path / "gone"])

    assert result.status == "not-found"
    assert result.errors and "gone" in result.errors[0]


def test_locate_failure_message_names_the_roots_and_the_files_searched(tmp_path: Path) -> None:
    write(tmp_path / "decoy.py", "VALUE = 1\n")

    message = describe_failure(find_project_declarations("alpha", [tmp_path]))

    assert str(tmp_path) in message
    assert "1 Python files searched" in message
    assert "web app" in message


def test_locate_reports_truncation_when_the_file_cap_is_hit(tmp_path: Path) -> None:
    for index in range(5):
        write(tmp_path / f"file_{index}.py", "VALUE = 1\n")

    result = find_project_declarations("alpha", [tmp_path], max_files=3)

    assert result.truncated is True
    assert result.files_scanned == 3
    assert "narrow the search path" in describe_failure(result)


def test_git_toplevel_uses_the_injected_git_runner(tmp_path: Path) -> None:
    calls: list[tuple[list[str], Path]] = []

    def fake_git(args: list[str], directory: Path) -> str | None:
        calls.append((args, directory))
        return str(tmp_path)

    assert git_toplevel(tmp_path / "sub", run_git=fake_git) == tmp_path.resolve()
    assert calls == [(["rev-parse", "--show-toplevel"], tmp_path / "sub")]

    assert git_toplevel(tmp_path, run_git=lambda args, directory: None) is None


def test_is_risky_root_rejects_the_home_directory_and_the_filesystem_root(tmp_path: Path) -> None:
    assert is_risky_root(Path.home()) is True
    assert is_risky_root(Path(Path.home().anchor)) is True
    assert is_risky_root(tmp_path) is False


def test_locate_result_status_is_derived_from_roots_and_matches() -> None:
    assert LocateResult(project_name="a").status == "no-roots"
    assert LocateResult(project_name="a", roots=(Path("/x"),)).status == "not-found"
