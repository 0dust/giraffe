"""Bad model selections block inference while transient probe failures remain advisory."""

import json

import httpx
import pytest

from giraffe.client import LLMClient, _headers
from giraffe.models import RunConfig, Target
from giraffe.preflight import (ConnectionTest, ModelDiscovery, PreflightError, check_connection,
                               check_target, discover_models)
from tests.test_connection import endpoint


@pytest.mark.parametrize("status,body,options,rejected", [
    (400, {"error": {"message": 'invalid reasoning value: "bogus" (must be ...)',
                     "type": "invalid_request_error", "param": None, "code": None}},
     {"reasoning_effort": "bogus"}, None),
    (400, {"error": {"message": "bad", "param": "reasoning_effort"}},
     {"reasoning_effort": "bogus"}, "reasoning_effort"),
    (400, {"error": {"param": "reasoning_effort", "message":
                     "Invalid value for reasoning_effort. The supplied chat_template_kwargs was accepted."}},
     {"reasoning_effort": "bogus", "chat_template_kwargs": {"enable_thinking": False}},
     "reasoning_effort"),
    (400, {"error": {"message":
                     "Invalid value for reasoning_effort. The supplied chat_template_kwargs was accepted."}},
     {"reasoning_effort": "bogus", "chat_template_kwargs": {"enable_thinking": False}}, None),
    (422, {"detail": [{"loc": ["body", "reasoning_effort"]},
                      {"loc": ["body", "chat_template_kwargs"]}]},
     {"reasoning_effort": "bogus", "chat_template_kwargs": {"enable_thinking": False}}, None),
    (422, {"detail": [{"loc": ["body", "chat_template_kwargs"]}],
           "message": "Invalid value for reasoning_effort."},
     {"reasoning_effort": "bogus", "chat_template_kwargs": {"enable_thinking": False}},
     "chat_template_kwargs"),
    (400, {"error": {"message": "Unknown field chat_template_kwargs SECRET-ECHO"}},
     {"reasoning_effort": "bogus", "chat_template_kwargs": {"enable_thinking": False}},
     "chat_template_kwargs"),
    (422, {"detail": [{"type": "extra_forbidden", "loc": ["body", "reasoning_effort"], "msg": "x"}]},
     {"reasoning_effort": "bogus"}, "reasoning_effort"),
    (400, {"error": {"message": "invalid user input"}}, {"user": "x"}, None),
    (400, {"error": {"message": "bad", "param": "user"}}, {"user": "x"}, "user"),
    (400, {"error": "bad request"}, {}, None),
    (404, {"error": "Unknown field reasoning_effort"}, {"reasoning_effort": "bogus"}, None),
])
async def test_request_option_rejection_message_is_private(monkeypatch, status, body, options, rejected):
    requests, _ = endpoint(monkeypatch, lambda request: httpx.Response(status, json=body))
    records = []
    execute = LLMClient.execute

    async def capture(client, spec):
        record = await execute(client, spec)
        records.append(record)
        return record

    monkeypatch.setattr(LLMClient, "execute", capture)
    target = Target(name="local", url="http://server", model="tiny")
    with pytest.raises(PreflightError) as error:
        await check_target(target, RunConfig(targets=[target], request_options=options))

    if rejected:
        expected = (f"The server returned HTTP {status} and rejected request_options.{rejected}. "
                    "Check that field's value or remove it from the configuration.")
    elif options and status in {400, 422}:
        expected = (f"The server returned HTTP {status}. Check the model name, the OpenAI-compatible "
                    f"endpoint and request_options ({', '.join(sorted(options))}).")
    else:
        expected = (f"The server returned HTTP {status}. "
                    "Check the model name and OpenAI-compatible endpoint.")
    assert str(error.value) == expected
    assert error.value.blocking is (status == 404)
    assert len(requests) == 1
    workload = records[0].workload
    assert workload.get("rejected_request_option") == rejected
    assert workload.get("request_option_keys") == (sorted(options) if options and status != 404 else None)
    for private in ("invalid reasoning value", "bogus", "SECRET-ECHO"):
        assert private not in str(error.value)
        assert private not in json.dumps(workload)


