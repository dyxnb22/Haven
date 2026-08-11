"""Static tool registry.

Tools are compiled into the program; the model can never register, rename, or
re-version a tool. Validation errors become stable codes, not raw tracebacks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import ValidationError

from haven.contracts.tools import ARGS_MODELS, TOOL_VERSION, ToolArgs


@dataclass(frozen=True, slots=True)
class ValidationFailure:
    code: str  # "unknown_tool" | "invalid_arguments"
    message: str


class ToolRegistry:
    """Looks up tools and validates raw model arguments strictly."""

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(ARGS_MODELS)

    @property
    def version(self) -> str:
        return TOOL_VERSION

    def validate(self, tool_name: str, arguments_json: str) -> ToolArgs | ValidationFailure:
        model = ARGS_MODELS.get(tool_name)
        if model is None:
            return ValidationFailure("unknown_tool", f"tool {tool_name!r} is not registered")
        payload = arguments_json or "{}"
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as exc:
            return ValidationFailure("invalid_arguments", f"arguments are not valid JSON: {exc}")
        if not isinstance(raw, dict):
            return ValidationFailure("invalid_arguments", "arguments must be a JSON object")
        try:
            # Validate the JSON text, not the parsed object: strict mode in JSON
            # mode accepts a JSON array for a tuple field, which Python mode
            # would reject. Providers always hand us JSON text.
            return model.model_validate_json(payload)
        except ValidationError as exc:
            issues = "; ".join(
                f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
                for err in exc.errors()[:5]
            )
            return ValidationFailure("invalid_arguments", f"invalid arguments: {issues}")
