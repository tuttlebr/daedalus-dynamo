"""AIPerf compatibility for Dynamo-specific chat-completions behavior.

Dynamo can emit named SSE events such as reasoning_dispatch/tool_call_dispatch
alongside OpenAI chat.completion.chunk data frames. AIPerf's OpenAI chat parser
currently sees only the JSON data frame and expects every frame to have an
OpenAI "object" discriminator. Ignore discriminator-less side-channel frames so
they do not turn successful requests into parser failures.

The benchmark can also attach Dynamo agentic hints through --extra-inputs.
AIPerf generates per-request output token limits, so update agent_hints.osl from
the actual request payload before sending.
"""

from __future__ import annotations

import os
from typing import Any


def _enabled(name: str, default: bool = True) -> bool:
    value = os.environ.get(name, str(default).lower()).lower()
    return value in {"1", "true", "yes", "on"}


def _patch_dynamo_stream_events() -> None:
    if not _enabled("AIPERF_PATCH_DYNAMO_STREAM_EVENTS"):
        return

    try:
        from aiperf.endpoints.openai_chat import ChatEndpoint
    except ModuleNotFoundError:
        return

    original = ChatEndpoint.extract_chat_response_data

    def extract_chat_response_data(self: Any, json_obj: dict[str, Any]) -> Any:
        if json_obj.get("object") is None and any(
            key in json_obj for key in ("reasoning_content", "tool_call", "tool_calls")
        ):
            return None
        return original(self, json_obj)

    extract_chat_response_data._dynamo_stream_events_patch = True  # type: ignore[attr-defined]
    ChatEndpoint.extract_chat_response_data = extract_chat_response_data


def _patch_dynamo_agent_hints() -> None:
    if not _enabled("AIPERF_PATCH_DYNAMO_AGENT_HINTS"):
        return

    try:
        from aiperf.endpoints.openai_chat import ChatEndpoint
    except ModuleNotFoundError:
        return

    original = ChatEndpoint.format_payload
    if getattr(original, "_dynamo_agent_hints_patch", False):
        return

    def format_payload(self: Any, request_info: Any) -> Any:
        payload = original(self, request_info)
        if not (
            _enabled("DYNAMO_AGENT_HINTS_DYNAMIC_OSL")
            and _enabled("USE_EXPECTED_OSL")
            and isinstance(payload, dict)
        ):
            return payload

        nvext = payload.get("nvext")
        if not isinstance(nvext, dict):
            return payload
        hints = nvext.get("agent_hints")
        if not isinstance(hints, dict):
            return payload

        max_tokens = payload.get("max_tokens", payload.get("max_completion_tokens"))
        if isinstance(max_tokens, int) and max_tokens > 0:
            hints["osl"] = max_tokens

        return payload

    format_payload._dynamo_agent_hints_patch = True  # type: ignore[attr-defined]
    ChatEndpoint.format_payload = format_payload


_patch_dynamo_stream_events()
_patch_dynamo_agent_hints()
