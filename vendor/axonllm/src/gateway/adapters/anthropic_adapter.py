"""Anthropic provider adapter for the LLM-Router."""

import json
import logging
from collections.abc import Callable

from src.gateway.adapters.anthropic_style import AnthropicStyleAdapter
from src.gateway.models import ModelInfo, StreamChunk, TokenUsage

logger = logging.getLogger(__name__)

PROVIDER_NAME = "anthropic"
_STOP_REASONS = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
}

_ANTHROPIC_MODELS = [
    ModelInfo(model_id="claude-3-opus-20240229", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="claude-3-sonnet-20240229", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
    ModelInfo(model_id="claude-3-haiku-20240307", provider=PROVIDER_NAME, capabilities=["chat", "streaming"]),
]


class AnthropicAdapter(AnthropicStyleAdapter):
    """Translates between the unified Gateway format and Anthropic's native API format."""

    PROVIDER_NAME = PROVIDER_NAME
    _MODELS = _ANTHROPIC_MODELS

    def stream_translator(self) -> Callable[[dict], StreamChunk]:
        """Map Anthropic content-block positions to contiguous tool indices."""
        tool_indexes: dict[int, int] = {}

        def translate(chunk: dict) -> StreamChunk:
            chunk_type = chunk.get("type")
            block_index = chunk.get("index")
            content_block = chunk.get("content_block", {}) or {}
            delta = chunk.get("delta", {}) or {}
            if (
                chunk_type == "content_block_start"
                and content_block.get("type") == "tool_use"
                and isinstance(block_index, int)
            ):
                tool_indexes.setdefault(block_index, len(tool_indexes))
            if (
                chunk_type in {"content_block_start", "content_block_delta"}
                and isinstance(block_index, int)
                and block_index in tool_indexes
                and (
                    content_block.get("type") == "tool_use"
                    or delta.get("type") == "input_json_delta"
                )
            ):
                chunk = {**chunk, "index": tool_indexes[block_index]}
            return self.translate_stream_chunk(chunk)

        return translate

    def translate_stream_chunk(self, chunk: dict) -> StreamChunk:
        """Anthropic streaming uses message_start with nested message.id/model.

        Usage arrives in two events: ``message_start`` carries input_tokens (and
        cache tokens) in message.usage; ``message_delta`` carries the cumulative
        output_tokens. We attach whatever usage the event carries; the agent's
        end-of-stream accumulator merges input+output across the stream.
        """
        chunk_type = chunk.get("type", "")
        choices: list[dict] = []
        is_final = False
        usage = None

        if chunk_type == "content_block_start":
            index = chunk.get("index", 0)
            content_block = chunk.get("content_block", {}) or {}
            if content_block.get("type") == "tool_use":
                choices = [{
                    "index": 0,
                    "delta": {
                        "tool_calls": [{
                            "index": index,
                            "id": content_block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": content_block.get("name", ""),
                                "arguments": json.dumps(
                                    content_block.get("input", {})
                                )
                                if content_block.get("input")
                                else "",
                            },
                        }]
                    },
                    "finish_reason": None,
                }]
        elif chunk_type == "content_block_delta":
            delta = chunk.get("delta", {})
            if delta.get("type") == "input_json_delta":
                choices = [{
                    "index": 0,
                    "delta": {
                        "tool_calls": [{
                            "index": chunk.get("index", 0),
                            "function": {
                                "arguments": delta.get("partial_json", ""),
                            },
                        }]
                    },
                    "finish_reason": None,
                }]
            else:
                delta_content = delta.get("text", "")
                if delta_content:
                    choices = [{
                        "index": 0,
                        "delta": {"content": delta_content},
                        "finish_reason": None,
                    }]
        elif chunk_type == "message_start":
            u = chunk.get("message", {}).get("usage", {}) or {}
            if u:
                usage = TokenUsage(
                    prompt_tokens=u.get("input_tokens", 0),
                    completion_tokens=u.get("output_tokens", 0),
                    total_tokens=u.get("input_tokens", 0) + u.get("output_tokens", 0),
                    cached_tokens=u.get("cache_read_input_tokens", 0),
                    cache_creation_tokens=u.get("cache_creation_input_tokens", 0),
                )
        elif chunk_type == "message_delta":
            u = chunk.get("usage", {}) or {}
            if u:
                usage = TokenUsage(
                    prompt_tokens=u.get("input_tokens", 0),
                    completion_tokens=u.get("output_tokens", 0),
                    total_tokens=u.get("input_tokens", 0) + u.get("output_tokens", 0),
                )
            stop_reason = (chunk.get("delta", {}) or {}).get("stop_reason")
            if stop_reason is not None:
                choices = [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": _STOP_REASONS.get(
                        stop_reason,
                        stop_reason,
                    ),
                }]
                is_final = True
        elif chunk_type == "message_stop":
            is_final = True

        return StreamChunk(
            id=(
                chunk.get("message", {}).get("id", "")
                if chunk_type == "message_start"
                else chunk.get("id", "")
            ),
            choices=choices,
            model=(
                chunk.get("message", {}).get("model", "")
                if chunk_type == "message_start"
                else chunk.get("model", "")
            ),
            is_final=is_final,
            usage=usage,
        )
