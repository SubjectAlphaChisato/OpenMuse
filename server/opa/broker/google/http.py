"""The HTTP library for all Google traffic.

Authlib's httpx integration prefers `httpx2` when installed (the Anthropic SDK 1.x installs it) and
falls back to `httpx`. Google calls use the same library so one transport serves OAuth and API calls.
"""
try:
    import httpx2 as hx
except ImportError:  # pragma: no cover
    import httpx as hx  # type: ignore[no-redef]

__all__ = ["hx"]
