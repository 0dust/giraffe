"""Read and identify the local executable text a user confirms for one run."""
from __future__ import annotations

import hashlib
import os
import socket
import stat
from pathlib import Path


MAX_SCRIPT_BYTES = 64 * 1024
CHANGED_SCRIPT = "The script changed. Review it again."


def preview_script(path: str) -> dict:
    if not path or "\x00" in path:
        raise ValueError("Restart script needs a valid local file path.")
    resolved = Path(path).expanduser().resolve()
    try:
        fd = os.open(resolved, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        raise ValueError("Restart script does not exist.") from None
    except OSError:
        raise ValueError("Restart script cannot be read.") from None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise ValueError("Restart script must be a regular file.")
    with os.fdopen(fd, "rb") as source:
        if not info.st_mode & 0o111 or not os.access(resolved, os.X_OK):
            raise ValueError("Restart script must be executable.")
        if info.st_size > MAX_SCRIPT_BYTES:
            raise ValueError("Restart script must be at most 64 KiB.")
        data = source.read(MAX_SCRIPT_BYTES + 1)
    if len(data) > MAX_SCRIPT_BYTES:
        raise ValueError("Restart script must be at most 64 KiB.")
    try:
        contents = data.decode("utf-8")
        if "\x00" in contents:
            raise UnicodeError()
    except UnicodeError:
        raise ValueError("Restart script must be UTF-8 text.") from None
    return {"path": str(resolved), "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data), "machine": socket.gethostname(), "contents": contents}
