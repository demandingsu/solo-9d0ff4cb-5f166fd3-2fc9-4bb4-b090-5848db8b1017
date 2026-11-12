"""抓取器单元测试：重定向识别、按最终 URL 去重、失效链接语义。

运行：python3 -m unittest discover -s tests -v
"""
import functools
import http.server
import pathlib
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

import requests  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402
from scraper import (  # noqa: E402
    MAX_REDIRECT_HOPS,
    AliasInfo,
    ScrapeError,
    crawl_catalog,
    fetch_final_detail,
    normalize_model,
)


def _meta_refresh_html(target: str) -> str:
    return (
        '<html><head><meta charset="utf-8">'
        f'<meta http-equiv="refresh" content="0; url={target}"></head>'
        "<body>跳转中</body></html>"
    )


DETAIL_HTML = (
    '<html><body><h1 class="product-title">MCE 微孔滤膜 MF-50（100片装）</h1>'
    '<table><tr><td class="product-model">MF-50</td>'
    '<td class="pack-size">100</td>'
    '<td class="pack-price">¥225.00</td>'
    '<td class="stock-packs">1</td></tr></table></body></html>'
)


class _Server:
    def __init__(self, directory: str, handler_cls=http.server.SimpleHTTPRequestHandler):
        handler = functools.partial(handler_cls, directory=directory)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _302Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.endswith("/redirect-302.html"):
            self.send_response(302)
            self.send_header("Location", "final.html")
            self.end_headers()
            return
        super().do_GET()


class _LoopHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        # a 与 b 互相 meta refresh 由静态文件实现；此处模拟服务端持续 302 环
        if self.path.endswith("/loop.html"):
            self.send_response(302)
            self.send_header("Location", "loop.html")
            self.end_headers()
            return
        super().do_GET()


class TestNormalizeModel(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_model(" mf-50 "), "MF50")
        self.assertEqual(normalize_model("ny_66"), "NY66")
        self.assertEqual(normalize_model(None), "")