@pytest.mark.parametrize("status,body", [
    pytest.param(400, {"error": {"param": "max_tokens", "message":
                               "max_tokens must be at least 64 when reasoning_effort is high."}},
                 id="param-with-prose"),
    pytest.param(400, {"error": {"param": "max_tokens", "message":
                               "max_tokens must be at least 64."}}, id="param-without-prose"),
    pytest.param(422, {"detail": [{"loc": ["body", "max_tokens"], "msg": "x"}],
                       "message": "max_tokens must be at least 64 when reasoning_effort is high."},
                 id="loc-with-prose"),
])
async def test_other_structured_field_keeps_generic_preflight_warning(monkeypatch, status, body):
    requests, _ = endpoint(monkeypatch, lambda request: httpx.Response(status, json=body))
    records = []
    execute = LLMClient.execute

    async def capture(client, spec):
        record = await execute(client, spec)
        records.append(record)
        return record

    monkeypatch.setattr(LLMClient, "execute", capture)
    target = Target(name="local", url="http://server", model="tiny")
    with pytest.raises(PreflightError) as error:
        await check_target(target, RunConfig(targets=[target], request_options={"reasoning_effort": "high"}))

    message = str(error.value)
    assert message == (f"The server returned HTTP {status}. "
                       "Check the model name and OpenAI-compatible endpoint.")
    assert "reasoning_effort" not in message and "rejected request_options" not in message
    assert not error.value.blocking
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["max_tokens"] == 32 and payload["reasoning_effort"] == "high"
    assert records[0].workload["rejected_other_field"] is True
    assert "rejected_request_option" not in records[0].workload
    assert "max_tokens" not in json.dumps(records[0].workload)


async def test_request_option_fallback_lists_at_most_five_keys(monkeypatch):
    endpoint(monkeypatch, lambda request: httpx.Response(400, json={"error": {"message": "bad"}}))
    target = Target(name="local", url="http://server", model="tiny")
    options = {f"option_{letter}": "x" for letter in "gfedcba"}
    with pytest.raises(PreflightError) as error:
        await check_target(target, RunConfig(targets=[target], request_options=options))
    assert str(error.value) == (
        "The server returned HTTP 400. Check the model name, the OpenAI-compatible endpoint "
        "and request_options (option_a, option_b, option_c, option_d, option_e and 2 more).")
    assert "option_f" not in str(error.value) and "option_g" not in str(error.value)


async def test_model_rejection_with_request_options_keeps_precedence(monkeypatch):
    endpoint(monkeypatch, lambda request: httpx.Response(200, json={"data": [{"id": "available"}]})
             if request.method == "GET" else httpx.Response(400, json={"error": {
                 "message": "model tiny not found", "param": "reasoning_effort"}}))
    target = Target(name="local", url="http://server", model="tiny")
    with pytest.raises(PreflightError) as error:
        await check_target(target, RunConfig(targets=[target], request_options={"reasoning_effort": "bogus"}))
    assert error.value.blocking
    assert str(error.value) == 'Model "tiny" not served at http://server. Available: available.'


async def test_rejection_records_only_request_options_sent_in_payload(monkeypatch):
    from giraffe.models import RequestSpec

    requests, _ = endpoint(monkeypatch, lambda request: httpx.Response(400, json={
        "error": {"message": "bad model stream_options temperature", "param": "model"}}))
    target = Target(name="local", url="http://server", model="tiny")
    config = RunConfig(targets=[target], request_options={"reasoning_effort": "bogus"})
    async with LLMClient(target, config) as client:
        record = await client.execute(RequestSpec(
            fixture_id="connection", scenario="connection", check_ids=["access"],
            messages=[{"role": "user", "content": "Reply with OK."}], stream=False,
            options={"model": "ignored", "temperature": None, "stream_options": {"include_usage": True}},
        ))
    payload = json.loads(requests[0].content)
    assert payload["model"] == "tiny"
    assert "temperature" not in payload and "stream_options" not in payload
    assert record.workload["request_option_keys"] == ["reasoning_effort"]
    assert "rejected_request_option" not in record.workload


@pytest.mark.parametrize("scenario", ["connection", "serving"])
async def test_rejection_records_only_configured_preflight_options(monkeypatch, scenario):
    from giraffe.models import RequestSpec

    requests, _ = endpoint(monkeypatch, lambda request: httpx.Response(400, json={"error": "bad"}))
    target = Target(name="local", url="http://server", model="tiny")
    config = RunConfig(targets=[target], request_options={"reasoning_effort": "x"}, stream=True)
    async with LLMClient(target, config) as client:
        client.token_evidence["usage"] = "supported"
        record = await client.execute(RequestSpec(
            fixture_id="probe", scenario=scenario, check_ids=["access"],
            messages=[{"role": "user", "content": "Reply with OK."}], stream=True,
            options={"temperature": 0},
        ))
    payload = json.loads(requests[0].content)
    assert payload["temperature"] == 0 and payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    if scenario == "connection":
        assert record.workload["request_option_keys"] == ["reasoning_effort"]
    else:
        assert "request_option_keys" not in record.workload


