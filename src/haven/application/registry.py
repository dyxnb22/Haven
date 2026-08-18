"""静态工具注册表。

工具在程序中静态编译；模型永远不能注册、重命名或重新设置工具版本。验证错误会转化
为稳定的错误代码，而不是原始 traceback。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import ValidationError

from haven.contracts.tools import ARGS_MODELS, TOOL_VERSION, ToolArgs


@dataclass(frozen=True, slots=True)
class ValidationFailure:
    code: str  # 取值："unknown_tool" | "invalid_arguments"
    message: str


class ToolRegistry:
    """查找工具，并严格验证模型的原始参数。"""

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
            # 校验 JSON 文本，而不是解析后的对象：JSON 模式的严格校验允许元组
            # 字段使用 JSON 数组，而 Python 模式会拒绝它。提供商始终向我们
            # 传递 JSON 文本。
            return model.model_validate_json(payload)
        except ValidationError as exc:
            issues = "; ".join(
                f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
                for err in exc.errors()[:5]
            )
            return ValidationFailure("invalid_arguments", f"invalid arguments: {issues}")
