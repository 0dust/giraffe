"""Run one 1B engine and Giraffe on the same Colab VM; never expose a tunnel."""

import argparse
import collections
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine", choices=["llamacpp", "lmdeploy", "tgi", "tensorrt-llm"])
    args = parser.parse_args()
    root = Path("/content/giraffe-engine-validation")
    root.mkdir(parents=True, exist_ok=True)
    lock = (root / "engine.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    output = root / (args.engine + "-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime()))
    output.mkdir()
    engine_env = root / ("env-" + args.engine)
    suite_env = root / "suite-env"
    env = os.environ.copy()
    env.update(TOKENIZERS_PARALLELISM="false", OMP_NUM_THREADS="2", CARGO_BUILD_JOBS="2")
    env["PATH"] = str(engine_env / "bin") + ":/root/.cargo/bin:" + env["PATH"]
    env["VIRTUAL_ENV"] = str(engine_env)
    # Colab can force uv into the system interpreter through inherited settings.
    env.pop("UV_SYSTEM_PYTHON", None)
    env["UV_PYTHON"] = str(engine_env / "bin/python")
    env["UV_PROJECT_ENVIRONMENT"] = str(engine_env)
    process = None

    def command(argv, *, cwd=None, timeout=1800):
        print("Running:", " ".join(map(str, argv)), flush=True)
        with (output / "installation.log").open("a") as log:
            log.write("\n" + " ".join(map(str, argv)) + "\n")
            log.flush()
            subprocess.run(list(map(str, argv)), check=True, cwd=cwd, env=env, timeout=timeout,
                           stdout=log, stderr=subprocess.STDOUT)

    try:
        try:
            with socket.create_connection(("127.0.0.1", 18080), timeout=1):
                raise RuntimeError("Port 18080 is already occupied. Use a fresh Colab runtime before loading another engine.")
        except OSError:
            pass
        hardware = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total,compute_cap", "--format=csv,noheader"],
            text=True, timeout=15,
        ).strip()
        (output / "hardware.txt").write_text(hardware + "\n")
        print(hardware, flush=True)
        active = subprocess.check_output(
            ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"], text=True, timeout=15,
        ).strip()
        if active:
            raise RuntimeError("GPU processes are already active. Stop the previous engine or use a fresh runtime.")
        capability = float(hardware.splitlines()[0].split(",")[-1].strip())
        if args.engine == "tensorrt-llm" and capability < 8:
            raise RuntimeError("This TensorRT-LLM PyTorch setup requires Ampere or newer; select A100/L4 rather than T4.")

        command([sys.executable, "-m", "pip", "install", "uv"])
        uv = [sys.executable, "-m", "uv"]
        for venv in (suite_env, engine_env):
            if not (venv / "bin/python").exists():
                command(uv + ["venv", "--python", "3.11", "--seed", str(venv)])
        suite_python = suite_env / "bin/python"
        command(uv + ["pip", "install", "--python", str(suite_python), str(root / "source"), "huggingface-hub"])

        model_id = "unsloth/Llama-3.2-1B-Instruct"
        model = root / "model-1b"
        if args.engine != "llamacpp":
            # Resolve and record the revision, then download that exact revision.
            download = (
                "from huggingface_hub import HfApi, snapshot_download; import json; from pathlib import Path; "
                f"repo={model_id!r}; revision=HfApi().model_info(repo).sha; "
                f"Path({str(output / 'model.json')!r}).write_text(json.dumps(dict(repo=repo,revision=revision))); "
                f"snapshot_download(repo,revision=revision,local_dir={str(model)!r},"
                "allow_patterns=['*.json','*.safetensors','*.model','*.txt','*.jinja'])"
            )
            command([suite_python, "-c", download])

        python = engine_env / "bin/python"
        port = 18080
        if args.engine == "llamacpp":
            source = root / "llama.cpp"
            if not source.exists():
                command(["git", "clone", "--depth", "1", "--branch", "b11696",
                         "https://github.com/ggml-org/llama.cpp", source])
            command(["cmake", "-S", source, "-B", source / "build", "-DGGML_CUDA=ON",
                     f"-DCMAKE_CUDA_ARCHITECTURES={int(capability * 10)}", "-DLLAMA_BUILD_TESTS=OFF"])
            command(["cmake", "--build", source / "build", "--target", "llama-server", "-j2"])
            gguf = root / "Llama-3.2-1B-Instruct-Q4_K_M.gguf"
            download = (
                "from huggingface_hub import hf_hub_download; import shutil; "
                "p=hf_hub_download('bartowski/Llama-3.2-1B-Instruct-GGUF',"
                "'Llama-3.2-1B-Instruct-Q4_K_M.gguf',revision='067b946cf014b7c697f3654f621d577a3e3afd1c'); "
                f"shutil.copyfile(p,{str(gguf)!r})"
            )
            command([suite_python, "-c", download])
            with gguf.open("rb") as model_file:
                digest = hashlib.file_digest(model_file, "sha256").hexdigest()
            (output / "model.json").write_text(json.dumps({"model": gguf.name, "sha256": digest}))
            launch = [source / "build/bin/llama-server", "--model", gguf, "--alias", "llama3.2-1b",
                      "--host", "127.0.0.1", "--port", str(port), "--ctx-size", "8192",
                      "--parallel", "2", "--threads", "2", "--batch-size", "256",
                      "--ubatch-size", "128", "--n-gpu-layers", "999", "--metrics", "--jinja"]
        elif args.engine == "lmdeploy":
            command(uv + ["pip", "install", "--python", str(python), "lmdeploy==0.18.0"])
            launch = [engine_env / "bin/lmdeploy", "serve", "api_server", model,
                      "--server-name", "127.0.0.1", "--server-port", str(port),
                      "--model-name", "llama3.2-1b", "--session-len", "4096",
                      "--max-batch-size", "2", "--cache-max-entry-count", "0.2"]
        elif args.engine == "tensorrt-llm":
            command(["apt-get", "update"])
            command(["apt-get", "install", "-y", "libopenmpi-dev", "openmpi-bin"])
            command(uv + ["pip", "install", "--python", str(python), "tensorrt-llm==1.2.1",
                          "--extra-index-url", "https://pypi.nvidia.com"])
            launch = [engine_env / "bin/trtllm-serve", model, "--backend", "pytorch",
                      "--host", "127.0.0.1", "--port", str(port), "--max_batch_size", "2",
                      "--max_num_tokens", "4096", "--free_gpu_memory_fraction", "0.2"]
        else:
            # Build the official unoptimized server path, avoiding Docker-in-Colab.
            # This is a compatibility experiment, not an optimized TGI performance run.
            python_libdir = subprocess.check_output(
                [str(python), "-c", "import sysconfig; print(sysconfig.get_config_var('LIBDIR'))"],
                text=True, env=env, timeout=15,
            ).strip()
            env["LD_LIBRARY_PATH"] = python_libdir + ":" + env.get("LD_LIBRARY_PATH", "")
            command(["apt-get", "update"])
            command(["apt-get", "install", "-y", "build-essential", "pkg-config", "libssl-dev", "protobuf-compiler"])
            if not shutil.which("cargo", path=env["PATH"]):
                installer = root / "rustup.sh"
                urllib.request.urlretrieve("https://sh.rustup.rs", installer)
                command(["sh", installer, "-y", "--profile", "minimal"])
            source = root / "text-generation-inference"
            if not source.exists():
                command(["git", "clone", "--depth", "1", "--branch", "v3.3.5",
                         "https://github.com/huggingface/text-generation-inference", source])
            command(["make", "install-server-cpu"], cwd=source)
            # v3.3.5 generates stubs with 1.71.0 but its lock selects 1.70.0.
            command(uv + ["pip", "install", "--python", str(python), "grpcio==1.71.0"])
            # TGI's Docker build prefetches the kernels locked by server/pyproject.toml.
            # Runtime imports only use that cache, even when custom kernels are disabled.
            command([engine_env / "bin/kernels", "download", "."], cwd=source / "server")
            # GPU imports require this extension even on the generic model path.
            # The official image uses the same Makefile-flash-att revision and
            # PyTorch 2.7. Pin its dependency layer instead of compiling 57 CUDA objects.
            layer_norm = subprocess.run([str(python), "-c", "import torch, dropout_layer_norm"],
                                        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if layer_norm.returncode:
                layer_digest = "255625c39b0a97739da55734be687ce0188504ffeab0da8e96fda2202f13fbca"
                repository = "huggingface/text-generation-inference"
                registry = "https://ghcr.io"
                with urllib.request.urlopen(registry + "/token?scope=repository:" + repository + ":pull", timeout=60) as response:
                    token = json.load(response)["token"]
                blob = root / "tgi-layernorm.tar.gz"
                request = urllib.request.Request(registry + "/v2/" + repository + "/blobs/sha256:" + layer_digest,
                                                 headers={"Authorization": "Bearer " + token})
                with urllib.request.urlopen(request, timeout=60) as response, blob.open("wb") as destination:
                    shutil.copyfileobj(response, destination)
                with blob.open("rb") as downloaded:
                    if hashlib.file_digest(downloaded, "sha256").hexdigest() != layer_digest:
                        raise RuntimeError("Official TGI dependency layer checksum mismatch")
                platlib = subprocess.check_output(
                    [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['platlib'])"],
                    text=True, env=env, timeout=15,
                ).strip()
                filename = "dropout_layer_norm.cpython-311-x86_64-linux-gnu.so"
                with tarfile.open(blob, "r:gz") as archive:
                    member = next(item for item in archive if item.isfile() and item.name.endswith("/" + filename))
                    with archive.extractfile(member) as library, (Path(platlib) / filename).open("wb") as destination:
                        shutil.copyfileobj(library, destination)
                (output / "vendor-dependency.json").write_text(json.dumps({
                    "image_repository": repository, "layer_sha256": layer_digest,
                    "image_source_revision": "db931fcf42da018a3657580c64bf2720f167f9ab",
                    "dependency_revision": "ceee0de88c037ee6eda5e75c813a8648e4bcb1c9",
                }, indent=2))
                command([python, "-c", "import torch, dropout_layer_norm; print(torch.__version__, dropout_layer_norm.__file__)"])
            command(["cargo", "install", "--locked", "--path", "backends/v3", "--root", engine_env], cwd=source, timeout=3600)
            command(["cargo", "install", "--locked", "--path", "launcher", "--root", engine_env], cwd=source, timeout=3600)
            launch = [engine_env / "bin/text-generation-launcher", "--model-id", model,
                      "--hostname", "127.0.0.1", "--port", str(port), "--num-shard", "1",
                      "--max-input-tokens", "3840", "--max-total-tokens", "4096",
                      "--max-batch-total-tokens", "8192", "--max-concurrent-requests", "2",
                      "--disable-custom-kernels"]

        with (output / "packages.txt").open("w") as packages:
            subprocess.run([str(python), "-m", "pip", "freeze"], env=env, check=True, stdout=packages)
        launch = list(map(str, launch))
        (output / "launch.json").write_text(json.dumps(launch, indent=2))
        with (output / "server.log").open("w") as log:
            process = subprocess.Popen(launch, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        ready = time.monotonic() + 600
        while time.monotonic() < ready:
            if process.poll() is not None:
                raise RuntimeError("Engine exited; inspect server.log")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=2) as response:
                    models = json.load(response)
                break
            except (OSError, ValueError):
                time.sleep(2)
        else:
            raise RuntimeError("Engine readiness exceeded 600 seconds; inspect server.log")

        config = json.loads((root / "config.json").read_text())
        # Complete the bounded matrix even when an engine returns many errors.
        # This changes only the abort threshold, never the acceptance limits.
        config["stop_after_errors"] = config["max_requests"] + 1
        target = config["targets"][0]
        target.update(name=args.engine + "-1b", url=f"http://127.0.0.1:{port}/v1",
                      model=models["data"][0]["id"], metrics_profile=args.engine,
                      deployment={"discovery": args.engine, "runtime": {"engine": args.engine},
                                  "serving": {"context_limit": 4096}})
        target["metrics_url"] = f"http://127.0.0.1:{port}/metrics" if args.engine != "tensorrt-llm" else None
        (output / "config.json").write_text(json.dumps(config, indent=2))
        result = subprocess.run([str(suite_env / "bin/giraffe"), "run", "--config", str(output / "config.json"),
                                 "--output", str(output / "run")], env=env, timeout=1900)
        report_file = output / "run/report.json"
        if not report_file.exists():
            raise RuntimeError(f"Giraffe returned {result.returncode} without a report")
        report = json.loads(report_file.read_text())
        counts = collections.Counter(check["status"] for check in report["checks"])
        print("Observed 17-check results:", dict(counts), flush=True)
        for check in report["checks"]:
            print(check["id"], check["status"], check["summary"], flush=True)
    except Exception as exc:
        # Startup/installation failures are never engine acceptance failures.
        config = json.loads((root / "config.json").read_text())
        summary = {"engine": args.engine, "execution": "unqualified", "reason": str(exc),
                   "checks": [{"check_id": name, "status": "inconclusive", "reason": "No complete live report"}
                              for name in config["checks"]]}
        (output / "inconclusive.json").write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary, indent=2), flush=True)
    finally:
        if process:
            # The launcher can exit before its GPU worker processes.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=10)
        archive = shutil.make_archive(str(output), "zip", output)
        print("Engine stopped. Results archive:", archive, flush=True)


if __name__ == "__main__":
    main()
