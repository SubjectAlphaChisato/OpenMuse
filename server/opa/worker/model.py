"""Model adapter interface (DEV-01) plus the scripted and Anthropic implementations."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from opa.core.settings import Settings


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class GenerateResult:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str  # end_turn | tool_use | max_tokens | refusal | error
    usage: dict[str, int] = field(default_factory=dict)
    refusal_detail: str | None = None
    raw_content: list[dict[str, Any]] = field(default_factory=list)


class ModelAdapter(Protocol):
    name: str

    async def generate(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], limits: dict[str, Any], *, system: str) -> GenerateResult: ...


class ModelSetupError(RuntimeError):
    """Raised when the configured provider cannot be used (missing key, unknown provider)."""


class ScriptedAdapter:
    """Deterministic adapter. Script format (YAML or JSON):

    default: {text: "...", stop_reason: end_turn}
    match:
      - contains: "brief"          # substring of the latest user message
        steps:                     # consumed in order per task run
          - {tool_calls: [{name: x, input: {...}}]}
          - {text: "done"}
    """

    name = "scripted"

    def __init__(self, script: dict[str, Any] | None = None, path: str | None = None) -> None:
        if script is None and path:
            script = yaml.safe_load(Path(path).read_text()) or {}
        self.script = script or {"default": {"text": "OK. (scripted model)", "stop_reason": "end_turn"}}

    def _pick(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Rules match the task's first user message (the intent), so a restart or a user
        reply does not change which script applies."""
        intent = ""
        for m in messages:
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                intent = m["content"]
                break
        for rule in self.script.get("match", []):
            if rule.get("contains", "") in intent:
                return list(rule.get("steps", []))
        return [self.script.get("default", {"text": "OK. (scripted model)"})]

    async def generate(self, messages, tools, limits, *, system: str) -> GenerateResult:
        steps = self._pick(messages)
        # The step index is derived from history, so the adapter is stateless across restarts.
        idx = sum(1 for m in messages if m.get("role") == "assistant")
        idx = min(idx, len(steps) - 1)
        step = steps[idx] if steps else {"text": "OK."}
        last = self._last_result(messages)
        calls = [ToolCall(id=f"call_{idx}_{n}", name=c["name"], input=self._resolve_last(self._resolve_auto(c.get("input", {}), c["name"], limits.get("catalog_tools") or tools), last))
                 for n, c in enumerate(step.get("tool_calls", []))]
        stop = step.get("stop_reason") or ("tool_use" if calls else "end_turn")
        return GenerateResult(
            text=step.get("text", ""),
            tool_calls=calls,
            stop_reason=stop,
            usage={"input_tokens": 0, "output_tokens": 0},
            refusal_detail=step.get("refusal_detail"),
        )


    @staticmethod
    def _resolve_auto(inp: dict[str, Any], name: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
        """Scripts may write "@auto" for a value the tool schema restricts to an enum (e.g. connection_id)."""
        tool = next((t for t in tools if t["name"] == name), None)
        out = dict(inp)
        for k, v in inp.items():
            if v == "@auto" and tool:
                enum = tool["input_schema"].get("properties", {}).get(k, {}).get("enum") or [""]
                out[k] = enum[0]
        return out


    @staticmethod
    def _last_result(messages: list[dict[str, Any]]) -> dict[str, Any]:
        for m in reversed(messages):
            if m.get("role") == "user" and isinstance(m.get("content"), list):
                for b in m["content"]:
                    if b.get("type") == "tool_result":
                        try:
                            return (json.loads(str(b.get("content")).split("\n", 1)[0]) or {}).get("data") or {}
                        except ValueError:
                            return {}
        return {}

    @classmethod
    def _resolve_last(cls, value: Any, last: dict[str, Any]) -> Any:
        """Scripts may write "@last.<key>" to reuse a value from the most recent tool result (e.g. a new artifact ID)."""
        if isinstance(value, str) and value.startswith("@last."):
            return last.get(value[len("@last."):])
        if isinstance(value, dict):
            return {k: cls._resolve_last(v, last) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._resolve_last(v, last) for v in value]
        return value


class AnthropicAdapter:
    """Real provider adapter. Follows the current Claude API contract:
    adaptive thinking (no `thinking` param), `output_config.effort`, strict tools,
    tool_choice auto, refusal stop reason handled, server-side fallbacks enabled."""

    name = "anthropic"

    def __init__(self, settings: Settings) -> None:
        if not settings.anthropic_api_key:
            raise ModelSetupError("OPA_ANTHROPIC_API_KEY is not set")
        from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

        # The worker's only route to the model is the egress proxy when one is configured.
        http_client = DefaultAsyncHttpxClient(proxy=settings.model_proxy) if settings.model_proxy else None
        self.client = AsyncAnthropic(api_key=settings.anthropic_api_key, http_client=http_client)
        self.model = settings.model_id
        self.effort = settings.model_effort

    async def generate(self, messages, tools, limits, *, system: str) -> GenerateResult:
        from anthropic import APIConnectionError, APIStatusError, RateLimitError

        api_tools = [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t["input_schema"],
                "strict": True,
            }
            for t in tools
        ]
        try:
            resp = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=messages,
                tools=api_tools or None,  # type: ignore[arg-type]
                output_config={"effort": self.effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except RateLimitError as e:
            return GenerateResult(text="", tool_calls=[], stop_reason="error", refusal_detail=f"rate_limited: {e}")
        except APIStatusError as e:
            return GenerateResult(text="", tool_calls=[], stop_reason="error", refusal_detail=f"provider_error {e.status_code}: {e.message}")
        except APIConnectionError as e:
            return GenerateResult(text="", tool_calls=[], stop_reason="error", refusal_detail=f"provider_unavailable: {e}")

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        raw: list[dict[str, Any]] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
                raw.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                inp = block.input if isinstance(block.input, dict) else json.loads(json.dumps(block.input))
                calls.append(ToolCall(id=block.id, name=block.name, input=inp))
                raw.append({"type": "tool_use", "id": block.id, "name": block.name, "input": inp})
            # thinking blocks are never persisted (TASK-01)
        detail = None
        if resp.stop_reason == "refusal" and getattr(resp, "stop_details", None):
            sd = resp.stop_details
            detail = f"{getattr(sd, 'category', None)}: {getattr(sd, 'explanation', '')}"
        usage = {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
        }
        return GenerateResult("".join(text_parts), calls, resp.stop_reason or "end_turn", usage, detail, raw)


def build_adapter(settings: Settings) -> ModelAdapter:
    if settings.model_provider == "scripted":
        return ScriptedAdapter(path=settings.scripted_model_path)
    if settings.model_provider == "anthropic":
        if settings.mode == "demo":
            raise ModelSetupError("demo mode never makes provider calls; set OPA_MODEL_PROVIDER=scripted")
        return AnthropicAdapter(settings)
    raise ModelSetupError(f"unknown model provider {settings.model_provider!r}")


def setup_problems(settings: Settings) -> list[str]:
    """Actionable setup state for /api/me (DEV-01)."""
    problems: list[str] = []
    if settings.mode == "live" and settings.model_provider == "scripted":
        problems.append("model: live mode requires a real provider (OPA_MODEL_PROVIDER=anthropic)")
    if settings.model_provider == "anthropic" and not settings.anthropic_api_key:
        problems.append("model: OPA_ANTHROPIC_API_KEY is not set")
    return problems
