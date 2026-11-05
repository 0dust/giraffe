"""Two logical replicas behind real localhost HTTP; no external deployment.

Only tests call remove(). The read-only collector reports the router's actual
admission/completion history, never a Giraffe verdict. IDs name incarnations and
individual server attempts, including requests outside the measured cohort.
"""
from __future__ import annotations

import copy
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeReplicaRouter:
    def __init__(self, *, long_replica="replica-b-boot-1", attribution=True,
                 terminal="stop", release_delay=.35):
        self.a, self.b = "replica-a-boot-1", "replica-b-boot-1"
        self.long_replica = long_replica
        self.attribution = attribution
        self.terminal = terminal
        self.release_delay = release_delay
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.removing = threading.Event()
        self.started = time.monotonic()
        self.sequence = 0
        self.events = []
        self.inventories = []
        self.attempts = []
        self.active = {}
        self.mode = None
        self.removed_at = None
        self.requested = False
        self.terminated = False
        self.late_admitted = False
        self.late_admission_seen = threading.Event()
        self.collector_reads = 0
        self._inventory([self.a, self.b])

    def _stamp(self):
        self.sequence += 1
        return dict(event_id=f"source-{self.sequence}", sequence=self.sequence,
                    time_ms=(time.monotonic() - self.started) * 1000)

    def _inventory(self, replicas):
        self.inventories.append({**self._stamp(), "replicas": replicas})

    def _fact(self, kind, replica, **fields):
        self.events.append({**self._stamp(), "kind": kind, "replica_id": replica, **fields})

    def _terminate(self):
        if not self.terminated:
            self._fact("replica_terminated", self.b)
            self.terminated = True
            self._inventory([self.a])

    def request_removal(self):
        """Record external removal intent separately from the routing cutoff."""
        with self.lock:
            if not self.requested:
                self._fact("removal_requested", self.b)
                self.requested = True

    def remove(self, mode="graceful"):
        """External test action; the inference and collector APIs cannot remove."""
        if mode not in {"graceful", "abrupt", "late_admission", "retry_survivor"}:
            raise ValueError(mode)
        with self.lock:
            if self.mode is not None:
                raise RuntimeError("One removal per fixture")
            self.mode = mode
            self.removed_at = time.monotonic()
            self.request_removal()
            self._fact("drain_effective", self.b)
            if mode == "abrupt":
                self._terminate()
            elif not any(replica == self.b for replica in self.active.values()):
                self._terminate()
            self.removing.set()

    def export(self):
        with self.lock:
            self.collector_reads += 1
            value = dict(schema_version=1, target="service", source="test-router",
                         clock_id="router-boot-1", through_sequence=self.sequence,
                         captured_at=datetime.now(timezone.utc).isoformat(), clock_uncertainty_ms=1,
                         inventories=copy.deepcopy(self.inventories), events=copy.deepcopy(self.events),
                         coverage=[dict(replica_id=self.b, start_sequence=0, end_sequence=self.sequence,
                                        complete=True, scope="all", gaps=[], truncated=False)])
            # Bounds are a fixture invariant, not silent truncation of evidence.
            assert len(value["events"]) <= 256
            assert len(json.dumps({"scale_down_lifecycle": value}).encode()) <= 65536
            return {"scale_down_lifecycle": value}

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    @property
    def collector_url(self):
        return self.url.removesuffix("/v1") + "/configuration"

    def __enter__(self):
        router = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def _json(self, status, value):
                content = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(content)
                self.close_connection = True

            def do_GET(self):
                if self.path == "/configuration":
                    self._json(200, router.export())
                elif self.path == "/v1/models":
                    self._json(200, {"data": [{"id": "fake-model"}]})
                else:
                    self._json(404, {})

            def do_POST(self):
                if self.path != "/v1/chat/completions":
                    self._json(404, {})
                    return
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                long = any(message.get("content") == "LONG" for message in payload["messages"])
                with router.lock:
                    replica = router.long_replica if long else router.a
                    if (not long and router.mode == "late_admission" and not router.late_admitted
                            and not router.terminated):
                        replica = router.b
                        router.late_admitted = True
                    attempt = f"attempt-{len(router.attempts) + 1}"
                    request_id = f"request-{attempt}"
                    correlation = request_id if router.attribution else f"unmapped-{attempt}"
                    router.attempts.append(dict(request_id=request_id, attempt_id=attempt,
                                                replica_id=replica, long=long))
                    router.active[attempt] = replica
                    router._fact("request_admitted", replica, request_id=correlation, attempt_id=attempt)
                    if not long and replica == router.b and router.mode == "late_admission":
                        router.late_admission_seen.set()
                outcome = "failed"
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    if router.attribution:
                        self.send_header("x-replica-id", replica)
                    self.send_header("Connection", "close")
                    self.end_headers()

                    def chunk(content=None, finish=None):
                        value = dict(id=request_id, model="fake-model", choices=[dict(index=0,
                            delta={"content": content} if content is not None else {}, finish_reason=finish)])
                        self.wfile.write(f"data: {json.dumps(value)}\n\n".encode())
                        self.wfile.flush()

                    chunk("OK")
                    if long:
                        while not router.stop.wait(.025):
                            if router.removing.is_set():
                                if router.mode == "abrupt" and replica == router.b:
                                    return  # Real EOF, without finish_reason or DONE.
                                if router.mode == "retry_survivor" and replica == router.b:
                                    # One controlled backend retry behind the same
                                    # HTTP response; never overwrite the original.
                                    with router.lock:
                                        router._fact("request_completed", replica, request_id=correlation,
                                                     attempt_id=attempt, outcome="failed")
                                        del router.active[attempt]
                                        replica = router.a
                                        attempt = f"attempt-{len(router.attempts) + 1}"
                                        router.attempts.append(dict(request_id=request_id, attempt_id=attempt,
                                                                    replica_id=replica, long=True))
                                        router.active[attempt] = replica
                                        router._fact("request_admitted", replica, request_id=correlation,
                                                     attempt_id=attempt)
                                        router._terminate()
                                elapsed = time.monotonic() - router.removed_at
                                if router.mode == "late_admission" and not router.late_admission_seen.is_set():
                                    # Bound the synchronization, but never emit a
                                    # successful long completion without admission.
                                    if elapsed >= 1.5:
                                        return
                                elif elapsed >= router.release_delay:
                                    break
                            chunk(" ")
                    if router.stop.is_set():
                        return
                    if long and router.terminal == "eof":
                        return
                    if long and router.terminal == "sse_error":
                        self.wfile.write(b'data: {"error":{"message":"controlled stream failure"}}\n\n')
                    else:
                        chunk(finish=router.terminal if long else "stop")
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    outcome = "completed" if not long or router.terminal == "stop" else "failed"
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                finally:
                    self.close_connection = True
                    with router.lock:
                        router._fact("request_completed", replica, request_id=correlation,
                                     attempt_id=attempt, outcome=outcome)
                        del router.active[attempt]
                        if (router.mode is not None and not router.terminated
                                and not any(r == router.b for r in router.active.values())):
                            router._terminate()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
