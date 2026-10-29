# Offline and container use

[Documentation index](README.md) · [Running and baselines](running.md)

## Prepare an offline installation

Checks that size synthetic inputs need dependency wheels **and cached tokenizer
files**. Giraffe does not download model weights. Online tokenizer preparation may
download data from Hugging Face or the built-in encoding's data host. Inference,
optional metrics and optional AI evaluator requests use configured endpoints.

From the checkout on an online machine with a compatible Python version, OS and
architecture, activate the environment where Giraffe is installed and prepare:

```sh
python -m pip wheel . --wheel-dir wheelhouse
export TIKTOKEN_CACHE_DIR="$PWD/tokenizer-cache"
python -c 'import tiktoken; tiktoken.get_encoding("o200k_base")'
```

Transfer `wheelhouse/` and `tokenizer-cache/` to the offline machine. In an
activated Python environment there:

```sh
python -m pip install --no-index --find-links wheelhouse giraffe-llm
export TIKTOKEN_CACHE_DIR="$PWD/tokenizer-cache"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
```

Choose **builtin** in the UI's **Token ID** field, or set
`targets[].tokenizer: builtin` in your configuration. Built-in counts are reference
counts; server-reported usage determines measured input-size coverage.

For model-specific tokenization, prepare the chosen tokenizer online and transfer
its Hugging Face cache too (`HF_HOME`), or use a local tokenizer directory.
Dependency wheels do not contain tokenizer files. Offline flags restrict model
hub access; Giraffe still needs access to the configured inference, metrics and
evaluator endpoints.

## Docker

From the checkout, with Docker available:

```sh
docker build -t giraffe .
docker run --rm -v "$PWD:/work" giraffe run \
  --url http://host.docker.internal:11434/v1 --model llama3.2:3b \
  --checks access,serving,correctness --simultaneous-limit 1 --default-concurrency 1 --html \
  --output runs/container
```

Start the inference server and load that model separately. The mounted directory
receives `runs/container/report.json` and `report.html`. The image installs Giraffe
and its dependencies; tokenizer caches still need preparation for offline use.

`host.docker.internal` is provided by Docker Desktop. On Linux, use a reachable
service address or an appropriate Docker host mapping. For authentication, pass
the named variable to Docker, for example `docker run -e INFERENCE_API_KEY ...`,
and configure `api_key_env` or `--api-key-env INFERENCE_API_KEY` for the target.

These commands describe the supported configuration path. The
[historical validation record](validation/2026-10-08.md) contains no live Docker
qualification for the current alpha.
