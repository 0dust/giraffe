"""Optional vLLM V1 scheduler observer, loaded only inside the inference server.

Use --scheduler-cls giraffe.vllm_cancellation_hooks.CancellationScheduler.
The scheduler API is version-sensitive; validated against vLLM 0.30.0.
"""

import json
import os
import re
import stat
import time

from vllm.v1.core.sched.scheduler import Scheduler


class CancellationScheduler(Scheduler):
    def _free_request(self, request, delay_free_blocks=False):
        result = super()._free_request(request, delay_free_blocks=delay_free_blocks)
        # A dispatch acknowledgement or delayed KV transfer is insufficient.
        if request.request_id in self.requests:
            return result
        match = re.fullmatch(r"(.+)-[0-9a-f]{8}", request.request_id)
        reason = {"FINISHED_ABORTED": "abort", "FINISHED_STOPPED": "stop",
                  "FINISHED_LENGTH_CAPPED": "length"}.get(request.status.name)
        path, model = os.environ.get("GIRAFFE_TRACE_FILE"), os.environ.get("GIRAFFE_TRACE_MODEL")
        if not match or not reason or not path or not model:
            return result
        ended = time.time_ns()
        row = {"schema_version": 1, "source": "vllm::scheduler",
               "request_id": match[1], "internal_request_id": request.request_id,
               "model": model, "finish_reasons": [reason],
               "started_at_ns": int(request.arrival_time * 1e9),
               "finished_at_ns": ended, "received_at_ns": time.time_ns()}
        data = json.dumps(row).encode() + b"\n"
        # Observability must never prevent the runtime from freeing a request.
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
            try:
                info = os.fstat(fd)
                if stat.S_ISREG(info.st_mode) and info.st_size + len(data) <= 16 * 1024 * 1024 and len(data) <= 16 * 1024:
                    os.write(fd, data)
            finally:
                os.close(fd)
        except OSError:
            pass
        return result
