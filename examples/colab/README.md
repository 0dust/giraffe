# Colab validation

Build a notebook containing the current local Giraffe source and all 17 checks:

```sh
python examples/colab/build_notebook.py --output /tmp/Giraffe-1B-17-checks.ipynb
# Build with an engine already selected:
python examples/colab/build_notebook.py --engine tgi --output /tmp/Giraffe-TGI-1B.ipynb
```

Upload the generated notebook to Google Drive and open it with Google Colab.
Choose a GPU runtime, select one engine, and run all cells. Run each engine in a
fresh runtime. The model is Llama 3.2 1B Instruct: a public Hugging Face copy for
TensorRT-LLM/LMDeploy/TGI and a Q4_K_M GGUF for llama.cpp. Record and compare
quantization separately; these are engine compatibility runs, not equivalent
performance benchmarks.

Pinned engine versions: llama.cpp b11696, LMDeploy 0.18.0, TGI 3.3.5,
TensorRT-LLM 1.2.1. The TensorRT-LLM PyTorch setup rejects pre-Ampere GPUs; use
an A100 or compatible L4. Colab allocation, driver compatibility, package
installation and model execution must be verified in the actual assigned runtime.
The TGI recipe builds from official source, downloads its locked kernels, and
uses a SHA256-pinned layer-normalization binary from Hugging Face's official image.
That component uses the same pinned source revision and PyTorch version.
The source recipe also aligns gRPC with its generated stubs. The suite retains
its request/time budgets and acceptance limits while continuing through request
errors so that all 17 checks can finish.
It launches with `--disable-custom-kernels`; this compatibility run does not
validate the optimized CUDA image. Source compilation can take time.

The server binds to loopback and Giraffe runs beside it. No tunnel, API key, or
Google Drive filesystem mount is required. GPU observations still need an
exporter; native server-cancellation proof is unavailable in these adapters.
The final cell downloads a ZIP with raw logs and the JSON/HTML report. A failed
installation or startup is recorded separately from acceptance failures.

Google Drive file tools can upload the notebook; they cannot allocate a Colab
GPU or execute notebook cells. Upload verification is not execution verification.
