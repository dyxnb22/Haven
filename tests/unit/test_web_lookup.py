"""ADR 0038 沙箱隔离的技术文档查询与抓取单元测试。"""

import asyncio
import time
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

from haven.application import web_lookup
from haven.application.web_lookup import (
    _fetch_web_page,
    _SafeRedirectHandler,
    _search_web_docs,
    fetch_web_page,
    html_to_markdown,
    is_safe_url,
    search_web_docs,
)
from haven.domain.enums import PermissionMode, PolicyDecision
from haven.domain.policy import ToolFacts, evaluate_policy


class TestSSRFDefense:
    def test_safe_public_urls(self) -> None:
        safe, _ = is_safe_url("https://docs.python.org/3/library/ast.html")
        assert safe

        safe, _ = is_safe_url("http://example.com/api/v1")
        assert safe

    def test_unsupported_schemes_blocked(self) -> None:
        safe, reason = is_safe_url("ftp://example.com/file")
        assert not safe
        assert "unsupported scheme" in reason

        safe, reason = is_safe_url("file:///etc/passwd")
        assert not safe

    def test_localhost_and_internal_names_blocked(self) -> None:
        safe, reason = is_safe_url("http://localhost:8080/secret")
        assert not safe
        assert "localhost" in reason

    def test_private_and_loopback_ips_blocked(self) -> None:
        for ip_host in ("127.0.0.1", "10.0.0.1", "192.168.1.50", "172.20.0.1", "169.254.169.254"):
            safe, reason = is_safe_url(f"http://{ip_host}/api")
            assert not safe, f"{ip_host} should be blocked"
            assert "private/internal network IP" in reason


class TestHTMLToMarkdown:
    def test_strips_scripts_and_styles(self) -> None:
        html = """
        <html>
        <head><title>Test</title><style>body { color: red; }</style></head>
        <body>
            <script>alert('xss');</script>
            <h1>API Reference</h1>
            <p>Welcome to <code>Haven</code> documentation.</p>
        </body>
        </html>
        """
        md = html_to_markdown(html)
        assert "alert" not in md
        assert "color: red" not in md
        assert "# API Reference" in md
        assert "`Haven`" in md

    def test_converts_links_and_pre(self) -> None:
        html = """
        <p>See <a href="https://example.com/doc">the docs</a> for details.</p>
        <pre>def test_func():
    return True</pre>
        """
        md = html_to_markdown(html)
        assert "[the docs](https://example.com/doc)" in md
        assert "```\ndef test_func():\n    return True\n```" in md


class TestFetchAndSearch:
    async def test_fetch_blocks_unsafe_url(self) -> None:
        res = await fetch_web_page("http://127.0.0.1:8000/secret")
        assert res["status"] == "error"
        assert "SSRF" in res["error"]

    async def test_fetch_converts_html_content(self) -> None:
        mock_resp = MagicMock()
        mock_resp.headers.get_content_type.return_value = "text/html"
        mock_resp.headers.get_content_charset.return_value = "utf-8"
        mock_resp.read.return_value = b"<h1>Docs Title</h1><p>Doc content body.</p>"
        mock_resp.__enter__.return_value = mock_resp

        with patch.object(urllib.request.OpenerDirector, "open", return_value=mock_resp):
            res = await fetch_web_page("https://example.com/docs")
        assert res["status"] == "ok"
        assert "# Docs Title" in res["content"]
        assert "Doc content body." in res["content"]

    async def test_search_docs_normalizes_wrapped_urls(self) -> None:
        ddg_link = (
            'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Fast.html&amp;rut=1"'
        )
        mock_html = (
            f"<html><body>"
            f'<a class="result__title">Python AST</a>'
            f'<a class="result__url" {ddg_link}>docs.python.org</a>'
            f'<a class="result__snippet">Official AST documentation</a>'
            f"</body></html>"
        ).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_html
        mock_resp.__enter__.return_value = mock_resp

        with patch.object(urllib.request.OpenerDirector, "open", return_value=mock_resp):
            res = await search_web_docs("python ast")
        assert res["status"] == "ok"
        assert len(res["results"]) == 1
        assert res["results"][0]["url"] == "https://docs.python.org/3/ast.html"
        assert res["results"][0]["title"] == "Python AST"

    async def test_search_docs_fallback_on_network_failure(self) -> None:
        with patch.object(
            urllib.request.OpenerDirector, "open", side_effect=Exception("connection refused")
        ):
            res = await search_web_docs("python ast parse")
        assert res["status"] == "ok"
        assert "unavailable" in res["note"]

    def test_redirect_to_private_ip_is_blocked(self) -> None:
        handler = _SafeRedirectHandler()
        req = urllib.request.Request("https://example.com")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            handler.redirect_request(
                req, None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data/"
            )
        assert exc_info.value.code == 302
        assert "SSRF redirect blocked" in str(exc_info.value)


class TestEventLoopIsNeverBlocked:
    """DNS 与 HTTP 都是阻塞调用，因此两个入口必须卸载到工作线程。"""

    async def _measure_progress_during(self, coro: object) -> int:
        """在 `coro` 在途期间，同一个循环上的另一个协程推进了多少步。"""
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            for _ in range(6):
                await asyncio.sleep(0.02)
                ticks += 1

        task = asyncio.create_task(coro)  # type: ignore[arg-type]
        ticker_task = asyncio.create_task(ticker())
        await task
        ticks_during = ticks
        await ticker_task
        return ticks_during

    async def test_fetch_runs_off_the_event_loop(self) -> None:
        """抓取在途时，同一个循环上的协程必须继续前进。

        替身真的睡 0.3 秒：直接调用会把循环冻结那么久，ticker 一步都走不了；
        卸载生效时它应该已经跑完自己的 6 步。
        """

        def blocking_stand_in(*args: object, **kwargs: object) -> dict[str, object]:
            time.sleep(0.3)
            return {"status": "ok", "url": "u", "content": "x", "truncated": False}

        with patch.object(web_lookup, "_fetch_web_page", blocking_stand_in):
            ticks = await self._measure_progress_during(fetch_web_page("https://example.com"))
        assert ticks >= 4

    async def test_search_runs_off_the_event_loop(self) -> None:
        """检索同理：一次失败的 DNS 不该把 TUI 一起按住。"""

        def blocking_stand_in(*args: object, **kwargs: object) -> dict[str, object]:
            time.sleep(0.3)
            return {"status": "ok", "query": "q", "results": []}

        with patch.object(web_lookup, "_search_web_docs", blocking_stand_in):
            ticks = await self._measure_progress_during(search_web_docs("python ast"))
        assert ticks >= 4

    def test_the_sync_helpers_still_work_without_a_loop(self) -> None:
        """卸载不改变同步实现的契约：`_fetch_web_page` 仍可直接单测。"""
        res = _fetch_web_page("http://127.0.0.1:8000/secret", 100, 1.0)
        assert res["status"] == "error"
        assert "SSRF" in res["error"]
        assert _search_web_docs("", 5)["status"] == "error"


def test_web_policy_evaluation() -> None:
    for tool in ("web.search", "web.fetch"):
        facts = ToolFacts(tool_name=tool)
        assert evaluate_policy(PermissionMode.INTERACTIVE, facts).decision == PolicyDecision.ALLOW
        assert evaluate_policy(PermissionMode.READ_ONLY, facts).decision == PolicyDecision.ALLOW