@pytest.mark.parametrize("body,keys,expected", [
    (b"not JSON", ["temperature"], (None, False)),
    (b"[]", ["temperature"], (None, False)),
    (b'{"error":"bad reasoning_effort"}', ["reasoning_effort"], ("reasoning_effort", False)),
    (b'{"detail":"bad reasoning_effort"}', ["reasoning_effort"], ("reasoning_effort", False)),
    (b'{"message":"bad reasoning_effort"}', ["reasoning_effort"], ("reasoning_effort", False)),
    (b'{"error":{"message":"bad reasoning_effort_extra"}}', ["reasoning_effort"], (None, False)),
    (b'{"error":{"message":"bad option_name.value"}}', ["option_name.value"], ("option_name.value", False)),
    (b'{"error":{"message":"bad option_nameXvalue"}}', ["option_name.value"], (None, False)),
    (b'{"error":{"message":"invalid user input"}}', ["user"], (None, False)),
    (b'{"error":{"message":"bad seed"}}', ["seed"], (None, False)),
    (b'{"error":{"message":"stop now"}}', ["stop"], (None, False)),
    (b'{"error":{"param":"seed"}}', ["seed"], ("seed", False)),
    (b'{"detail":[{"loc":["body","stop"]}]}', ["stop"], ("stop", False)),
    (b'{"error":{"param":"z"},"detail":[{"loc":["a"]}]}', ["z", "a"], ("z", False)),
    (b'{"error":{"param":"reasoning_effort","message":"bad chat_template_kwargs"}}',
     ["reasoning_effort", "chat_template_kwargs"], ("reasoning_effort", False)),
    (b'{"error":{"param":"temperature"},"detail":[{"loc":["body","reasoning_effort"]}]}',
     ["reasoning_effort"], (None, True)),
    (b'{"error":{"param":"temperature","message":"bad reasoning_effort"}}',
     ["reasoning_effort"], (None, True)),
    (b'{"error":{"param":"temperature"}}', ["reasoning_effort"], (None, True)),
    (b'{"error":{"param":["reasoning_effort"],"message":"bad reasoning_effort"}}',
     ["reasoning_effort"], ("reasoning_effort", False)),
    (b'{"detail":[{"loc":["body","reasoning_effort"]}],"message":"bad chat_template_kwargs"}',
     ["reasoning_effort", "chat_template_kwargs"], ("reasoning_effort", False)),
    (b'{"detail":[{"loc":["reasoning_effort"]},{"loc":["reasoning_effort"]}]}',
     ["reasoning_effort"], ("reasoning_effort", False)),
    (b'{"detail":[{"loc":["reasoning_effort"]},{"loc":["chat_template_kwargs"]}],'
     b'"message":"bad reasoning_effort"}', ["reasoning_effort", "chat_template_kwargs"], (None, False)),
    (b'{"detail":[{"loc":["temperature"]}],"message":"bad reasoning_effort"}',
     ["reasoning_effort"], (None, True)),
    (b'{"error":{"message":"reasoning_effort and chat_template_kwargs"}}',
     ["reasoning_effort", "chat_template_kwargs"], (None, False)),
    (b'{"error":{"message":"bad reasoning_effort"}}',
     ["reasoning_effort", "reasoning_effort"], ("reasoning_effort", False)),
    (b'{"detail":[null,{"loc":"temperature"}]}', ["temperature"], (None, False)),
    (b'{"error":{"param":"","message":"bad reasoning_effort"}}',
     ["reasoning_effort"], ("reasoning_effort", False)),
    (b'{"detail":[{"loc":[]}],"message":"bad reasoning_effort"}',
     ["reasoning_effort"], (None, True)),
    (b'{"detail":[null,{"loc":"temperature"}],"message":"bad reasoning_effort"}',
     ["reasoning_effort"], ("reasoning_effort", False)),
    (b'{"error":{"message":null}}', ["temperature"], (None, False)),
])
def test_rejected_request_option_matches_only_supplied_keys(body, keys, expected):
    from giraffe.client import _rejected_request_option

    assert _rejected_request_option(body, iter(keys)) == expected


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
