from __future__ import annotations

from typing import Any

import pytest

import rebase as rb
from rebase.client import Bucket, BucketObject, Client, RebaseWorkflowError, _resolve_buckets_payload, bucket_env_var


class FakeResponse:
    def __init__(self, payload: Any = None, status_code: int = 200) -> None:
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = ""
        self.content = b""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


def _client() -> Client:
    return Client(api_key="rbw_test", api_url="https://workflows.example.com")


def test_bucket_is_exported() -> None:
    assert rb.Bucket is Bucket
    assert "Bucket" in rb.__all__


def test_env_var_naming_matches_the_api() -> None:
    assert bucket_env_var("forecasts") == "REBASE_BUCKET_FORECASTS"
    assert bucket_env_var("raw-data.v2") == "REBASE_BUCKET_RAW_DATA_V2"


def test_requires_a_name() -> None:
    with pytest.raises(ValueError, match="non-empty name"):
        Bucket("")


class TestUri:
    def test_prefers_the_injected_environment(self, monkeypatch) -> None:
        # Inside a deployed run the URI is already in the environment, so
        # resolving it must not cost an API call.
        monkeypatch.setenv("REBASE_BUCKET_FORECASTS", "gs://rb-acme-forecasts-abc")
        monkeypatch.setattr(
            Client, "get_bucket", lambda self, name: pytest.fail("must not call the API when env is set")
        )
        assert Bucket("forecasts", client=_client()).uri == "gs://rb-acme-forecasts-abc"

    def test_falls_back_to_the_api(self, monkeypatch) -> None:
        monkeypatch.delenv("REBASE_BUCKET_FORECASTS", raising=False)
        monkeypatch.setattr(Client, "get_bucket", lambda self, name: {"uri": "gs://rb-x"})
        assert Bucket("forecasts", client=_client()).uri == "gs://rb-x"


class TestRpcs:
    def test_object_ops_go_over_signed_urls(self, monkeypatch) -> None:
        observed: list[dict[str, Any]] = []

        def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
            observed.append({"method": method, "url": url, "json": kwargs.get("json"), "params": kwargs.get("params")})
            if url.endswith("/signed-urls"):
                paths = (kwargs.get("json") or {})["paths"]
                return FakeResponse(
                    {
                        "urls": [
                            {"path": p, "url": f"https://signed/{p}", "method": "GET", "expires_seconds": 600}
                            for p in paths
                        ]
                    }
                )
            if "/objects/stat" in url:
                return FakeResponse({"path": "a.txt", "size": 3})
            if url.endswith("/objects"):
                return FakeResponse(
                    {"objects": [{"path": "a.txt", "size": 3}], "prefixes": [], "next_page_token": None}
                )
            return FakeResponse({"name": "forecasts", "uri": "gs://rb-x"})

        monkeypatch.setattr("requests.request", fake_request)
        monkeypatch.setattr("requests.put", lambda url, data=None, timeout=None: FakeResponse())

        got: dict[str, Any] = {}

        def fake_get(url: str, timeout: int | None = None, stream: bool = False) -> Any:
            got["url"] = url
            response = FakeResponse()
            response.content = b"hello"
            return response

        monkeypatch.setattr("requests.get", fake_get)

        bucket = Bucket("forecasts", client=_client())
        assert bucket.put("a.txt", b"hello") == "a.txt"
        assert bucket.get("a.txt") == b"hello"
        assert got["url"] == "https://signed/a.txt"
        assert bucket.list()["objects"] == [BucketObject(key="a.txt", size=3)]
        assert bucket.stat("a.txt")["size"] == 3
        bucket.delete("a.txt")

        methods = [(item["method"], item["url"].rsplit("/buckets", 1)[-1]) for item in observed]
        assert ("DELETE", "/forecasts/objects") in methods

    def test_list_passes_delimiter_and_page_token(self, monkeypatch) -> None:
        observed: dict[str, Any] = {}

        def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
            observed.update(kwargs.get("params") or {})
            return FakeResponse({"objects": [], "prefixes": ["2026/"], "next_page_token": "t"})

        monkeypatch.setattr("requests.request", fake_request)
        page = Bucket("forecasts", client=_client()).list("2026/", delimiter="/", page_token="abc")
        assert observed["delimiter"] == "/"
        assert observed["page_token"] == "abc"
        assert page["prefixes"] == ["2026/"]
        assert page["next_page_token"] == "t"

    def test_iter_all_follows_pagination(self, monkeypatch) -> None:
        pages = [
            {"objects": [{"path": "a", "size": 1}], "prefixes": [], "next_page_token": "t"},
            {"objects": [{"path": "b", "size": 2}], "prefixes": [], "next_page_token": None},
        ]
        calls: list[Any] = []

        def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
            calls.append((kwargs.get("params") or {}).get("page_token"))
            return FakeResponse(pages[len(calls) - 1])

        monkeypatch.setattr("requests.request", fake_request)
        keys = [obj.key for obj in Bucket("forecasts", client=_client()).iter_all()]
        assert keys == ["a", "b"]
        assert calls == [None, "t"]

    def test_signed_urls_are_batched(self, monkeypatch) -> None:
        batches: list[int] = []

        def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
            paths = (kwargs.get("json") or {})["paths"]
            batches.append(len(paths))
            return FakeResponse(
                {"urls": [{"path": p, "url": f"https://s/{p}", "method": "GET", "expires_seconds": 60} for p in paths]}
            )

        monkeypatch.setattr("requests.request", fake_request)
        keys = [f"f{i}.txt" for i in range(250)]
        urls = Bucket("forecasts", client=_client()).signed_urls(keys)
        assert len(urls) == 250
        # Chunked to the API's cap rather than sent as one oversized request.
        assert batches == [100, 100, 50]

    def test_exists_is_false_on_404(self, monkeypatch) -> None:
        def fake_stat(self: Client, name: str, path: str) -> dict[str, Any]:
            error = RebaseWorkflowError("not found")
            error.status_code = 404
            raise error

        monkeypatch.setattr(Client, "stat_bucket_object", fake_stat)
        assert Bucket("forecasts", client=_client()).exists("missing.txt") is False


