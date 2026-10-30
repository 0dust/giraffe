"""Bounded tokenizer setup and offline prompt sizing.

The subprocess keeps tokenizer imports, file parsing and tokenization off the
event loop, and can be killed on timeout or Stop. It never contacts the endpoint.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

import httpx

PREPARATION_TIMEOUT_SECONDS = 10.0
TARGET_PREPARATION_TIMEOUT_SECONDS = 20.0
SETUP_TIMEOUT_SECONDS = 30.0
AUTOMATIC_SETUP_TIMEOUT_SECONDS = 5.0
BUILTIN = "builtin"
BUILTIN_ENCODING = "o200k_base"
HF_INSTALL_HINT = "Install giraffe-llm[hf-tokenizer] to use the model's own tokenizer"
BUILTIN_NOTICE = ("Using built-in tiktoken o200k_base for prompt sizing. These are reference "
                  "token counts, not the served model's counts; chat formatting is excluded. "
                  "Measured token usage comes from the server.")
MAX_SIZING_ATTEMPTS = 16
MAX_PROMPT_CHARS = 2_097_152


class TokenizerUnavailable(Exception):
    def __init__(self, message, *, code="tokenizer_unavailable", target_index=0):
        super().__init__(message)
        self.code = code
        self.target_index = target_index


def needs_tokenizer(config):
    session_sizing = ("sessions" in config.checks and config.sessions is not None
                     and config.sessions.sampling is not None
                     and config.sessions.sampling.input_tokens is not None)
    return session_sizing or "context" in config.checks or (
        "load" in config.checks and not config.custom_fixtures
        and not (config.request_source == "request_pack" and config.request_pack)
    )


def _alias_path(target):
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    hf_home = Path(os.environ.get("HF_HOME", cache_home / "huggingface"))
    identity = json.dumps([target.url.rstrip("/"), target.model]).encode()
    return hf_home / "giraffe-tokenizers" / (hashlib.sha256(identity).hexdigest() + ".json")


def _remembered_alias(target):
    try:
        with _alias_path(target).open() as cache:
            value = json.loads(cache.read(8192)).get("tokenizer")
        return value if isinstance(value, str) and value.strip() else None
    except (OSError, ValueError, AttributeError):
        return None


def _remember_alias(target, tokenizer=None):
    """Remember a supplied or resolved mapping only after successful loading.

    Files are scoped to endpoint + served model; no credentials or URLs are
    stored. A new override replaces the mapping. Cache failures never block runs.
    """
    path = _alias_path(target)
    temporary = None
    try:
        tokenizer = tokenizer or target.tokenizer
        tokenizer = str(Path(tokenizer).resolve()) if Path(tokenizer).is_dir() else tokenizer
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as cache:
            temporary = Path(cache.name)
            json.dump({"tokenizer": tokenizer}, cache)
        temporary.replace(path)
    except OSError:
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


async def _verify_tokenizer(name, *, allow_download=True):
    process = None
    try:
        process = await _start_worker(name, allow_download=allow_download)
        ready = await _read_worker(process)
        if ready.get("error"):
            raise TokenizerUnavailable(ready["error"], code=ready.get("code", "tokenizer_unavailable"))
        return ready
    except (OSError, ValueError):
        raise TokenizerUnavailable("Could not start or read the tokenizer worker; check the installed dependencies") from None
    finally:
        if process is not None:
            if process.returncode is None:
                process.kill()
            await process.wait()


async def resolve_hub_name(name):
    """Resolve exact/repository-suffix names, never infer a model from substrings."""
    if any(os.environ.get(key, "").lower() in {"1", "true", "yes", "on"}
           for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")):
        return None
    # Only repository-shaped names go to the Hub, never URLs or local paths.
    try:
        from huggingface_hub.utils import validate_repo_id
    except ImportError:
        return None
    try:
        validate_repo_id(name)
    except ValueError:
        return None
    # A qualified ID can be loaded directly using the user's Hub credentials.
    # Public search cannot discover private repositories and must not veto them.
    if "/" in name:
        return name
    try:
        async with httpx.AsyncClient(timeout=2, follow_redirects=False) as client:
            response = await client.get(f"https://huggingface.co/api/models/{name}")
            if response.status_code == 200:
                value = response.json().get("id")
                if isinstance(value, str) and value.lower() == name.lower():
                    return value
            elif response.status_code not in {400, 401, 404}:
                return None
            response = await client.get("https://huggingface.co/api/models",
                params={"search": name, "limit": 50, "sort": "downloads", "direction": -1})
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                return None
            matches = [row for row in rows if isinstance(row, dict)
                       and isinstance(row.get("id"), str)
                       and (row["id"].lower() == name.lower()
                            or row["id"].lower().endswith("/" + name.lower()))]
            # Match AIPerf's suffix rule: prefer the most downloaded exact suffix.
            matches.sort(key=lambda row: (row["id"].lower() != name.lower(),
                         -(row.get("downloads") or 0), row["id"]))
            return matches[0]["id"] if matches else None
    except (httpx.HTTPError, ValueError, TypeError, AttributeError, ImportError):
        return None


async def _resolve_tokenizer(config, target, verified, *, include_server_metadata=True):
    from .preflight import ModelDiscovery, PreflightError, discover_models

    name, source = _remembered_alias(target) or target.model, "target_model"
    if name != target.model:
        source = "remembered_mapping"
    try:
        ready = verified.get(name) or await _verify_tokenizer(name, allow_download=False)
        verified[name] = ready
        return name, source
    except TokenizerUnavailable as error:
        if error.code == "missing_dependency":
            raise
        pass
    if include_server_metadata:
        # Server metadata is useful when a serving alias hides the actual model.
        body = ModelDiscovery(url=target.url, api_key=target._api_key,
            api_key_env=target.api_key_env, headers_env=target.headers_env,
            ca_bundle=config.ca_bundle, proxy=config.proxy)
        try:
            async with asyncio.timeout(1.5):
                models = await discover_models(body, allow_manual=True, include_tokenizers=True)
            resolved = models.get("tokenizers", {}).get(target.model)
            if resolved:
                return resolved, "server_model_root"
        except (PreflightError, TimeoutError):
            pass
    if source == "remembered_mapping" or Path(name).is_dir():
        return name, source
    resolved = await resolve_hub_name(name)
    if resolved:
        return resolved, "hub_resolution"
    return BUILTIN, "builtin_fallback"


async def ensure_tokenizers(config, *, include_server_metadata=True):
    """Resolve automatically by default, with a built-in fallback.

    Discovery is bounded to five seconds across targets; downloads and validation
    have a separate 30-second budget. No inference is used to size prompts.
    """
    if not needs_tokenizer(config):
        return
    discovery_remaining = AUTOMATIC_SETUP_TIMEOUT_SECONDS
    verified = {}
    for index, target in enumerate(config.targets):
        identity = (target.url, target.model, target.tokenizer)
        if target._tokenizer_setup.get("identity") == identity:
            continue
        name, source = target.tokenizer or "auto", "configured" if target.tokenizer else "automatic"
        automatic = name == "auto"
        if automatic:
            started = time.monotonic()
            try:
                if discovery_remaining <= 0:
                    raise TimeoutError
                async with asyncio.timeout(discovery_remaining):
                    name, source = await _resolve_tokenizer(config, target, verified, **(
                        {} if include_server_metadata else {"include_server_metadata": False}))
            except (TimeoutError, TokenizerUnavailable, ImportError):
                name, source = BUILTIN, "builtin_fallback"
            finally:
                discovery_remaining -= time.monotonic() - started
        if name == BUILTIN and source == "configured":
            source = "builtin"
        try:
            async with asyncio.timeout(SETUP_TIMEOUT_SECONDS):
                ready = verified.get(name) or await _verify_tokenizer(name)
        except (TimeoutError, TokenizerUnavailable) as error:
            if automatic and name != BUILTIN:
                name, source = BUILTIN, "builtin_fallback"
                try:
                    async with asyncio.timeout(SETUP_TIMEOUT_SECONDS):
                        ready = verified.get(name) or await _verify_tokenizer(name)
                except (TimeoutError, TokenizerUnavailable):
                    raise TokenizerUnavailable(
                        "Built-in tokenizer could not be loaded. Its first use needs internet access "
                        "to download encoding data and a writable local cache. No run was started.",
                        target_index=index) from None
            else:
                reason = "setup timed out" if isinstance(error, TimeoutError) else str(error)
                raise TokenizerUnavailable(
                    f"Tokenizer setup for {target.name} failed: {reason}. "
                    + ("Built-in encoding data downloads automatically on first use; check internet access and the local cache. "
                       if name == BUILTIN else "Select builtin to use the built-in tokenizer. ")
                    + "No run was started.", target_index=index) from None
        verified[name] = ready
        target._tokenizer_setup = {"tokenizer": name, "source": source,
            "chat_template": ready.get("chat_template", False), "identity": identity,
            "requested": target.tokenizer or "auto"}
        if name != BUILTIN:
            _remember_alias(target, name)


async def preview_tokenizer(model, token_id="auto"):
    """Resolve and load the first-page selection without endpoint credentials/traffic."""
    from .models import RunConfig

    requested = token_id.strip() or "auto"
    if requested.lower() in {"auto", "automatic", "builtin"}:
        requested = "auto" if requested.lower() == "automatic" else requested.lower()
    name = model.strip() if requested == "auto" else requested
    if not name:
        raise TokenizerUnavailable("Enter the served model name, or enter a model name in Token ID.")
    config = RunConfig(targets=[{"name": "tokenizer-preview", "url": "http://tokenizer-preview.invalid/v1",
        "model": name, "tokenizer": "builtin" if requested == "builtin" else "auto"}], checks=["load"])
    await ensure_tokenizers(config, include_server_metadata=False)
    setup = config.targets[0]._tokenizer_setup
    fallback = setup["source"] == "builtin_fallback"
    tokenizer = setup["tokenizer"]
    return {"status": "fallback" if fallback else "ready", "tokenizer": tokenizer,
        "encoding": BUILTIN_ENCODING if tokenizer == BUILTIN else None,
        "message": (f'Could not resolve or load a tokenizer for "{name}". Built-in o200k_base will be used. '
                    'Enter a model name in Token ID to try again, or choose builtin.' if fallback else
                    'Ready: built-in tiktoken o200k_base will be used. Its counts may differ from your model.'
                    if tokenizer == BUILTIN else f'Ready: {tokenizer}. Tokenizer files are cached locally.')}


def _progress(run, target, scenario, status, **metadata):
    if hasattr(run, "preparation"):
        run.preparation(target.name, scenario, "Preparing input locally", status, **metadata)


async def prepare_messages(run, target, scenario, build_messages, target_tokens, *, tolerance=.1, content_only=False):
    """Size a message builder accepting a filler-character count, without inference.

    Returns the messages actually counted, local token count, provenance and the
    filler size. No character estimate is accepted as token evidence. Unavailable
    tokenizers are cached for this run, preventing repeated expensive failures.
    """
    setup = getattr(target, "_tokenizer_setup", {})
    tokenizer = setup.get("tokenizer") or getattr(target, "tokenizer", None) or BUILTIN
    if tokenizer == "auto":
        tokenizer = BUILTIN
    builtin = tokenizer == BUILTIN
    result = dict(status="unavailable", reason=None, reason_code="tokenizer_unavailable",
                  target_tokens=target_tokens, input_tokens=None, filler_chars=0,
                  request_ids=[], method="local_tokenizer", tokenizer=tokenizer,
                  tokenizer_source=setup.get("source") or ("configured" if getattr(target, "tokenizer", None) else "builtin"),
                  chat_template=False, local_files_only=True)
    _progress(run, target, scenario, "running", tokenizer=tokenizer, target_tokens=target_tokens)
    failures = getattr(run, "_tokenizer_unavailable", {})
    run._tokenizer_unavailable = failures
    if tokenizer in failures:
        result["reason"] = failures[tokenizer]
        _progress(run, target, scenario, "unavailable", reason=result["reason"])
        return result
    process = None
    started = time.monotonic()
    budgets = getattr(run, "_tokenizer_preparation_seconds", {})
    run._tokenizer_preparation_seconds = budgets
    remaining = TARGET_PREPARATION_TIMEOUT_SECONDS - budgets.get(target.name, 0)
    if remaining <= 0:
        result["reason"] = f"Local prompt preparation reached its {TARGET_PREPARATION_TIMEOUT_SECONDS:g}s target budget; no inference calibration was sent."
        _progress(run, target, scenario, "unavailable", reason=result["reason"])
        return result
    run_deadline = getattr(run, "deadline", float("inf"))
    target_deadline = started + remaining
    deadline = min(run_deadline, target_deadline, started + PREPARATION_TIMEOUT_SECONDS)
    try:
        async with asyncio.timeout(max(.001, deadline - started)):
            process = await _start_worker(tokenizer)
            ready = await _read_worker(process)
            if ready.get("error"):
                raise TokenizerUnavailable(ready["error"])
            scope = "message_content" if content_only else ready.get("token_count_scope", "message_content")
            result.update(chat_template=ready.get("chat_template", False),
                          token_count_scope="builtin_reference" if builtin else scope)
            low, high = 0, None
            filler = 0
            for attempt in range(MAX_SIZING_ATTEMPTS):
                reason = run.stop_reason(target.name) if hasattr(run, "stop_reason") else None
                if reason and reason != "maximum request budget reached":
                    raise TokenizerUnavailable(reason)
                messages = build_messages(filler)
                process.stdin.write((json.dumps({"messages": messages, "content_only": content_only}) + "\n").encode())
                await process.stdin.drain()
                answer = await _read_worker(process)
                if answer.get("error"):
                    raise TokenizerUnavailable(answer["error"])
                tokens = answer["input_tokens"]
                result.update(input_tokens=tokens, filler_chars=filler, attempts=attempt + 1)
                _progress(run, target, scenario, "running", tokenizer=tokenizer,
                          input_tokens=tokens, target_tokens=target_tokens, attempt=attempt + 1)
                if abs(tokens - target_tokens) <= max(1, target_tokens * tolerance) or (filler == 0 and tokens > target_tokens):
                    floor = filler == 0 and tokens > target_tokens
                    result.update(status="calibrated", reason_code="minimum_prompt_length" if floor else None,
                                  reason=(f"Input target {target_tokens} is below the minimum prompt length "
                                          f"({tokens} tokens with the local tokenizer)" if floor else None),
                                  messages=messages)
                    break
                if tokens < target_tokens:
                    low = filler
                else:
                    high = filler
                if high is not None:
                    candidate = (low + high) // 2
                elif filler == 0:
                    candidate = max(1, (target_tokens - tokens) * 4)
                else:
                    candidate = min(MAX_PROMPT_CHARS, max(filler + 1, round(filler * target_tokens / max(1, tokens))))
                if candidate == filler:
                    break
                filler = candidate
            if result["status"] != "calibrated":
                result.update(reason="Local prompt sizing could not reach the requested token target within its bounded attempts.",
                              reason_code="input_sizing_unavailable")
    except asyncio.CancelledError:
        reason = run.stop_reason(target.name) if hasattr(run, "stop_reason") else None
        if reason == "maximum request budget reached":
            reason = None
        _progress(run, target, scenario, "cancelled", reason=reason or "Preparation cancelled")
        raise
    except TimeoutError:
        if deadline == run_deadline:
            # Observe the execution deadline here, before worker cleanup. The
            # monitor may wake only after this unavailable result finishes.
            reason = run.stop_reason(target.name) if hasattr(run, "stop_reason") else None
            result["reason"] = (f"Local prompt preparation stopped: {reason or 'maximum run duration reached'}; "
                                "no inference calibration was sent.")
        elif deadline == target_deadline:
            result["reason"] = f"Local prompt preparation reached its {TARGET_PREPARATION_TIMEOUT_SECONDS:g}s target budget; no inference calibration was sent."
        else:
            result["reason"] = f"Local tokenizer preparation exceeded {PREPARATION_TIMEOUT_SECONDS:g}s; no inference calibration was sent."
            failures[tokenizer] = result["reason"]
    except (TokenizerUnavailable, OSError, ValueError, KeyError) as exc:
        result["reason"] = str(exc)
        failures[tokenizer] = result["reason"]
    finally:
        if process is not None:
            if process.returncode is None:
                process.kill()
            await process.wait()
        budgets[target.name] = budgets.get(target.name, 0) + time.monotonic() - started
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    if builtin and result["status"] == "calibrated":
        result["reason"] = BUILTIN_NOTICE + (" " + result["reason"] if result["reason"] else "")
        result["encoding"] = BUILTIN_ENCODING
    _progress(run, target, scenario, "completed" if result["status"] == "calibrated" else "unavailable",
              reason=result["reason"], tokenizer=tokenizer, builtin=builtin, input_tokens=result["input_tokens"])
    return result


async def _start_worker(tokenizer, *, allow_download=False):
    env = {**os.environ, "TOKENIZERS_PARALLELISM": "false"}
    if not allow_download:
        env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    # Endpoint credentials are never forwarded to Hugging Face. Existing HF
    # credentials/offline preferences remain under the user's control.
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", "giraffe.local_tokenizer", tokenizer,
        *(["--download"] if allow_download else []),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=env,
    )


async def _read_worker(process):
    line = await process.stdout.readline()
    if not line:
        raise TokenizerUnavailable("Tokenizer worker exited before loading; check the tokenizer model ID, local files and installed dependencies.")
    return json.loads(line)


def _builtin_encoding(allow_download):
    import tiktoken
    from tiktoken import load

    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(cache_home / "giraffe" / "tiktoken"))
    offline = any(os.environ.get(key, "").lower() in {"1", "true", "yes", "on"}
                  for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"))
    if not allow_download or offline:
        # tiktoken has no offline flag. In this isolated worker, refuse cache
        # misses at its I/O boundary; cached data still uses its hash validation.
        def no_download(_path):
            raise OSError("Built-in encoding is not cached; connect once to download it")
        load.read_file = no_download
    return tiktoken.get_encoding(BUILTIN_ENCODING)


def _worker(tokenizer_name, *, allow_download=False):
    builtin = tokenizer_name == BUILTIN
    try:
        if builtin:
            tokenizer = _builtin_encoding(allow_download)
            has_template = False
            def count(messages):
                return sum(len(tokenizer.encode(m["content"], disallowed_special=())) for m in messages)
        else:
            from transformers import AutoTokenizer
            try:
                tokenizer = AutoTokenizer.from_pretrained(tokenizer_name, local_files_only=True,
                                                           trust_remote_code=False)
            except OSError:
                if not allow_download:
                    raise
                tokenizer = AutoTokenizer.from_pretrained(tokenizer_name, local_files_only=False,
                                                           trust_remote_code=False)
            has_template = bool(tokenizer.chat_template)
            def count(messages, content_only=False):
                if has_template and not content_only:
                    return len(tokenizer.apply_chat_template(messages, tokenize=True,
                        add_generation_prompt=True, return_dict=False))
                return sum(len(tokenizer.encode(message["content"], add_special_tokens=False))
                           for message in messages)
        count([{"role": "user", "content": "Tokenizer setup."}])
    except ImportError:
        print(json.dumps({"error": ("Built-in tokenizer dependencies are missing; reinstall giraffe-llm"
                                   if builtin else HF_INSTALL_HINT),
                          "code": "missing_dependency"}), flush=True)
        return
    except Exception:
        print(json.dumps({"error": "Tokenizer files could not be loaded or used. Check network access, local cache permissions and offline settings"}), flush=True)
        return
    print(json.dumps({"ready": True, "chat_template": has_template,
                      "token_count_scope": "builtin_reference" if builtin else
                          "chat_template" if has_template else "message_content"}), flush=True)
    for line in sys.stdin:
        try:
            payload = json.loads(line)
            messages = payload["messages"]
            tokens = count(messages) if builtin else count(messages, payload.get("content_only", False))
            print(json.dumps({"input_tokens": tokens}), flush=True)
        except (ImportError, ValueError, TypeError, KeyError) as exc:
            print(json.dumps({"error": f"Local tokenization unavailable: {exc}"}), flush=True)


if __name__ == "__main__":
    _worker(sys.argv[1], allow_download="--download" in sys.argv[2:])
