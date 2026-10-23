"""Explicit local tokenizer fixture for tests with character-count fake endpoints."""
import json

import pytest


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
