# Check declared gateway access

Select **Gateway access policy** in the local UI, choose the LiteLLM profile, and
enter the expected cases plus environment variable references for existing test
identities. The endpoint and model come from Connection settings. The same check
is available from the CLI:

```sh
giraffe run --config examples/gateway-access.yaml
```

First edit the example's URL and model and supply `GIRAFFE_ALLOWED_KEY` and
`GIRAFFE_DENIED_KEY` in the environment where Giraffe runs. Use existing test
credentials whose intended policy you know: the first identity should be allowed
to call that model; the second should be invalid or denied that model. Giraffe
does not create keys, revoke keys, change privileges, or call administrative
gateway endpoints. Each case makes one inference request and may incur the
endpoint's normal inference cost. Access probes use a fixed prompt and the
declared model and output cap. They omit suite-level request options and set
LiteLLM's `disable_fallbacks` option to request routing to that model group alone.
When you select ordinary checks too, Access probes run after shared-key work on
all targets, before any Gateway Policy or Attribution probes.

Identities may include `headers_env`, mapping header names to environment variable
names. They replace the endpoint's credentials completely. An explicit empty
identity sends no authentication. A missing alias or invalid environment value
sends no request for that case. Never put resolved credentials in a configuration file.

When LiteLLM is configured with a custom key header, set **LiteLLM custom key
header** in the UI or `gateway_access.auth_header` in YAML to that header's name.
It takes precedence over the default headers when present in the request. Leave
the setting blank for LiteLLM's native key headers. Cases with other credential
headers remain unverified until the gateway's custom header is declared. See
[LiteLLM custom key headers](https://docs.litellm.ai/docs/proxy/virtual_keys#pass-litellm-key-in-custom-header).

The check supports up to 16 cases, a maximum 60-second deadline per request, and
an output cap of at most 128 tokens. Shared run limits also apply. A complete
matrix needs at least one expected allow and one expected deny. Cases with
opposite expectations must use distinct credentials. Conflicting cases send no
request and remain unverified. Every case must produce qualified evidence before
the matrix can pass.

| Response | Observation |
| --- | --- |
| HTTP 200 with a valid assistant answer and a `stop` or `length` finish reason | Allow |
| HTTP 200 with a nonempty assistant refusal and a `stop` finish reason | Allow |
| HTTP 200 with a `length` finish and assistant reasoning metered at the full requested output cap | Allow |
| HTTP 401 with a recognized LiteLLM authentication error | Deny |
| HTTP 403 with a recognized LiteLLM model-access error | Deny |
| Timeout, network error, 429, 5xx, redirect, unknown authentication error, reported fallback or different routed model group, tool call, malformed or incomplete response | Unverified |

Recognized 401 types are `token_not_found_in_db` and `expired_key`. An explicit
empty identity can also qualify when LiteLLM returns its exact missing-key
`auth_error`. Other generic `auth_error` responses remain unverified because they
can indicate an authentication backend failure.
Recognized 403 types are `key_model_access_denied`, `team_model_access_denied`,
`user_model_access_denied`, `org_model_access_denied`, and
`project_model_access_denied`. The type and any explicit error code must match
the HTTP status. Provider exception markers leave the response unverified.
Other versions or gateway error formats may remain unverified. See LiteLLM's
[error reference](https://docs.litellm.ai/docs/proxy/error_reference) and
[chat-completion client setup](https://docs.litellm.ai/docs/proxy/user_keys).

The report shows expected versus observed decisions for every declared case,
including cases that could not run. Observed access for an expected-deny case
fails the check. Policy observations remain separate from ordinary Endpoint
access and performance measurements. Unconfigured checks do not stop ordinary
shared-key tests. Safe configuration references and qualified outcomes survive
report export and baseline comparison; secrets and raw gateway responses do not.
Access comparisons require a baseline recorded with effective key qualification.

These cases check only the supplied identities, endpoint and model. They do not
certify tenant isolation or comprehensive gateway security. Automated fixtures
exercise the supported protocol; this example has not qualified a live gateway.
