import socket
import unittest

import httpx

from shengbo_prelive.research import ResearchService


def public_dns(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class ResearchTests(unittest.TestCase):
    def test_fetch_pins_validated_public_ip_and_strips_scripts(self):
        requests = []
        def handler(request):
            requests.append(request)
            self.assertEqual(request.url.host, "93.184.216.34")
            self.assertEqual(request.headers["host"], "public.example")
            self.assertEqual(request.extensions["sni_hostname"], "public.example")
            return httpx.Response(200, headers={"Content-Type": "text/html; charset=utf-8"}, text="<h1>商品标题</h1><script>private()</script><p>真实正文</p>")
        result = ResearchService(transport=httpx.MockTransport(handler), resolver=public_dns).fetch("https://public.example/product")
        self.assertEqual(result["status"], "ok")
        self.assertIn("真实正文", result["text"])
        self.assertNotIn("private()", result["text"])
        self.assertEqual(result["source_url"], "https://public.example/product")
    def test_private_dns_ipv6_and_local_urls_rejected_before_network(self):
        for url in ["http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/metadata", "http://localhost/", "file:///etc/passwd", "https://user:pass@public.example/", "http://10.1.2.3/"]:
            with self.subTest(url=url):
                def network(request):
                    self.fail("unexpected network")
                result = ResearchService(transport=httpx.MockTransport(network), resolver=public_dns).fetch(url)
                self.assertEqual(result["status"], "error")
        def mixed(host, port, **kwargs):
            return public_dns(host, port) + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", port))]
        self.assertEqual(ResearchService(resolver=mixed).fetch("https://mixed.example/")["status"], "error")
    def test_redirect_is_revalidated_and_private_redirect_not_followed(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/internal"})
        result = ResearchService(transport=httpx.MockTransport(handler), resolver=public_dns).fetch("https://public.example/")
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(requests), 1)
    def test_response_limits_and_failures_are_not_fabricated_content(self):
        cases = [httpx.Response(403, text="blocked"), httpx.Response(200, headers={"Content-Type": "application/pdf"}, content=b"%PDF"),
                 httpx.Response(200, headers={"Content-Length": "1000001"}, content=b"x"),
                 httpx.Response(200, headers={"Content-Encoding": "unsupported"}, content=b"opaque")]
        for response in cases:
            service = ResearchService(transport=httpx.MockTransport(lambda _: response), resolver=public_dns)
            result = service.fetch("https://public.example/")
            self.assertEqual(result["status"], "error")
            self.assertNotIn("text", result)
    def test_search_maps_only_returned_public_records_and_bounded_limit(self):
        class FakeSearch:
            def text(self, query, **kwargs):
                self.query = query
                return [{"title": "实际返回", "href": "https://public.example/", "body": "实际摘要"},
                        {"title": "private", "href": "http://127.0.0.1/", "body": "private"}]
        result = ResearchService(search_factory=FakeSearch).search("测试商品", 3)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["snippet"], "实际摘要")
        self.assertEqual(ResearchService(search_factory=FakeSearch).search("x", 6)["status"], "error")
    def test_search_failures_do_not_create_evidence(self):
        class FailingSearch:
            def text(self, *args, **kwargs):
                raise RuntimeError("private network failure detail")
        result = ResearchService(search_factory=FailingSearch).search("query", 5)
        self.assertEqual(result["status"], "error")
        self.assertNotIn("results", result)
        self.assertNotIn("private network", str(result))


if __name__ == "__main__":
    unittest.main()
