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


def test_function_cpu_memory_normalize_modal_style() -> None:
    @rb.function(name="heavy", cpu=2, memory=1024)
    def heavy() -> dict:
        return {}

    assert heavy.cloud_run_cpu == "2000m"
    assert heavy.cloud_run_memory == "1024Mi"

    @rb.function(name="strings", cpu="500m", memory="1Gi")
    def strings() -> dict:
        return {}

    assert strings.cloud_run_cpu == "500m"
    assert strings.cloud_run_memory == "1Gi"


def test_function_cpu_memory_default_none() -> None:
    @rb.function(name="light")
    def light() -> dict:
        return {}

    assert light.cloud_run_cpu is None
    assert light.cloud_run_memory is None


def test_function_rejects_nonpositive_resources() -> None:
    import pytest as _pytest

    with _pytest.raises(ValueError):

        @rb.function(name="bad", cpu=0)
        def bad() -> dict:
            return {}


def test_workflow_cpu_memory_normalize_like_a_function() -> None:
    """A workflow sizes its own job container with the same spelling a function uses."""

    @rb.workflow(name="heavy-flow", mode="job", cpu=2, memory=4096)
    def heavy_flow() -> dict:
        return {}

    assert heavy_flow.cloud_run_cpu == "2000m"
    assert heavy_flow.cloud_run_memory == "4096Mi"

    @rb.workflow(name="string-flow", mode="job", cpu="500m", memory="2Gi")
    def string_flow() -> dict:
        return {}

    assert string_flow.cloud_run_cpu == "500m"
    assert string_flow.cloud_run_memory == "2Gi"


def test_workflow_cpu_memory_default_none() -> None:
    """Declaring nothing stays NULL, so the backend default applies."""

    @rb.workflow(name="light-flow")
    def light_flow() -> dict:
        return {}

    assert light_flow.cloud_run_cpu is None
    assert light_flow.cloud_run_memory is None


def test_workflow_resources_is_separate_from_its_own_container() -> None:
    """`resources=` annotates steps; it must not be mistaken for the job's own size."""

    @rb.workflow(name="stepped", mode="job", resources={"memory": "8Gi"})
    def stepped() -> dict:
        return {}

    assert stepped.resource_policy == {"memory": "8Gi"}
    assert stepped.cloud_run_memory is None


def test_predictor_class_cpu_memory() -> None:
    class Heavy(rb.Predictor):
        name = "heavy-model"
        cpu = 0.5
        memory = 2048

        def predict(self) -> dict:
            return {}

    model = Heavy()
    assert model.cloud_run_cpu == "500m"
    assert model.cloud_run_memory == "2048Mi"
