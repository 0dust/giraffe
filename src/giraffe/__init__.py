"""Built-in deployment acceptance checks for an existing LLM service."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("giraffe-llm")
except PackageNotFoundError:
    # Source checkouts may not have installed distribution metadata.
    __version__ = "0.1.0a1"
