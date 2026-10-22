"""Untrusted public-web research with no keys, private networking or fake results."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit, urljoin

import httpx


class _ReadableHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "section", "article"}:
            self.parts.append("\n")
    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self.hidden = max(0, self.hidden - 1)
    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)
    def text(self):
        return "\n".join(re.sub(r"\s+", " ", x).strip() for x in "".join(self.parts).splitlines() if x.strip())


class ResearchService:
    def __init__(self, *, transport=None, resolver=None, search_factory=None, timeout=15, max_bytes=1_000_000):
        self.transport, self.resolver = transport, resolver or socket.getaddrinfo
        self.search_factory = search_factory
        self.timeout, self.max_bytes = min(timeout, 30), min(max_bytes, 1_000_000)

    @staticmethod
    def _error(code):
        return {"status": "error", "error": {"code": code, "message": "公开资料未能读取，未生成替代或推测内容"}}

    def _target(self, url):
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in (None, 80, 443) or len(url) > 4096):
            raise ValueError("unsafe_url")
        hostname = parsed.hostname.encode("idna").decode()
        if hostname.lower().rstrip(".") in {"localhost", "localhost.localdomain"}:
            raise ValueError("unsafe_url")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            direct = ipaddress.ip_address(hostname)
            addresses = [str(direct)]
        except ValueError:
            rows = self.resolver(hostname, port, type=socket.SOCK_STREAM)
            addresses = list(dict.fromkeys(row[4][0] for row in rows))
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError("private_target_rejected")
        # Pin the already-validated address so a second DNS lookup cannot rebind
        # the connection to localhost. TLS SNI retains the original hostname.
        address = ipaddress.ip_address(addresses[0])
        authority = f"[{address}]" if address.version == 6 else str(address)
        if parsed.port:
            authority += ":" + str(port)
        pinned = urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))
        host_header = hostname + (":" + str(port) if parsed.port else "")
        canonical = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
        return canonical, pinned, hostname, host_header

    def search(self, query, limit=5):
        if not isinstance(query, str) or not query.strip() or len(query) > 1000 or not isinstance(limit, int) or not 1 <= limit <= 5:
            return self._error("search_input_invalid")
        try:
            if self.search_factory:
                client = self.search_factory()
            else:
                from ddgs import DDGS
                client = DDGS(timeout=self.timeout)
            rows = client.text(query, max_results=limit, backend="duckduckgo")
            results = []
            for row in rows:
                url = row.get("href") or row.get("url") or ""
                try:
                    parsed = urlsplit(url)
                    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                        continue
                    ip = None
                    try:
                        ip = ipaddress.ip_address(parsed.hostname)
                    except ValueError:
                        pass
                    if (ip is not None and not ip.is_global) or parsed.hostname.lower().rstrip(".") == "localhost":
                        continue
                except Exception:
                    continue
                results.append({"title": str(row.get("title", ""))[:1000], "url": url[:4096],
                                "snippet": str(row.get("body", row.get("snippet", "")))[:5000],
                                "trust": "untrusted_external_content"})
                if len(results) == limit:
                    break
            return {"status": "ok", "query": query, "results": results, "source": "ddgs_public_search"}
        except Exception:
            return self._error("public_search_unavailable")

    def fetch(self, url):
        try:
            with httpx.Client(transport=self.transport, timeout=self.timeout, follow_redirects=False, trust_env=False) as client:
                for redirect in range(5):
                    source, pinned, hostname, host = self._target(url)
                    with client.stream("GET", pinned, headers={"Host": host, "Accept": "text/html,text/plain", "Accept-Encoding": "identity",
                                                                "User-Agent": "ShengboPreliveResearch/1.0"},
                                       extensions={"sni_hostname": hostname}) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                return self._error("fetch_redirect_invalid")
                            url = urljoin(source, location)
                            continue
                        if not response.is_success:
                            return self._error("fetch_public_http_error")
                        mime = response.headers.get("content-type", "text/plain").split(";")[0].lower()
                        if mime not in {"text/html", "text/plain", "application/xhtml+xml"}:
                            return self._error("fetch_content_type_unsupported")
                        if response.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                            return self._error("fetch_compressed_content_rejected")
                        declared = response.headers.get("content-length")
                        if declared and int(declared) > self.max_bytes:
                            return self._error("fetch_size_limit")
                        chunks, size = [], 0
                        for chunk in response.iter_bytes(chunk_size=16384):
                            size += len(chunk)
                            if size > self.max_bytes:
                                return self._error("fetch_size_limit")
                            chunks.append(chunk)
                        body = b"".join(chunks)
                        content = body.decode(response.encoding or "utf-8", errors="replace")
                        if mime in {"text/html", "application/xhtml+xml"}:
                            parser = _ReadableHTML()
                            parser.feed(content)
                            content = parser.text()
                        if not content.strip():
                            return self._error("fetch_no_readable_text")
                        return {"status": "ok", "source_url": source, "text": content[:100000],
                                "content_hash": "sha256:" + hashlib.sha256(body).hexdigest(),
                                "fetched_at": datetime.now(timezone.utc).isoformat(), "mime": mime,
                                "truncated": len(content) > 100000, "trust": "untrusted_external_content"}
                return self._error("fetch_redirect_limit")
        except Exception:
            return self._error("fetch_target_or_network_rejected")
