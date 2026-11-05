# Gateway quotas and priorities

Select `gateway_policy` to send bounded, identical, non-streaming requests using existing test identities. This optional check supports LiteLLM per-key RPM quotas and Enterprise dynamic RPM reservations. It does not change keys, quotas, priorities or gateway configuration.

Start with [the example configuration](../examples/gateway-policy.yaml). Supply the two environment variables locally, choose an existing model and enter the actual gateway version. The limited key must already have a 2 RPM limit; the peer key must already allow at least 8 RPM. Run `giraffe run --config examples/gateway-policy.yaml` or select **Gateway quotas & priorities** in the UI. Use the configuration JSON editor to declare targets[].test_identities with api_key_env or headers_env references, never credential values.

Each identity has a finite request cap, with a maximum of 100 requests. The traffic duration is at most 55 seconds, inside LiteLLM's documented 60-second policy window. Each identity sends sequentially and shares the suite's request slots and budgets. Unfinished work is cancelled at the traffic cutoff. A peer must receive a valid completion from a request dispatched **after** the affected identity's attributed rejection, within the same window. Offer enough peer requests for this to happen.

`window_state: isolated_fresh` declares that the operator established an unused policy window and excluded other traffic consuming these limits. Giraffe does not establish or reset that state. Leave it `unknown` when it cannot be established. Run this check alone: other selected checks can consume shared model capacity and therefore leave policy qualification unverified. Request caps and local duration bounds apply even when window state is unknown.

## Dynamic priority reservations

Use `capability: priority_rpm` only when the existing gateway runs LiteLLM's Enterprise `dynamic_rate_limiter_v3` with RPM reservations. For the example, replace the policy block with:

```yaml
gateway_policy:
  gateway: litellm
  capability: priority_rpm
  gateway_version: null # Actual deployed version, required for qualification.
  affected_identity: limited
  users:
    - {identity: limited, requests: 5, rpm: 2, priority: low}
    - {identity: peer, requests: 8, rpm: 8, priority: high}
  model_rpm: 10
  saturation_threshold: 0
  saturation_cache_seconds: 0
  duration_seconds: 15
  interval_seconds: 0.1
  max_output_tokens: 16
  window_state: unknown
```

Here `rpm` denotes each tier's existing reserved share. Tier labels must match the gateway's resolved key or team priority. The example assumes the gateway already has these settings; Giraffe sends no priority override. Nonzero saturation caching leaves enforcement unverified because gateway nodes may see different saturation states. Below a positive saturation threshold, idle capacity can be borrowed; the check allows that documented initial borrowing before judging excess admission.

Priority qualification requires the gateway's specific priority-pool rejection, matching resolved tiers and model RPM, strict-mode saturation evidence, actual input/output token usage, and overlapping requests. Actual input counts must match; actual output lengths must be positive and within a factor of two. Different workloads or missing usage remain unverified. Client-observed request overlap establishes concurrent offered work, not internal queue overlap. Response-time differences never establish priority enforcement. Scheduler queue priorities and TPM reservations are not supported by this check.

## Reading results

- **Observed:** attributed rejection and subsequent peer service match the declared policy and window assumptions.
- **Observed failure:** attributed evidence contradicts the declared RPM allowance within the isolated window.
- **Unverified:** provenance, policy/window state, comparable work or other necessary evidence is missing or conflicting.
- **Unexercised:** bounded traffic did not exercise policy rejection or subsequent peer service.
- **Unavailable:** the selected gateway or capability is unsupported; no policy traffic is sent.

Reports retain admitted, rejected, completed, cancelled and other failed requests per identity, actual token counts, declared settings, and qualifications. Generic 429s and provider rate limits cannot establish gateway policy enforcement. Raw gateway error messages and response headers are discarded. JSON, HTML, CLI, UI and reproduction exports carry the same qualifications. Baseline comparisons require matching policy settings and a qualified passing baseline, and compare observed acceptance without timing-based priority claims.

The controlled HTTP fixtures test Giraffe's classification, scheduling and reporting. They do not certify a live LiteLLM deployment. Live gateway enforcement must be exercised using the operator's actual configured test environment.

Sources: [LiteLLM limits](https://docs.litellm.ai/docs/proxy/users), [dynamic reservations and window semantics](https://docs.litellm.ai/docs/proxy/dynamic_rate_limit), [response header scope](https://docs.litellm.ai/docs/proxy/response_headers), [priority response semantics](https://docs.litellm.ai/docs/scheduler), [error reference](https://docs.litellm.ai/docs/proxy/error_reference).
