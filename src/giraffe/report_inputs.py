"""Stable identities for exact saved input messages."""

import hashlib
import json


def input_ref(messages: list[dict]) -> str:
    return hashlib.sha256(json.dumps(messages, ensure_ascii=False, allow_nan=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def verify_input(reference: str, messages: list[dict]) -> None:
    if not isinstance(messages, list) or input_ref(messages) != reference:
        raise ValueError("Saved input SHA-256 mismatch")
