"""Shared tokenizer fixture, watchdog and test lifecycle isolation guards."""
import asyncio
import faulthandler
from importlib.machinery import PathFinder
from importlib.util import module_from_spec
import json
import inspect
import os
from pathlib import Path
import sys
import threading
import weakref
from contextlib import nullcontext

import pytest
import pytest_asyncio


# A separately installed ``scripts`` package can shadow the repository's
# namespace directory, even when the checkout is first on sys.path. Storage
# tests deliberately exercise these repository measurement helpers.
_scripts_spec = PathFinder.find_spec("scripts", [str(Path(__file__).resolve().parents[1])])
if _scripts_spec is not None:
    sys.modules["scripts"] = module_from_spec(_scripts_spec)


@pytest.fixture(autouse=True)
def giraffe_resource_guard():
    """Attribute leaked work/state to its test, before it contaminates the next one."""
    tasks, threads, errors = weakref.WeakSet(), weakref.WeakSet(), []
    runner = sys.modules.get("giraffe.runner")
    client = getattr(runner, "LLMClient", None)
    create_task = asyncio.BaseEventLoop.create_task
    start_thread = threading.Thread.start

    def tracked_task(loop, coroutine, *args, **kwargs):
        task = create_task(loop, coroutine, *args, **kwargs)
        tasks.add(task)
        return task

    def tracked_thread(thread, *args, **kwargs):
        target = getattr(thread, "_target", None)
        owned = ((getattr(target, "__module__", "") or "").startswith("giraffe.")
                 or thread.name.startswith("giraffe-"))
        frame = sys._getframe(1)
        try:
            while not owned and frame is not None:
                owned = frame.f_globals.get("__name__", "").startswith("giraffe.")
                frame = frame.f_back
        finally:
            del frame
        if owned:
            threads.add(thread)
        return start_thread(thread, *args, **kwargs)

    # This context outlives function-scoped monkeypatch fixtures and pytest's
    # loop runner. Ordinary executor threads are joined by that runner first.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(asyncio.BaseEventLoop, "create_task", tracked_task)
        patch.setattr(threading.Thread, "start", tracked_thread)
        yield tasks, errors
        # The application-owned measurement loop stays idle between runs. Only
        # this persistent thread is exempt; per-run heartbeat/evidence/RAM
        # threads must be closed and joined before their owning test ends.
        persistent_threads = {"giraffe-run"}
        alive = sorted(thread.name for thread in threads
                       if thread.is_alive() and thread.name not in persistent_threads)
        if alive:
            errors.append("Giraffe threads left running: " + ", ".join(alive))
        if runner is not None and runner.LLMClient is not client:
            leaked = runner.LLMClient
            runner.LLMClient = client
            errors.append("Unrestored runner.LLMClient: " +
                          f"{getattr(leaked, '__module__', type(leaked).__module__)}."
                          f"{getattr(leaked, '__qualname__', type(leaked).__qualname__)}")
        if errors:
            pytest.fail("\n".join(errors), pytrace=False)


@pytest_asyncio.fixture(autouse=True)
async def giraffe_task_guard(giraffe_resource_guard):
    """Inspect before pytest closes its loop and would silently cancel orphans."""
    tasks, errors = giraffe_resource_guard
    baseline = asyncio.all_tasks()
    yield
    await asyncio.sleep(0)
    current = asyncio.current_task()
    pending = [task for task in set(tasks) | asyncio.all_tasks()
               if task not in baseline and task is not current and not task.done()]
    if pending:
        errors.append("Asyncio tasks left running: " + ", ".join(sorted(
            getattr(task.get_coro(), "__qualname__", type(task.get_coro()).__name__)
            for task in pending)))
        loop = asyncio.get_running_loop()
        local = []
        for task in pending:
            owner = task.get_loop()
            if owner.is_closed():
                continue
            if owner is loop:
                task.cancel()
                local.append(task)
            elif owner.is_running():
                owner.call_soon_threadsafe(task.cancel)
        if local:
            # Cleanup follows detection; a cooperative orphan still fails its
            # owning test. Bounded joining prevents a cascade in later tests.
            await asyncio.wait(local, timeout=.1)


