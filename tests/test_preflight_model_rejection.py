"""Bad model selections block inference while transient probe failures remain advisory."""

import httpx
import pytest

from giraffe.client import _headers
from giraffe.models import RunConfig, Target
from giraffe.preflight import (ConnectionTest, ModelDiscovery, PreflightError, check_connection,
                               check_target, discover_models)
from tests.test_connection import endpoint


@pytest.mark.parametrize("status", [400, 404, 422])
@pytest.mark.parametrize("body", ["Model does not exist: PRIVATE", "tiny not found: PRIVATE", "unknown model tiny: PRIVATE"])
async def test_rejected_model_lists_available_models_without_echoing_body(monkeypatch, status, body):
    def respond(request):
        if request.method == "GET":
            assert request.headers["Authorization"] == "Bearer secret-key"
            return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]})
        return httpx.Response(status, text=body)

    requests, _ = endpoint(monkeypatch, respond)
    monkeypatch.setenv("PREFLIGHT_MODEL_KEY", "secret-key")
    target = Target(name="local", url="http://server/v1", model="tiny", api_key_env="PREFLIGHT_MODEL_KEY")
    with pytest.raises(PreflightError) as error:
        await check_target(target, RunConfig(targets=[target]))
    assert error.value.blocking
    assert str(error.value) == 'Model "tiny" not served at http://server/v1. Available: a, b.'
    assert [request.method for request in requests] == ["POST", "GET"]


@pytest.mark.parametrize("discovery_status", [404, 503])
async def test_rejected_model_blocks_when_discovery_is_unavailable(monkeypatch, discovery_status):
    requests, _ = endpoint(monkeypatch, lambda request: httpx.Response(
        discovery_status if request.method == "GET" else 422, text="model tiny does not exist: PRIVATE"))
    target = Target(name="local", url="http://server", model="tiny")
    with pytest.raises(PreflightError) as error:
        await check_target(target, RunConfig(targets=[target]))
    assert error.value.blocking
    assert str(error.value) == 'Model "tiny" not served at http://server.'
    assert len(requests) == 2


@pytest.mark.parametrize("status,body", [(400, "bad temperature"), (422, "invalid input"),
                                        (500, "model not found"), (503, "model not found")])
async def test_non_model_and_transient_errors_remain_advisory(monkeypatch, status, body):
    requests, _ = endpoint(monkeypatch, lambda request: httpx.Response(status, text=body))
    target = Target(name="local", url="http://server", model="tiny")
    with pytest.raises(PreflightError) as error:
        await check_target(target, RunConfig(targets=[target]))
    assert not error.value.blocking
    assert len(requests) == 1


@pytest.mark.parametrize("variable", ["OPENAI_API_KEY", "CUSTOM_API_KEY"])
@pytest.mark.parametrize("operation", ["headers", "discovery", "probe", "connection"])
async def test_missing_api_key_names_actual_variable_without_sending_traffic(monkeypatch, variable, operation):
    monkeypatch.delenv(variable, raising=False)
    requests, _ = endpoint(monkeypatch, lambda request: pytest.fail("Missing key sent traffic"))
    target = Target(name="local", url="http://server", model="tiny", api_key_env=variable)
    expected = f"API key environment variable {variable} is not set"
    with pytest.raises(ValueError if operation == "headers" else PreflightError) as error:
        if operation == "headers":
            _headers(target)
        elif operation == "discovery":
            await discover_models(ModelDiscovery(url=target.url, api_key_env=variable))
        elif operation == "probe":
            await check_target(target, RunConfig(targets=[target]))
        else:
            await check_connection(ConnectionTest(url=target.url, model=target.model, api_key_env=variable))
    assert str(error.value) == expected
    assert not requests


@pytest.mark.parametrize("status", [400, 404, 422])
def test_cli_rejected_model_exits_three_before_suite_or_artifacts(monkeypatch, tmp_path, capsys, status, local_tokenizer):
    from giraffe.cli import main

    endpoint(monkeypatch, lambda request: httpx.Response(200, json={"data": [{"id": "available"}]})
             if request.method == "GET" else httpx.Response(status, json={"error": "model not found"}))
    monkeypatch.setattr("giraffe.cli._run", lambda *args, **kwargs: pytest.fail("Rejected model ran suite"))
    directory = tmp_path / "run"
    assert main(["run", "--url", "http://server", "--model", "wrong", "--output", str(directory)]) == 3
    assert 'Model "wrong" not served at http://server. Available: available' in capsys.readouterr().err
    assert not directory.exists()


@pytest.mark.parametrize('payload', [
    {'error': {'message': "Unsupported parameter 'max_tokens' is not supported with this model", 'code': 'unsupported_parameter'}},
    {'detail': [{'loc': ['body', 'model'], 'msg': 'invalid value', 'input': 'tiny'}]},
    {'error': {'message': 'Unknown parameter max_tokens for tiny model', 'code': 'unsupported_parameter'}},
])
async def test_parameter_rejection_is_advisory_even_when_it_mentions_model(monkeypatch, payload):
    endpoint(monkeypatch, lambda request: httpx.Response(422, json=payload))
    target = Target(name='local', url='http://server', model='tiny')
    with pytest.raises(PreflightError) as error:
        await check_target(target, RunConfig(targets=[target]))
    assert not error.value.blocking


async def test_discovered_model_is_never_classified_as_not_served(monkeypatch):
    endpoint(monkeypatch, lambda request: httpx.Response(200, json={'data': [{'id': 'tiny'}]})
             if request.method == 'GET' else httpx.Response(400, text='unknown model tiny'))
    target = Target(name='local', url='http://server', model='tiny')
    with pytest.raises(PreflightError) as error:
        await check_target(target, RunConfig(targets=[target]))
    assert not error.value.blocking


@pytest.mark.parametrize('payload', [
    {'detail': 'Model tiny not found'},
    {'message': 'Model tiny does not exist'},
    {'error': {'code': 'model_not_found', 'message': None}},
])
async def test_model_rejection_accepts_string_details_and_error_codes(monkeypatch, payload):
    endpoint(monkeypatch, lambda request: httpx.Response(422, json=payload))
    target = Target(name='local', url='http://server', model='tiny')
    with pytest.raises(PreflightError) as error:
        await check_target(target, RunConfig(targets=[target]))
    assert error.value.blocking
    assert str(error.value) == 'Model "tiny" not served at http://server.'


@pytest.mark.parametrize('detail', [
    {'message': None, 'code': None},
    {'message': ['model tiny not found'], 'code': {'value': 'model_not_found'}},
    {'message': {'model': 'tiny'}, 'code': ['model_not_found']},
])
async def test_malformed_error_metadata_remains_advisory(monkeypatch, detail):
    endpoint(monkeypatch, lambda request: httpx.Response(422, json={'error': detail}))
    target = Target(name='local', url='http://server', model='tiny')
    with pytest.raises(PreflightError) as error:
        await check_target(target, RunConfig(targets=[target]))
    assert not error.value.blocking