class TestFetchFinalDetail(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        (root / "final.html").write_text(DETAIL_HTML, encoding="utf-8")
        (root / "meta-old.html").write_text(_meta_refresh_html("final.html"), encoding="utf-8")
        (root / "meta-chain-1.html").write_text(_meta_refresh_html("meta-chain-2.html"), encoding="utf-8")
        (root / "meta-chain-2.html").write_text(_meta_refresh_html("final.html"), encoding="utf-8")
        (root / "meta-a.html").write_text(_meta_refresh_html("meta-b.html"), encoding="utf-8")
        (root / "meta-b.html").write_text(_meta_refresh_html("meta-a.html"), encoding="utf-8")
        self.server = _Server(self._tmp.name)
        self.session = requests.Session()
        self.netloc = f"127.0.0.1:{self.server.httpd.server_address[1]}"

    def tearDown(self):
        self.session.close()
        self.server.stop()
        self._tmp.cleanup()

    def test_plain_page_returns_own_url(self):
        final_url, soup = fetch_final_detail(self.session, f"{self.server.base}/final.html", self.netloc)
        self.assertTrue(final_url.endswith("/final.html"))
        self.assertIsInstance(soup, BeautifulSoup)

    def test_meta_refresh_followed_to_final_url(self):
        final_url, _ = fetch_final_detail(self.session, f"{self.server.base}/meta-old.html", self.netloc)
        self.assertTrue(final_url.endswith("/final.html"))

    def test_meta_refresh_chain(self):
        final_url, _ = fetch_final_detail(self.session, f"{self.server.base}/meta-chain-1.html", self.netloc)
        self.assertTrue(final_url.endswith("/final.html"))

    def test_meta_refresh_loop_raises(self):
        with self.assertRaises(ScrapeError):
            fetch_final_detail(self.session, f"{self.server.base}/meta-a.html", self.netloc)

    def test_missing_page_raises(self):
        with self.assertRaises(ScrapeError):
            fetch_final_detail(self.session, f"{self.server.base}/nope.html", self.netloc)


class TestHttp302Redirect(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        (root / "final.html").write_text(DETAIL_HTML, encoding="utf-8")
        self.server = _Server(self._tmp.name, _302Handler)
        self.session = requests.Session()
        self.netloc = f"127.0.0.1:{self.server.httpd.server_address[1]}"

    def tearDown(self):
        self.session.close()
        self.server.stop()
        self._tmp.cleanup()

    def test_http_302_followed(self):
        final_url, _ = fetch_final_detail(
            self.session, f"{self.server.base}/redirect-302.html", self.netloc
        )
        self.assertTrue(final_url.endswith("/final.html"))


class TestCrawlDedupAndFailures(unittest.TestCase):
    def _build_site(self, root: pathlib.Path):
        details = root / "details"
        details.mkdir()
        (details / "mf50-100.html").write_text(DETAIL_HTML, encoding="utf-8")
        (details / "mf50-100-old.html").write_text(
            _meta_refresh_html("mf50-100.html"), encoding="utf-8"
        )
        (details / "mf50-100-alias.html").write_text(
            _meta_refresh_html("mf50-100.html"), encoding="utf-8"
        )
        (root / "index.html").write_text(
            '<html><body><ul class="product-list">'
            '<li class="product-item"><a class="product-link" href="details/mf50-100-old.html">'
            "MCE 微孔滤膜 MF-50（100片/包 旧链接）</a></li>"
            '<li class="product-item"><a class="product-link" href="details/mf50-100.html">'
            "MCE 微孔滤膜 MF-50（100片/包 现行链接）</a></li>"
            '<li class="product-item"><a class="product-link" href="details/mf50-100-alias.html">'
            "MCE 微孔滤膜 MF-50（100片/包 新别名）</a></li>"
            "</ul></body></html>",
            encoding="utf-8",
        )

    def test_aliases_dedup_by_final_url_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self._build_site(root)
            server = _Server(tmp)
            try:
                result = crawl_catalog(f"{server.base}/index.html", "MF-50")
            finally:
                server.stop()

        self.assertEqual(len(result.matched), 1)
        offer = result.matched[0]
        self.assertTrue(offer.detail_url.endswith("/details/mf50-100.html"))
        # 库存与价格只纳入一次：100 片、1 包
        self.assertEqual(offer.stock_packs, 1)
        self.assertEqual(offer.pack_size, 100)
        self.assertEqual(offer.price_fen, 22500)
        # 报价真实来源：三个原始链接全部保留
        self.assertEqual(
            sorted(u.rsplit("/", 1)[-1] for u in offer.source_link_urls),
            ["mf50-100-alias.html", "mf50-100-old.html", "mf50-100.html"],
        )
        self.assertEqual(len(result.alias_details), 2)
        self.assertIsInstance(result.alias_details[0], AliasInfo)
        self.assertEqual(result.failed_details, [])

    def test_target_labeled_dead_link_is_failure_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self._build_site(root)
            # 再加一个标注目标型号、却指向不存在页面的链接
            (root / "index.html").write_text(
                (root / "index.html").read_text(encoding="utf-8").replace(
                    "</ul>",
                    '<li class="product-item"><a class="product-link" href="details/ghost.html">'
                    "MCE 微孔滤膜 MF-50（100片/包 迁移中）</a></li></ul>",
                ),
                encoding="utf-8",
            )
            server = _Server(tmp)
            try:
                result = crawl_catalog(f"{server.base}/index.html", "MF-50")
            finally:
                server.stop()

        # 失效链接没有被去重吞掉：进入 failed_details
        self.assertEqual(len(result.failed_details), 1)
        self.assertTrue(result.failed_details[0].endswith("/details/ghost.html"))
        # 有效包装仍被正常识别，不因失败链接而丢失
        self.assertEqual(len(result.matched), 1)

    def test_failed_alias_landing_on_404_reports_source_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            details = root / "details"
            details.mkdir()
            (details / "good.html").write_text(DETAIL_HTML, encoding="utf-8")
            (details / "broken-alias.html").write_text(
                _meta_refresh_html("gone.html"), encoding="utf-8"
            )
            (root / "index.html").write_text(
                '<html><body><ul class="product-list">'
                '<li class="product-item"><a class="product-link" href="details/good.html">'
                "MCE 微孔滤膜 MF-50（100片/包）</a></li>"
                '<li class="product-item"><a class="product-link" href="details/broken-alias.html">'
                "MCE 微孔滤膜 MF-50（100片/包 失效别名）</a></li>"
                "</ul></body></html>",
                encoding="utf-8",
            )
            server = _Server(tmp)
            try:
                result = crawl_catalog(f"{server.base}/index.html", "MF-50")
            finally:
                server.stop()

        self.assertEqual(len(result.matched), 1)
        self.assertEqual(
            [u.rsplit("/", 1)[-1] for u in result.failed_details],
            ["broken-alias.html"],
        )

    def test_fallback_scan_marks_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            details = root / "details"
            details.mkdir()
            (details / "d1.html").write_text(
                '<html><body><h1 class="product-title">特价滤膜 甲</h1>'
                '<table><tr><td class="product-model">FALL-1</td>'
                '<td class="pack-size">40</td><td class="pack-price">¥90.00</td>'
                '<td class="stock-packs">8</td></tr></table></body></html>',
                encoding="utf-8",
            )
            (root / "index.html").write_text(
                '<html><body><ul class="product-list">'
                '<li class="product-item"><a class="product-link" href="details/d1.html">特价滤膜 甲</a></li>'
                '<li class="product-item"><a class="product-link" href="details/d2.html">特价滤膜 乙</a></li>'
                "</ul></body></html>",
                encoding="utf-8",
            )
            server = _Server(tmp)
            try:
                result = crawl_catalog(f"{server.base}/index.html", "FALL-1")
            finally:
                server.stop()

        self.assertEqual(len(result.matched), 1)
        self.assertTrue(result.matched[0].detail_url.endswith("/details/d1.html"))
        self.assertEqual(
            [u.rsplit("/", 1)[-1] for u in result.unverified_failed], ["d2.html"]
        )
        self.assertEqual(result.failed_details, [])


class TestRedirectHopLimit(unittest.TestCase):
    def test_server_redirect_loop_raises_within_hop_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = _Server(tmp, _LoopHandler)
            session = requests.Session()
            netloc = f"127.0.0.1:{server.httpd.server_address[1]}"
            try:
                # requests 自身会先达到重定向上限抛 ScrapeError；无论如何必须报错而非静默
                with self.assertRaises(ScrapeError):
                    fetch_final_detail(session, f"{server.base}/loop.html", netloc)
            finally:
                session.close()
                server.stop()
        self.assertGreater(MAX_REDIRECT_HOPS, 0)


if __name__ == "__main__":
    unittest.main()
