"""Run the autouse guards in fresh pytest processes, including deliberate leaks."""
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]


def guarded_run(pytester, source, *, cleanup=""):
    pytester.makeconftest(Path(__file__).with_name("conftest.py").read_text() + cleanup)
    pytester.makeini("[pytest]\nasyncio_mode = auto\n")
    pytester.makepyfile(source)
    return pytester.runpytest_subprocess("-q", "-p", "no:cacheprovider", timeout=15)


def test_guard_reports_an_unjoined_task_even_when_it_can_cancel_it(pytester):
    result = guarded_run(pytester, """
        import asyncio
        async def test_orphan():
            asyncio.create_task(asyncio.sleep(600))
    """)
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*Asyncio tasks left running: sleep*"])


def test_guard_checks_pending_tasks_on_a_closed_custom_loop(pytester):
    result = guarded_run(pytester, """
        import asyncio
        orphan = None
        def test_orphan():
            global orphan
            loop = asyncio.new_event_loop()
            orphan = loop.create_task(asyncio.sleep(600))
            loop.close()
    """)
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*Asyncio tasks left running: sleep*"])


@pytest.mark.parametrize("daemon", [False, True])
@pytest.mark.parametrize("name", ["giraffe-guard-probe", "giraffe-heartbeat", "giraffe-evidence", "giraffe-ram"])
def test_guard_reports_a_surviving_giraffe_thread(pytester, daemon, name):
    result = guarded_run(pytester, f"""
        import threading
        stop = threading.Event()
        thread = None
        def test_orphan():
            global thread
            thread = threading.Thread(target=stop.wait,
                                      name={name!r}, daemon={daemon!r})
            thread.start()
    """, cleanup="""

def pytest_sessionfinish(session):
    # Stop only after the autouse guard has diagnosed the deliberate leak.
    module = session.items[0].module
    module.stop.set()
    module.thread.join(timeout=1)
""")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines([f"*Giraffe threads left running: {name}*"])


def test_guard_names_and_restores_unmanaged_client_replacement(pytester):
    result = guarded_run(pytester, """
        from giraffe import runner
        original = runner.LLMClient
        class FakeClient:
            pass
        def test_a_leak():
            runner.LLMClient = FakeClient
        def test_b_next_test_is_not_contaminated():
            assert runner.LLMClient is original
    """)
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(["*Unrestored runner.LLMClient: *FakeClient*"])


def test_guard_accepts_managed_tasks_threads_and_client_patch(pytester):
    result = guarded_run(pytester, """
        import asyncio
        import threading
        from giraffe import runner
        async def test_clean(monkeypatch):
            monkeypatch.setattr(runner, 'LLMClient', object())
            task = asyncio.create_task(asyncio.sleep(0))
            await task
            thread = threading.Thread(target=lambda: None, name='giraffe-guard-probe')
            thread.start()
            thread.join()
            await asyncio.to_thread(lambda: None)
    """)
    result.assert_outcomes(passed=1)
