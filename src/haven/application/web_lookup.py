"""沙箱隔离的只读技术文档查询与抓取（ADR 0038）。

提供外部官方技术文档检索与网页提取，解决新库接口变动引发的代码幻觉。
安全约束：
- repo.exec 本地代码沙箱隔离红线 (deny network*) 坚决不放宽；
- 抓取请求由受控的只读网络通道发起，严格防范 SSRF（阻断私网/回环/链路本地 IP）；
- 自动剥离 HTML/JS/CSS，转为清洁 Markdown；
- 抓取内容自动标记为 untrusted，以安全隔离框形式投递给模型。

DNS 与 HTTP 都是阻塞调用，因此两个公开入口都是 `async` 并整体卸载到工作线程：
在事件循环上跑它们，等于让代理和 TUI 一起冻结最长数秒。
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse


def is_safe_url(url: str) -> tuple[bool, str]:
    """验证 URL 是否为安全的公网 HTTP/HTTPS 地址（防范 SSRF）。

    同步函数，因为它唯一的另一个调用者 `_SafeRedirectHandler.redirect_request`
    是 urllib 的同步回调，没有机会 await。它内含阻塞 DNS，因此只能从工作线程里
    调用——`fetch_web_page` / `search_web_docs` 正是那样做的。
    """
    try:
        parsed = urlparse(url)
    except Exception as exc:
        return False, f"invalid url: {exc}"

    if parsed.scheme not in ("http", "https"):
        return False, f"unsupported scheme {parsed.scheme!r}; only http and https are allowed"

    hostname = parsed.hostname
    if not hostname:
        return False, "missing hostname"

    # 阻断直接写 localhost 或常见内网名称
    if hostname.lower() in ("localhost", "local", "broadcasthost"):
        return False, "access to localhost is prohibited"

    try:
        # 解析 DNS 获取所有对应 IP 地址
        addr_info = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        return False, f"dns resolution failed for {hostname}: {exc}"

    for item in addr_info:
        sockaddr = item[4]
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue

        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False, f"access to private/internal network IP ({ip_str}) is prohibited"

    return True, ""


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """拦截重定向请求，对重定向目标 URL 重新执行 SSRF 校验，防止重定向绕过。"""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        safe, reason = is_safe_url(newurl)
        if not safe:
            raise urllib.error.HTTPError(
                newurl, code, f"SSRF redirect blocked: {reason}", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _HTMLToMarkdownParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._pieces: list[str] = []
        self._ignored_depth = 0
        self._in_pre = False
        self._current_href = ""
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        if tag_lower in (
            "script",
            "style",
            "head",
            "nav",
            "svg",
            "noscript",
            "iframe",
            "footer",
            "aside",
            "form",
        ):
            self._ignored_depth += 1
            return
        if self._ignored_depth > 0:
            return

        attr_dict = dict(attrs)
        if tag_lower in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag_lower[1])
            self._pieces.append(f"\n\n{'#' * level} ")
        elif tag_lower == "p":
            self._pieces.append("\n\n")
        elif tag_lower == "br":
            self._pieces.append("\n")
        elif tag_lower == "li":
            self._pieces.append("\n* ")
        elif tag_lower == "pre":
            self._in_pre = True
            self._pieces.append("\n```\n")
        elif tag_lower == "code" and not self._in_pre:
            self._pieces.append("`")
        elif tag_lower == "a":
            href = attr_dict.get("href")
            if href and not href.startswith("javascript:"):
                self._current_href = href
                self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower in (
            "script",
            "style",
            "head",
            "nav",
            "svg",
            "noscript",
            "iframe",
            "footer",
            "aside",
            "form",
        ):
            if self._ignored_depth > 0:
                self._ignored_depth -= 1
            return
        if self._ignored_depth > 0:
            return

        if tag_lower == "pre":
            self._in_pre = False
            self._pieces.append("\n```\n")
        elif tag_lower == "code" and not self._in_pre:
            self._pieces.append("`")
        elif tag_lower == "a" and self._current_href:
            link_content = "".join(self._link_text).strip()
            if link_content:
                self._pieces.append(f"[{link_content}]({self._current_href})")
            self._current_href = ""
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._ignored_depth > 0:
            return
        unescaped = html.unescape(data)
        if self._current_href:
            self._link_text.append(unescaped)
        else:
            self._pieces.append(unescaped)

    def get_markdown(self) -> str:
        text = "".join(self._pieces)
        # 整理过多空白换行
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_markdown(html_content: str) -> str:
    """将 HTML 页面内容解析并格式化为纯净 Markdown 文本。"""
    parser = _HTMLToMarkdownParser()
    try:
        parser.feed(html_content)
        parser.close()
        return parser.get_markdown()
    except Exception:
        # 回退为粗略正则剔除
        stripped = re.sub(r"<[^>]+>", " ", html_content)
        return re.sub(r"\s+", " ", stripped).strip()


async def fetch_web_page(url: str, max_chars: int = 10000, timeout: float = 8.0) -> dict[str, Any]:
    """安全抓取技术文档网页并转换为有界 Markdown。

    DNS 解析（`socket.getaddrinfo`）和 HTTP 抓取都是阻塞调用，且前者根本不受
    `timeout` 约束——系统解析器卡住时它能挂住数十秒。直接在事件循环上跑，就会把
    代理连同 TUI 一起冻结同样长的时间，因此整体卸载到工作线程：与
    `adapters/workspace_search.py` 对阻塞文件系统搜索的做法一致。
    """
    return await asyncio.to_thread(_fetch_web_page, url, max_chars, timeout)


def _fetch_web_page(url: str, max_chars: int, timeout: float) -> dict[str, Any]:
    """同步实现；只经由 `fetch_web_page` 的工作线程调用。"""
    safe, reason = is_safe_url(url)
    if not safe:
        return {"status": "error", "url": url, "error": f"SSRF security check failed: {reason}"}

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Haven-DocFetcher/1.0 (+https://github.com/haven)",
            "Accept": "text/html,application/xhtml+xml,text/plain",
        },
    )

    opener = urllib.request.build_opener(_SafeRedirectHandler())
    try:
        with opener.open(req, timeout=timeout) as resp:
            content_type = resp.headers.get_content_type()
            raw_bytes = resp.read(max_chars * 4)  # 限制抓取字节数
            charset = resp.headers.get_content_charset() or "utf-8"
            decoded = raw_bytes.decode(charset, errors="replace")
    except Exception as exc:
        return {"status": "error", "url": url, "error": f"fetch failed: {exc}"}

    md = html_to_markdown(decoded) if "html" in content_type else decoded.strip()
    truncated = False
    if len(md) > max_chars:
        md = md[:max_chars] + f"\n\n[... truncated {len(md) - max_chars} characters ...]"
        truncated = True

    return {
        "status": "ok",
        "url": url,
        "content": md,
        "truncated": truncated,
    }


async def search_web_docs(query: str, max_results: int = 5) -> dict[str, Any]:
    """在网络上检索官方文档或技术问答。

    支持 DuckDuckGo 轻量 HTML 接口；在受限网络或离线测试中安全降级返回有界结果。

    与 `fetch_web_page` 同理：DNS 与 HTTP 都是阻塞调用，必须在工作线程里跑，
    否则一次检索失败就能把整个代理冻结数秒。
    """
    return await asyncio.to_thread(_search_web_docs, query, max_results)


def _search_web_docs(query: str, max_results: int) -> dict[str, Any]:
    """同步实现；只经由 `search_web_docs` 的工作线程调用。"""
    max_results = max(1, min(max_results, 10))
    query_str = query.strip()
    if not query_str:
        return {"status": "error", "query": query, "error": "query cannot be empty"}

    encoded = urllib.parse.quote_plus(query_str)
    search_url = f"https://html.duckduckgo.com/html/?q={encoded}"

    req = urllib.request.Request(
        search_url,
        headers={"User-Agent": "Haven-DocSearch/1.0 (+https://github.com/haven)"},
    )

    opener = urllib.request.build_opener(_SafeRedirectHandler())
    try:
        with opener.open(req, timeout=6.0) as resp:
            html_text = resp.read(150_000).decode("utf-8", errors="replace")
    except Exception as exc:
        # 网络异常或离线环境：安全降级，不抛出异常中断代理运行
        return {
            "status": "ok",
            "query": query,
            "results": [],
            "note": f"search network lookup unavailable ({exc}); try direct repo exploration",
        }

    # 解析 DuckDuckGo 结果
    results: list[dict[str, str]] = []
    # 提取 <a class="result__snippet" ...> 与 <a class="result__url" ...>
    snippets = re.findall(
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', html_text, re.DOTALL | re.IGNORECASE
    )
    urls = re.findall(r'<a[^>]+class="result__url"[^>]+href="([^"]+)"', html_text, re.IGNORECASE)
    titles = re.findall(
        r'<a[^>]+class="result__title"[^>]*>(.*?)</a>', html_text, re.DOTALL | re.IGNORECASE
    )

    for i in range(min(len(urls), len(snippets), max_results)):
        clean_title = re.sub(r"<[^>]+>", "", titles[i] if i < len(titles) else "").strip()
        clean_snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip()
        raw_url = urls[i].strip()
        if "uddg=" in raw_url:
            query_parts = urllib.parse.parse_qs(urllib.parse.urlparse(raw_url).query)
            uddg_val = query_parts.get("uddg")
            if uddg_val:
                raw_url = uddg_val[0]
        elif raw_url.startswith("//"):
            raw_url = f"https:{raw_url}"

        results.append(
            {
                "title": clean_title,
                "url": raw_url,
                "snippet": clean_snippet,
            }
        )

    return {
        "status": "ok",
        "query": query,
        "results": results,
    }
