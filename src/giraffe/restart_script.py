"""Read and identify the local executable text a user confirms for one run."""
from __future__ import annotations

import hashlib
import os
import re
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
    except UnicodeError:
        raise ValueError("Restart script must be UTF-8 text.") from None
    if any((ord(char) < 0x20 and char not in "\n\t") or 0x7f <= ord(char) <= 0x9f
           or 0x200b <= ord(char) <= 0x200f or 0x202a <= ord(char) <= 0x202e
           or 0x2066 <= ord(char) <= 0x2069 or char in "\u2060\ufeff" for char in contents):
        raise ValueError("Restart script contains control or invisible characters. Remove them to review it safely.")
    if not contents.startswith("#!"):
        raise ValueError("Restart script must start with a #! line.")
    return {"path": str(resolved), "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data), "machine": socket.gethostname(), "contents": contents}


def script_environment(targets):
    """Inherit ordinary environment values while retaining credentials for redaction."""
    environment = os.environ.copy()
    secrets = set()
    for target in targets:
        if target._api_key is not None:
            secrets.add(target._api_key.get_secret_value())
        for name in [target.api_key_env, *target.headers_env.values()]:
            if name is not None:
                value = environment.pop(name, None)
                if value:
                    secrets.add(value)
    return environment, {value.encode("utf-8") for value in secrets if value}


def read_output_tail(path, secrets):
    """Redact credentials crossing the raw tail boundary without reading a growing log."""
    window = 4096 + max((len(value) - 1 for value in secrets), default=0)
    with path.open("rb") as source:
        source.seek(max(0, os.fstat(source.fileno()).st_size - window))
        data = source.read(window)
    start = max(0, len(data) - 4096)
    if secrets:
        alternatives = b"|".join(re.escape(value) for value in sorted(secrets, key=len, reverse=True))
        spans = []
        for match in re.finditer(b"(?=(" + alternatives + b"))", data):
            left, right = match.span(1)
            if right <= start:
                continue
            left = max(start, left)
            if spans and left < spans[-1][1]:
                spans[-1] = (spans[-1][0], max(right, spans[-1][1]))
            else:
                spans.append((left, right))
        pieces = []
        cursor = start
        for left, right in spans:
            pieces.extend((data[cursor:left], b"[redacted]"))
            cursor = right
        pieces.append(data[cursor:])
        data = b"".join(pieces)
    else:
        data = data[start:]
    return data[-4096:].decode("utf-8", errors="replace")