class TestResolvePayload:
    def test_accepts_names_and_handles(self) -> None:
        assert _resolve_buckets_payload(["b", "a"], None) == [{"bucket": "a"}, {"bucket": "b"}]
        assert _resolve_buckets_payload([Bucket.from_name("raw")], None) == [{"bucket": "raw"}]

    def test_none_is_empty(self) -> None:
        assert _resolve_buckets_payload(None, None) == []

    def test_no_network_without_create_if_missing(self) -> None:
        # A deploy naming existing buckets must not need auth to resolve them.
        assert _resolve_buckets_payload(["raw"], None) == [{"bucket": "raw"}]

    def test_create_if_missing_requires_a_client(self) -> None:
        with pytest.raises(RebaseWorkflowError, match="requires an authenticated client"):
            _resolve_buckets_payload([Bucket.from_name("raw", create_if_missing=True)], None)

    def test_rejects_wrong_types(self) -> None:
        with pytest.raises(RebaseWorkflowError, match="must be rebase.Bucket or bucket names"):
            _resolve_buckets_payload([123], None)


class TestDeployAttachment:
    def _deploy(self, monkeypatch, **decorator_kwargs: Any) -> dict[str, Any]:
        observed: dict[str, Any] = {}
        monkeypatch.setattr(Client, "find_function", lambda self, name, project=None: None)
        monkeypatch.setattr(Client, "ensure_project", lambda self, name: {"id": "project-id"})

        def fake_register(self: Client, **kwargs: Any) -> dict[str, Any]:
            observed.update(kwargs)
            return {"id": "function-id"}

        monkeypatch.setattr(Client, "register_function", fake_register)

        @rb.function(project="ml", name="train", **decorator_kwargs)
        def train() -> dict:
            return {}

        train.client = _client()
        train.deploy()
        return observed

    def test_buckets_reach_the_deploy_payload(self, monkeypatch) -> None:
        observed = self._deploy(monkeypatch, buckets=["forecasts", "raw"])
        assert observed["buckets"] == [{"bucket": "forecasts"}, {"bucket": "raw"}]

    def test_decorator_keeps_buckets_unresolved(self) -> None:
        @rb.function(project="ml", name="train", buckets=["forecasts"])
        def train() -> dict:
            return {}

        # Unresolved until deploy, so importing a module never hits the network.
        assert train.buckets == ["forecasts"]


def test_register_function_omits_buckets_when_empty(monkeypatch) -> None:
    # The API forbids unknown fields, so a client that always sent "buckets"
    # would 422 every deploy against a server that predates the field.
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        if url.endswith("/functions"):
            observed.update(kwargs.get("json") or {})
        return FakeResponse({"id": "function-id"})

    monkeypatch.setattr("requests.request", fake_request)
    monkeypatch.setattr(Client, "ensure_project", lambda self, name: {"id": "project-id"})
    _client().register_function(project="ml", name="train", source_code="def f(): pass", entrypoint="f")
    assert "buckets" not in observed
    # volumes keeps its historical always-present shape; only buckets is new.
    assert observed["volumes"] == []


def test_register_function_includes_buckets_when_set(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        if url.endswith("/functions"):
            observed.update(kwargs.get("json") or {})
        return FakeResponse({"id": "function-id"})

    monkeypatch.setattr("requests.request", fake_request)
    monkeypatch.setattr(Client, "ensure_project", lambda self, name: {"id": "project-id"})
    _client().register_function(
        project="ml",
        name="train",
        source_code="def f(): pass",
        entrypoint="f",
        buckets=[{"bucket": "forecasts"}],
    )
    assert observed["buckets"] == [{"bucket": "forecasts"}]
