"""简历条目里的数字必须由测量生成，否则它会静静腐烂。

这份文档的存在意义就是被粘进简历，所以它的数字错得最贵。而它曾经确实错了：
写着 885 项测试、89% 覆盖率、102 个模块、39/39 评测，实际早已是 1456 / 91% /
152 / 40/40——落后了 571 个测试，且没有任何门禁在看它，因为它不在四个带生成
表格的文件里。

这些测试只锁“改写机制”：用合成值驱动，不碰覆盖率数据。“文档里的数字此刻是否
与测量一致”由 `metrics` 门禁负责——它运行在 `tests` 与 `eval` 之后，拿到的正是
新鲜覆盖率；而单元测试自己就跑在覆盖率运行中间，那时数据必然还是旧的。
"""

import re

from scripts import refresh_metrics as metrics

#: 合成测量值：形状与 `measure()` 相同，但不依赖任何真实数据文件。
_SYNTHETIC = {
    "tests": 1234,
    "coverage": 77,
    "modules": 42,
    "eval": "9/9",
    "violations": 0,
}


def _resume_text() -> str:
    # 与 refresh_metrics 自身一致：相对路径 + 调用时解析，测试替换 ROOT 时才不会失效。
    return (metrics.ROOT / metrics._RESUME_REL).read_text(encoding="utf-8")


class TestResumeClaimRewrite:
    def test_a_stale_number_is_rewritten_to_the_measurement(self) -> None:
        """过期数字必须被改写，而不是留着。

        按真实腐烂方式构造：句子结构不变，只有一个数字漂移——这正是当初
        885 项测试那种情形。结构不变的漂移才能被正则重新找到并改写。
        """
        text = _resume_text()
        rotted = re.sub(
            r"\*\*(\d+) 项测试", lambda m: f"**{int(m.group(1)) + 1} 项测试", text, count=1
        )
        assert rotted != text, "构造漂移失败，测试本身失去了意义"
        assert metrics._RESUME_CLAIM.search(rotted) is not None, "漂移后应当仍可匹配"
        restored, _matched = metrics._replace_resume_claim(
            rotted, metrics.render_resume_claim(_SYNTHETIC)
        )
        assert restored != rotted, "过期数字没有被改写"
        # 同一输入重复改写必须幂等，否则每次 refresh 都会产生新 diff。
        again, _ = metrics._replace_resume_claim(restored, metrics.render_resume_claim(_SYNTHETIC))
        assert again == restored

    def test_a_missing_claim_is_left_alone_so_check_can_flag_it(self) -> None:
        """整句消失时不能伪造一个出来——check 必须能报出它缺失。"""
        text = _resume_text()
        without = metrics._RESUME_CLAIM.sub(lambda _m: "（此句已丢失）", text, count=1)
        assert without != text
        replaced, matched = metrics._replace_resume_claim(without, "任何生成值")
        assert replaced == without
        assert matched is False

    def test_check_reports_a_missing_claim_instead_of_passing(self) -> None:
        """契约的另一半：`--check` 必须把"那句话不见了"判成过期。

        此前只测了 helper 原样返回，于是 `updated != current` 不成立、门禁静默自失效——
        那句是整份文档里最不该出错的一句，它一旦因改写措辞而匹配不上，此后每一次真实的
        数字回归都会被放行，且没有任何信号。
        """
        text = _resume_text()
        without = metrics._RESUME_CLAIM.sub(lambda _m: "（此句已丢失）", text, count=1)
        _, matched = metrics._replace_resume_claim(without, "任何生成值")
        assert matched is False, "替换没发生必须显式告知调用方，否则 check 会以为没问题"

    def test_check_reports_a_stale_number(self) -> None:
        """数字对不上时 `matched` 仍为 True，过期由"文本变了"这一路报出。"""
        text = _resume_text()
        updated, matched = metrics._replace_resume_claim(text, "最终验证 **9999 项测试…")
        assert matched is True
        assert updated != text

    def test_rendering_puts_every_measured_number_in(self) -> None:
        """渲染结果必须包含全部五个数字，漏一个就等于简历在说谎。"""
        claim = metrics.render_resume_claim(_SYNTHETIC)
        for fragment in ("1234", "77%", "42", "9/9", "0"):
            assert fragment in claim, f"渲染结果缺少 {fragment}"
        assert metrics._RESUME_CLAIM.search(claim) is not None, "渲染结果无法被反向匹配"

    def test_the_document_actually_carries_the_claim(self) -> None:
        """文档里必须真有这一句；没有它，上面全部测试都在测空文本。"""
        assert metrics._RESUME_CLAIM.search(_resume_text()) is not None
