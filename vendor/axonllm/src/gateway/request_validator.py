"""Request validation for incoming chat completion requests."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import tiktoken

from src.gateway.models import ChatCompletionRequest, ValidationError

if TYPE_CHECKING:
    from src.gateway.model_registry import ModelRegistry

VALID_ROLES = {"system", "user", "assistant", "tool"}
VALID_TOOL_CHOICES = {"none", "auto", "required"}

DEFAULT_MAX_MESSAGES = 128
DEFAULT_MAX_MESSAGE_CONTENT_BYTES = 128 * 1024
DEFAULT_MAX_TOTAL_MESSAGE_CONTENT_BYTES = 512 * 1024
DEFAULT_MAX_CONTENT_PARTS = 64
DEFAULT_MAX_TOOLS = 64
DEFAULT_MAX_TOOL_SCHEMA_BYTES = 64 * 1024
DEFAULT_MAX_TOTAL_TOOL_SCHEMA_BYTES = 256 * 1024
DEFAULT_MAX_STOP_SEQUENCES = 4
DEFAULT_MAX_STOP_SEQUENCE_BYTES = 1024
DEFAULT_MAX_SYSTEM_BYTES = 64 * 1024
DEFAULT_MAX_MODEL_BYTES = 256
DEFAULT_MAX_REQUESTED_OUTPUT_TOKENS = 128 * 1024


def _error(field: str, message: str) -> ValidationError:
    return ValidationError(field=field, message=message, severity="error")


def _json_size(value: Any) -> int:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return len(encoded.encode("utf-8"))


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))


def _is_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class RequestValidator:
    """Validate chat request shape, resource bounds, model, and context size."""

    def __init__(
        self,
        model_registry: ModelRegistry | None = None,
        *,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        max_message_content_bytes: int = DEFAULT_MAX_MESSAGE_CONTENT_BYTES,
        max_total_message_content_bytes: int = DEFAULT_MAX_TOTAL_MESSAGE_CONTENT_BYTES,
        max_content_parts: int = DEFAULT_MAX_CONTENT_PARTS,
        max_tools: int = DEFAULT_MAX_TOOLS,
        max_tool_schema_bytes: int = DEFAULT_MAX_TOOL_SCHEMA_BYTES,
        max_total_tool_schema_bytes: int = DEFAULT_MAX_TOTAL_TOOL_SCHEMA_BYTES,
        max_stop_sequences: int = DEFAULT_MAX_STOP_SEQUENCES,
        max_stop_sequence_bytes: int = DEFAULT_MAX_STOP_SEQUENCE_BYTES,
        max_system_bytes: int = DEFAULT_MAX_SYSTEM_BYTES,
        max_requested_output_tokens: int = DEFAULT_MAX_REQUESTED_OUTPUT_TOKENS,
    ) -> None:
        self.model_registry = model_registry
        self.max_messages = self._positive_limit("max_messages", max_messages)
        self.max_message_content_bytes = self._positive_limit("max_message_content_bytes", max_message_content_bytes)
        self.max_total_message_content_bytes = self._positive_limit(
            "max_total_message_content_bytes", max_total_message_content_bytes
        )
        self.max_content_parts = self._positive_limit("max_content_parts", max_content_parts)
        self.max_tools = self._positive_limit("max_tools", max_tools)
        self.max_tool_schema_bytes = self._positive_limit("max_tool_schema_bytes", max_tool_schema_bytes)
        self.max_total_tool_schema_bytes = self._positive_limit(
            "max_total_tool_schema_bytes", max_total_tool_schema_bytes
        )
        self.max_stop_sequences = self._positive_limit("max_stop_sequences", max_stop_sequences)
        self.max_stop_sequence_bytes = self._positive_limit("max_stop_sequence_bytes", max_stop_sequence_bytes)
        self.max_system_bytes = self._positive_limit("max_system_bytes", max_system_bytes)
        self.max_requested_output_tokens = self._positive_limit(
            "max_requested_output_tokens", max_requested_output_tokens
        )

    @staticmethod
    def _positive_limit(name: str, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return value

    def validate_payload(
        self,
        payload: Mapping[str, Any],
        *,
        allow_empty_model: bool = False,
        check_model: bool = False,
    ) -> list[ValidationError]:
        """Validate an HTTP payload before routing or dataclass coercion."""
        if not isinstance(payload, Mapping):
            return [_error("body", "Request body must be a JSON object.")]

        request = ChatCompletionRequest(
            messages=payload.get("messages"),  # type: ignore[arg-type]
            model=payload.get("model", ""),  # type: ignore[arg-type]
            temperature=payload.get("temperature"),
            max_tokens=payload.get("max_tokens"),
            top_p=payload.get("top_p"),
            stop=payload.get("stop"),  # type: ignore[arg-type]
            stream=payload.get("stream", False),
            system=payload.get("system"),
            tools=payload.get("tools"),  # type: ignore[arg-type]
            tool_choice=payload.get("tool_choice"),
        )
        errors = self.validate(
            request,
            check_model=check_model,
            require_model=not allow_empty_model,
        )

        context = payload.get("context")
        if context is not None:
            if not isinstance(context, dict):
                errors.append(_error("context", "Field 'context' must be an object."))
            elif "smart_routing" in context and not isinstance(context["smart_routing"], bool):
                errors.append(
                    _error(
                        "context.smart_routing",
                        "Field 'context.smart_routing' must be a boolean.",
                    )
                )

        provider = payload.get("provider")
        if provider is not None and (not isinstance(provider, str) or not provider.strip()):
            errors.append(_error("provider", "Field 'provider' must be a non-empty string."))
        return errors

    def validate(
        self,
        request: ChatCompletionRequest,
        *,
        check_model: bool = True,
        require_model: bool = True,
    ) -> list[ValidationError]:
        """Validate a parsed request and return every client-correctable error."""
        errors = self.validate_shape(request, require_model=require_model)
        if errors or not check_model:
            return errors

        models = getattr(self.model_registry, "models", None)
        if not isinstance(models, Mapping):
            return errors

        if request.model not in models:
            return [_error("model", f"Model '{request.model}' not found.")]

        model_config = models.get(request.model)
        max_context = getattr(model_config, "max_context_tokens", None)
        if isinstance(max_context, int) and not isinstance(max_context, bool):
            estimated_prompt_tokens = self._estimate_prompt_tokens(request)
            requested_output_tokens = request.max_tokens or 0
            total_tokens = estimated_prompt_tokens + requested_output_tokens
            if total_tokens > max_context:
                errors.append(
                    _error(
                        "messages",
                        (
                            f"Estimated prompt tokens ({estimated_prompt_tokens}) plus "
                            f"requested output tokens ({requested_output_tokens}) exceed "
                            f"model limit ({max_context})."
                        ),
                    )
                )
        return errors

    def validate_shape(
        self,
        request: ChatCompletionRequest,
        *,
        require_model: bool = True,
    ) -> list[ValidationError]:
        """Validate types and hard resource bounds without resolving a model."""
        errors: list[ValidationError] = []
        errors.extend(self._validate_messages(request.messages))
        errors.extend(self._validate_model(request.model, require_model))
        errors.extend(self._validate_sampling(request))
        errors.extend(self._validate_stop(request.stop))
        errors.extend(self._validate_system(request.system))
        errors.extend(self._validate_tools(request.tools, request.tool_choice))

        if not isinstance(request.stream, bool):
            errors.append(_error("stream", "Field 'stream' must be a boolean."))
        return errors

    def _validate_messages(self, messages: Any) -> list[ValidationError]:
        if not isinstance(messages, list) or not messages:
            return [
                _error(
                    "messages",
                    "Field 'messages' is required and must be a non-empty list.",
                )
            ]
        if len(messages) > self.max_messages:
            return [
                _error(
                    "messages",
                    f"Field 'messages' may contain at most {self.max_messages} items.",
                )
            ]

        errors: list[ValidationError] = []
        total_content_bytes = 0
        for idx, message in enumerate(messages):
            field = f"messages[{idx}]"
            if not isinstance(message, dict):
                errors.append(_error(field, f"Message at index {idx} must be an object."))
                continue

            role = message.get("role")
            if "role" not in message:
                errors.append(
                    _error(
                        f"{field}.role",
                        f"Message at index {idx} is missing required field 'role'.",
                    )
                )
            elif not isinstance(role, str):
                errors.append(_error(f"{field}.role", f"Message role at index {idx} must be a string."))
            elif role not in VALID_ROLES:
                errors.append(
                    _error(
                        f"{field}.role",
                        (f"Invalid role '{role}' at index {idx}. Must be one of: {sorted(VALID_ROLES)}."),
                    )
                )

            tool_calls = message.get("tool_calls")
            function_call = message.get("function_call")
            has_call = bool(tool_calls) or bool(function_call)
            if "content" not in message:
                if not has_call:
                    errors.append(
                        _error(
                            f"{field}.content",
                            f"Message at index {idx} is missing required field 'content'.",
                        )
                    )
                content_bytes = 0
            else:
                content = message["content"]
                content_bytes, content_errors = self._message_content_size(content, field, has_call)
                errors.extend(content_errors)

            if tool_calls is not None:
                if not isinstance(tool_calls, list):
                    errors.append(_error(f"{field}.tool_calls", "Field 'tool_calls' must be a list."))
                elif len(tool_calls) > self.max_tools:
                    errors.append(
                        _error(
                            f"{field}.tool_calls",
                            f"Field 'tool_calls' may contain at most {self.max_tools} items.",
                        )
                    )
                elif not all(isinstance(call, dict) for call in tool_calls):
                    errors.append(
                        _error(
                            f"{field}.tool_calls",
                            "Every tool call must be an object.",
                        )
                    )
                else:
                    try:
                        content_bytes += _json_size(tool_calls)
                    except (TypeError, ValueError):
                        errors.append(
                            _error(
                                f"{field}.tool_calls",
                                "Tool calls must contain valid JSON values.",
                            )
                        )

            if function_call is not None:
                if not isinstance(function_call, dict):
                    errors.append(
                        _error(
                            f"{field}.function_call",
                            "Field 'function_call' must be an object.",
                        )
                    )
                else:
                    name = function_call.get("name")
                    if not isinstance(name, str) or not name.strip():
                        errors.append(
                            _error(
                                f"{field}.function_call.name",
                                "Function call name must be a non-empty string.",
                            )
                        )
                    try:
                        content_bytes += _json_size(function_call)
                    except (TypeError, ValueError):
                        errors.append(
                            _error(
                                f"{field}.function_call",
                                "Function calls must contain valid JSON values.",
                            )
                        )

            if content_bytes > self.max_message_content_bytes:
                errors.append(
                    _error(
                        f"{field}.content",
                        (f"Message content may not exceed {self.max_message_content_bytes} bytes."),
                    )
                )
            total_content_bytes += content_bytes

            for key in ("name", "tool_call_id"):
                value = message.get(key)
                if value is not None and (not isinstance(value, str) or not value.strip()):
                    errors.append(
                        _error(
                            f"{field}.{key}",
                            f"Field '{key}' must be a non-empty string.",
                        )
                    )

        if total_content_bytes > self.max_total_message_content_bytes:
            errors.append(
                _error(
                    "messages",
                    (f"Total message content may not exceed {self.max_total_message_content_bytes} bytes."),
                )
            )
        return errors

    def _message_content_size(
        self,
        content: Any,
        field: str,
        has_call: bool,
    ) -> tuple[int, list[ValidationError]]:
        if content is None:
            if has_call:
                return 0, []
            return 0, [
                _error(
                    f"{field}.content",
                    "Message content may be null only when the message has a tool call.",
                )
            ]
        if isinstance(content, str):
            return _utf8_size(content), []
        if not isinstance(content, list):
            return 0, [
                _error(
                    f"{field}.content",
                    "Message content must be a string or a list of content parts.",
                )
            ]
        if len(content) > self.max_content_parts:
            return 0, [
                _error(
                    f"{field}.content",
                    f"Message content may contain at most {self.max_content_parts} parts.",
                )
            ]
        if not all(isinstance(part, dict) for part in content):
            return 0, [_error(f"{field}.content", "Every message content part must be an object.")]
        try:
            return _json_size(content), []
        except (TypeError, ValueError):
            return 0, [_error(f"{field}.content", "Message content must contain valid JSON values.")]

    def _validate_model(self, model: Any, require_model: bool) -> list[ValidationError]:
        if not isinstance(model, str):
            return [_error("model", "Field 'model' must be a string.")]
        if require_model and not model.strip():
            return [_error("model", "Field 'model' is required.")]
        if _utf8_size(model) > DEFAULT_MAX_MODEL_BYTES:
            return [
                _error(
                    "model",
                    f"Field 'model' may not exceed {DEFAULT_MAX_MODEL_BYTES} bytes.",
                )
            ]
        return []

    def _validate_sampling(self, request: ChatCompletionRequest) -> list[ValidationError]:
        errors: list[ValidationError] = []
        if request.temperature is not None:
            if not _is_number(request.temperature):
                errors.append(_error("temperature", "Field 'temperature' must be a finite number."))
            elif not 0 <= request.temperature <= 2:
                errors.append(_error("temperature", "Field 'temperature' must be between 0 and 2."))

        if request.top_p is not None:
            if not _is_number(request.top_p):
                errors.append(_error("top_p", "Field 'top_p' must be a finite number."))
            elif not 0 <= request.top_p <= 1:
                errors.append(_error("top_p", "Field 'top_p' must be between 0 and 1."))

        if request.max_tokens is not None and (
            not isinstance(request.max_tokens, int) or isinstance(request.max_tokens, bool) or request.max_tokens <= 0
        ):
            errors.append(_error("max_tokens", "Field 'max_tokens' must be a positive integer."))
        elif request.max_tokens is not None and request.max_tokens > self.max_requested_output_tokens:
            errors.append(
                _error(
                    "max_tokens",
                    (f"Field 'max_tokens' may not exceed {self.max_requested_output_tokens}."),
                )
            )
        return errors

    def _validate_stop(self, stop: Any) -> list[ValidationError]:
        if stop is None:
            return []
        sequences = [stop] if isinstance(stop, str) else stop
        if not isinstance(sequences, list):
            return [_error("stop", "Field 'stop' must be a string or a list of strings.")]
        if len(sequences) > self.max_stop_sequences:
            return [
                _error(
                    "stop",
                    (f"Field 'stop' may contain at most {self.max_stop_sequences} sequences."),
                )
            ]

        errors: list[ValidationError] = []
        for idx, sequence in enumerate(sequences):
            if not isinstance(sequence, str) or not sequence:
                errors.append(_error(f"stop[{idx}]", "Stop sequences must be non-empty strings."))
            elif _utf8_size(sequence) > self.max_stop_sequence_bytes:
                errors.append(
                    _error(
                        f"stop[{idx}]",
                        (f"Stop sequences may not exceed {self.max_stop_sequence_bytes} bytes."),
                    )
                )
        return errors

    def _validate_system(self, system: Any) -> list[ValidationError]:
        if system is None:
            return []
        if not isinstance(system, str):
            return [_error("system", "Field 'system' must be a string.")]
        if _utf8_size(system) > self.max_system_bytes:
            return [
                _error(
                    "system",
                    f"Field 'system' may not exceed {self.max_system_bytes} bytes.",
                )
            ]
        return []

    def _validate_tools(
        self,
        tools: Any,
        tool_choice: Any,
    ) -> list[ValidationError]:
        errors: list[ValidationError] = []
        if tools is None:
            if tool_choice is not None:
                errors.append(_error("tool_choice", "Field 'tool_choice' requires at least one tool."))
            return errors
        if not isinstance(tools, list):
            return [_error("tools", "Field 'tools' must be a list.")]
        if len(tools) > self.max_tools:
            return [
                _error(
                    "tools",
                    f"Field 'tools' may contain at most {self.max_tools} items.",
                )
            ]

        total_schema_bytes = 0
        for idx, tool in enumerate(tools):
            field = f"tools[{idx}]"
            if not isinstance(tool, dict):
                errors.append(_error(field, "Every tool must be an object."))
                continue
            try:
                schema_bytes = _json_size(tool)
            except (TypeError, ValueError):
                errors.append(_error(field, "Tool definitions must contain valid JSON values."))
                continue
            total_schema_bytes += schema_bytes
            if schema_bytes > self.max_tool_schema_bytes:
                errors.append(
                    _error(
                        field,
                        (f"A tool definition may not exceed {self.max_tool_schema_bytes} bytes."),
                    )
                )

            if tool.get("type") != "function":
                errors.append(_error(f"{field}.type", "Tool type must be 'function'."))
            function = tool.get("function")
            if not isinstance(function, dict):
                errors.append(_error(f"{field}.function", "Tool function must be an object."))
                continue
            name = function.get("name")
            if not isinstance(name, str) or not name.strip():
                errors.append(
                    _error(
                        f"{field}.function.name",
                        "Tool function name must be a non-empty string.",
                    )
                )
            elif _utf8_size(name) > 128:
                errors.append(
                    _error(
                        f"{field}.function.name",
                        "Tool function name may not exceed 128 bytes.",
                    )
                )
            description = function.get("description")
            if description is not None and not isinstance(description, str):
                errors.append(
                    _error(
                        f"{field}.function.description",
                        "Tool function description must be a string.",
                    )
                )
            parameters = function.get("parameters")
            if parameters is not None and not isinstance(parameters, dict):
                errors.append(
                    _error(
                        f"{field}.function.parameters",
                        "Tool function parameters must be an object.",
                    )
                )
            strict = function.get("strict")
            if strict is not None and not isinstance(strict, bool):
                errors.append(
                    _error(
                        f"{field}.function.strict",
                        "Tool function strict must be a boolean.",
                    )
                )

        if total_schema_bytes > self.max_total_tool_schema_bytes:
            errors.append(
                _error(
                    "tools",
                    (f"Total tool definitions may not exceed {self.max_total_tool_schema_bytes} bytes."),
                )
            )
        errors.extend(self._validate_tool_choice(tool_choice, bool(tools)))
        return errors

    @staticmethod
    def _validate_tool_choice(
        tool_choice: Any,
        has_tools: bool,
    ) -> list[ValidationError]:
        if tool_choice is None:
            return []
        if not has_tools:
            return [_error("tool_choice", "Field 'tool_choice' requires at least one tool.")]
        if isinstance(tool_choice, str):
            if tool_choice not in VALID_TOOL_CHOICES:
                return [
                    _error(
                        "tool_choice",
                        (f"String tool_choice must be one of: {sorted(VALID_TOOL_CHOICES)}."),
                    )
                ]
            return []
        if not isinstance(tool_choice, dict):
            return [
                _error(
                    "tool_choice",
                    "Field 'tool_choice' must be a string or an object.",
                )
            ]
        function = tool_choice.get("function")
        if (
            tool_choice.get("type") != "function"
            or not isinstance(function, dict)
            or not isinstance(function.get("name"), str)
            or not function["name"].strip()
        ):
            return [
                _error(
                    "tool_choice",
                    "Object tool_choice must name a function tool.",
                )
            ]
        return []

    @staticmethod
    def _estimate_prompt_tokens(request: ChatCompletionRequest) -> int:
        prompt_parts: dict[str, Any] = {"messages": request.messages}
        if request.system is not None:
            prompt_parts["system"] = request.system
        if request.tools is not None:
            prompt_parts["tools"] = request.tools
        prompt_text = json.dumps(
            prompt_parts,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        try:
            encoding = tiktoken.get_encoding("cl100k_base")
            return len(encoding.encode(prompt_text))
        except Exception:
            return max(1, (_utf8_size(prompt_text) + 3) // 4)