# Both ASGI/test loops and the dedicated engine loop must reject cross-thread APIs.
os.environ["PYTHONASYNCIODEBUG"] = "1"


_watchdog_output = None


def pytest_configure(config):
    global _watchdog_output
    capture = config.pluginmanager.getplugin("capturemanager")
    with capture.global_and_fixture_disabled() if capture else nullcontext():
        _watchdog_output = os.fdopen(os.dup(sys.stderr.fileno()), "w")


def pytest_unconfigure(config):
    global _watchdog_output
    if _watchdog_output is not None:
        _watchdog_output.close()
        _watchdog_output = None


@pytest.fixture(autouse=True)
def test_watchdog():
    """A wedged test must terminate CI, not merely print a traceback forever."""
    seconds = float(os.environ.get("GIRAFFE_TEST_TIMEOUT_SECONDS", "120"))
    faulthandler.dump_traceback_later(seconds, file=_watchdog_output, exit=True)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()


@pytest.fixture
def local_tokenizer(monkeypatch, tmp_path):
    from giraffe import local_tokenizer as sizing
    monkeypatch.setenv("HF_HOME", str(tmp_path / "fake-tokenizer-cache"))

    async def start_worker(_name, **_kwargs):
        # Match the fake clients' count through the worker protocol. In-memory
        # I/O also works with tests' virtual clocks; dedicated tokenizer tests
        # separately exercise process launch, offline loading and termination.
        class Worker:
            returncode = None
            def __init__(self):
                self.stdin = self.stdout = self
                self.rows = [{"ready": True, "chat_template": True, "token_count_scope": "chat_template"}]
            def write(self, line):
                messages = json.loads(line)["messages"]
                self.rows.append({"input_tokens": round(sum(len(m.get("content", "")) for m in messages) / 4)})
            async def drain(self):
                pass
            async def readline(self):
                return (json.dumps(self.rows.pop(0)) + "\n").encode()
            def kill(self):
                self.returncode = -9
            async def wait(self):
                return self.returncode
        return Worker()

    monkeypatch.setattr(sizing, "_start_worker", start_worker)


@pytest.fixture
def web_auto_auth():
    """Security tests override this to exercise anonymous test clients."""
    return True


@pytest.fixture(autouse=True)
def giraffe_web_client_auth(monkeypatch, web_auto_auth):
    """Give ASGI clients real per-app credentials; production auth stays enabled."""
    if not web_auto_auth:
        return
    import httpx
    from starlette.testclient import TestClient

    test_client_init = TestClient.__init__
    test_client_signature = inspect.signature(test_client_init)

    def authenticated_test_client(client, *args, **kwargs):
        # TestClient may use a different HTTP backend or a transport without
        # .app. Authenticate from its public constructor argument instead.
        bound = test_client_signature.bind(client, *args, **kwargs)
        app = bound.arguments["app"]
        token = getattr(getattr(app, "state", None), "giraffe_token", None)
        if token:
            headers = httpx.Headers(bound.arguments.get("headers"))
            headers.setdefault("X-Giraffe-Token", token)
            bound.arguments["headers"] = dict(headers)
        return test_client_init(*bound.args, **bound.kwargs)

    def authenticated_init(original):
        def initialize(client, *args, **kwargs):
            app = getattr(kwargs.get("transport"), "app", None)
            token = getattr(getattr(app, "state", None), "giraffe_token", None)
            if token:
                headers = httpx.Headers(kwargs.get("headers"))
                headers.setdefault("X-Giraffe-Token", token)
                kwargs["headers"] = headers
            original(client, *args, **kwargs)
        return initialize

    monkeypatch.setattr(httpx.Client, "__init__", authenticated_init(httpx.Client.__init__))
    monkeypatch.setattr(httpx.AsyncClient, "__init__", authenticated_init(httpx.AsyncClient.__init__))

    monkeypatch.setattr(TestClient, "__init__", authenticated_test_client)
