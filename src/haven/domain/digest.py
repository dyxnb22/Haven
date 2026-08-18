"""供审批、票据和日志使用的规范化哈希辅助函数。"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> str:
    """用于绑定摘要的确定性 JSON（键排序且不含空格）。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_of(value: Any) -> str:
    return sha256_text(canonical_json(value))
