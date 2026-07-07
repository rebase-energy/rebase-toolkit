import pytest

import rebase as rb
from rebase.client import RebaseWorkflowError, Secret, _resolve_secrets_payload


class StubClient:
    def __init__(self) -> None:
        self.created: dict[str, dict[str, str]] = {}

    def set_secret(self, name: str, values: dict[str, str]) -> dict:
        self.created[name] = dict(values)
        return {"name": name, "secret_refs": {key: f"rbw-ws-{name}--{key}" for key in values}}

    def get_secret(self, name: str) -> dict:
        return {"name": name, "secret_refs": {"TOKEN": f"rbw-ws-{name}--TOKEN"}}


def test_secret_is_exported() -> None:
    assert rb.Secret is Secret


def test_from_name_resolves_to_refs() -> None:
    refs = Secret.from_name("acme-snowflake").resolve(StubClient())
    assert refs == {"TOKEN": "rbw-ws-acme-snowflake--TOKEN"}


def test_from_name_rejects_empty() -> None:
    with pytest.raises(ValueError):
        Secret.from_name("  ")


def test_from_dict_creates_bundle_on_resolve() -> None:
    client = StubClient()
    refs = Secret.from_dict({"A": "1", "B": "2"}, name="inline-test").resolve(client)
    assert client.created == {"inline-test": {"A": "1", "B": "2"}}
    assert refs == {"A": "rbw-ws-inline-test--A", "B": "rbw-ws-inline-test--B"}


def test_from_dict_without_name_uses_stable_content_hash() -> None:
    a = Secret.from_dict({"A": "1"})._resolved_name()
    b = Secret.from_dict({"A": "1"})._resolved_name()
    c = Secret.from_dict({"A": "2"})._resolved_name()
    assert a == b
    assert a != c
    assert a.startswith("inline-")


def test_from_dotenv_parses_file(tmp_path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text('# comment\nTOKEN="abc"\nEMPTY_LINE_SKIPPED\nUSER=svc\n')
    secret = Secret.from_dotenv(dotenv)
    assert secret.env_dict == {"TOKEN": "abc", "USER": "svc"}


def test_resolve_payload_accepts_modal_style_list() -> None:
    payload = _resolve_secrets_payload([Secret.from_name("acme"), "other"], StubClient())
    assert payload == {"TOKEN": "rbw-ws-other--TOKEN"}  # later bundles win on key collisions


def test_resolve_payload_passes_through_ref_map_without_client() -> None:
    payload = _resolve_secrets_payload({"ENV": "some-ref"}, None)
    assert payload == {"ENV": "some-ref"}


def test_resolve_payload_rejects_non_secret_entries() -> None:
    with pytest.raises(RebaseWorkflowError, match="must be rebase.Secret"):
        _resolve_secrets_payload([42], StubClient())


def test_function_decorator_accepts_secret_list() -> None:
    @rb.function(name="load", secrets=[Secret.from_name("acme")])
    def load() -> dict:
        return {}

    assert isinstance(load.secrets, list)
    assert isinstance(load.secrets[0], Secret)


def test_predictor_class_attribute_accepts_secret_list() -> None:
    class P(rb.Predictor):
        name = "p"
        secrets = [Secret.from_name("acme")]

        def predict(self) -> dict:
            return {}

    model = P()
    assert isinstance(model.secrets, list)
    assert model.secrets[0].name == "acme"
