"""规范化哈希的契约：跨进程、跨版本可复现。

`digest.py` 此前的覆盖率是个陷阱：它的输出只在"同一边算两次"之间比较，因此把
`canonical_json` 的排序、分隔符、转义全都改坏，1563 条测试依然全绿（变异测试实测）。
但这些函数存在的理由恰恰是**可复现**——审批、票据和检查点摘要都要在另一个进程、
另一个版本里算出一模一样的值。这里直接把那条契约钉住。
"""

import json

from haven.domain.digest import canonical_json, digest_of, sha256_bytes, sha256_text


class TestCanonicalJson:
    def test_key_order_does_not_change_the_result(self) -> None:
        """语义相同的字典必须序列化成同一个字符串。

        这是 `sort_keys=True` 存在的全部理由：没有它，同一份数据在两条构造路径下
        会得到不同摘要，而摘要本该只取决于内容。
        """
        assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
        assert canonical_json({"outer": {"y": 1, "x": 2}}) == canonical_json(
            {"outer": {"x": 2, "y": 1}}
        )

    def test_key_order_does_not_change_the_digest(self) -> None:
        assert digest_of({"b": 1, "a": 2}) == digest_of({"a": 2, "b": 1})

    def test_the_output_is_compact(self) -> None:
        """键之间和冒号后不留空格：摘要要短，且不因格式化选择而变。"""
        assert canonical_json({"a": 1, "b": [1, 2]}) == '{"a":1,"b":[1,2]}'

    def test_non_ascii_survives_as_text_not_escapes(self) -> None:
        """`ensure_ascii=False`：摘要取决于文本本身，而不是某种转义风格。"""
        assert canonical_json({"k": "中文"}) == '{"k":"中文"}'

    def test_the_result_is_stable_across_calls(self) -> None:
        value = {"z": [1, {"nested": True}], "a": None}
        assert canonical_json(value) == canonical_json(value)

    def test_it_matches_hand_written_json(self) -> None:
        """与 `json.dumps` 的约定一致，只是键排序且紧凑。"""
        value = {"b": 2, "a": 1}
        assert json.loads(canonical_json(value)) == value


class TestDigestHelpers:
    def test_sha256_text_is_64_hex_chars(self) -> None:
        digest = sha256_text("haven")
        assert len(digest) == 64
        int(digest, 16)

    def test_sha256_bytes_matches_the_text_form_for_utf8(self) -> None:
        assert sha256_bytes(b"haven") == sha256_text("haven")

    def test_digest_of_is_the_hash_of_the_canonical_form(self) -> None:
        value = {"a": 1}
        assert digest_of(value) == sha256_text(canonical_json(value))

    def test_different_values_give_different_digests(self) -> None:
        assert digest_of({"a": 1}) != digest_of({"a": 2})
        assert sha256_text("a") != sha256_text("b")

    def test_a_known_vector(self) -> None:
        """钉住算法本身：换成 sha1 或改一种编码，这个向量就会变。

        数值由独立工具（`hashlib.sha256(b\"haven\").hexdigest()`）算出，不是从
        实现里抄的——否则它只会证明"实现等于它自己"。
        """
        assert (
            sha256_text("haven")
            == "04a69d8da302ef4efbe84fa115e688a02e70a772500ba0e22e673467da47da11"
        )
        assert (
            sha256_bytes(b"haven")
            == "04a69d8da302ef4efbe84fa115e688a02e70a772500ba0e22e673467da47da11"
        )
