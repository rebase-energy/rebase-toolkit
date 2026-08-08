import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _project(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))["project"]


def test_rebase_alias_metapackage_tracks_toolkit_version() -> None:
    toolkit = _project(ROOT / "pyproject.toml")
    alias = _project(ROOT / "pypi" / "rebase" / "pyproject.toml")

    assert alias["name"] == "rebase"
    assert alias["version"] == toolkit["version"]
    assert alias["dependencies"] == [f"rebase-toolkit=={toolkit['version']}"]


def test_rebase_alias_extras_forward_to_toolkit_extras() -> None:
    toolkit = _project(ROOT / "pyproject.toml")
    alias = _project(ROOT / "pypi" / "rebase" / "pyproject.toml")

    assert alias["optional-dependencies"] == {
        extra: [f"rebase-toolkit[{extra}]=={toolkit['version']}"]
        for extra in (
            "data",
            "huggingface",
            "modeling",
            "hillclimb",
            "snowflake",
            "databricks",
            "bigquery",
            "fabric",
            "sources",
            "all",
        )
    }
