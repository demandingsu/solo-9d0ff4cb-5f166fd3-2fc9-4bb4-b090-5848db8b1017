"""端到端检查：本地 HTTP 服务托管样本站，调用 FastAPI 完整链路。

运行：python3 tests/e2e_check.py
"""
import functools
import http.server
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402

SITE_DIR = ROOT / "sample_site"


class RedirectHandler(http.server.SimpleHTTPRequestHandler):
    """演示真实 HTTP 302 重定向：old-302.html 跳转到 canonical.html。"""

    def do_GET(self):  # noqa: N802 - http.server 约定命名
        if self.path.endswith("/old-302.html"):
            target = self.path.replace("old-302.html", "canonical.html")
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return
        super().do_GET()


def make_server(directory: str, handler_cls=http.server.SimpleHTTPRequestHandler):
    handler = functools.partial(handler_cls, directory=directory)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def check(name: str, cond: bool, extra: str = ""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {extra}" if extra else ""))
    if not cond:
        raise SystemExit(f"检查失败: {name}")


def main():
    httpd, base = make_server(str(SITE_DIR))
    client = TestClient(app)
    catalog = f"{base}/index.html"
    print(f"样本站: {catalog}")

    # 1) 核心场景：旧/新链接跳转到同一 100 片装（仅剩 1 包），需求 200 → 库存不足
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "MF-50", "demand": 200})
    assert r.status_code == 200, r.text
    d = r.json()
    check("MF-50/200 状态 insufficient_stock", d["status"] == "insufficient_stock", d["status"])
    check("无方案、无确定最低价", d["solution"] is None)
    check("库存只算一次：可购总量 100 片", d["total_available_pieces"] == 100, str(d.get("total_available_pieces")))

    # 2) 同一包装按跳转后的实际详情地址只识别一次
    check("匹配详情仅 1 个（100 片装）", len(d["matched_detail_pages"]) == 1, str(d["matched_detail_pages"]))
    check(
        "匹配详情为跳转后的实际地址",
        d["matched_detail_pages"][0].endswith("/details/mf50-100.html"),
        d["matched_detail_pages"][0],
    )

    # 3) 别名与跨页原始链接重复的计数
    check("抓取 3 个列表页", len(d["list_pages"]) == 3, str(d["list_pages"]))
    check("跨页原始链接重复跳过 1 次", d["duplicate_detail_links_skipped"] == 1)
    check("识别出 2 条别名链接", len(d["alias_detail_links"]) == 2, str(d["alias_detail_links"]))
    alias_sources = {a["source_link_url"].rsplit("/", 1)[-1] for a in d["alias_detail_links"]}
    check(
        "别名来源为旧链接与新别名",
        alias_sources == {"mf50-100-old.html", "mf50-100-alias.html"},
        str(alias_sources),
    )
    check(
        "别名均落到 mf50-100.html",
        all(a["final_detail_url"].endswith("/details/mf50-100.html") for a in d["alias_detail_links"]),
    )

    # 4) 需求 100：恰好买这唯一一包，报价明细给出真实来源（全部原始链接）
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "MF-50", "demand": 100})
    d = r.json()
    check("MF-50/100 状态 ok", d["status"] == "ok", d.get("message", ""))
    items = d["solution"]["items"]
    check("仅一种包装、采购 1 包", len(items) == 1 and items[0]["purchased_packs"] == 1)
    check("总价 ¥225.00", d["solution"]["total_price"] == "225.00")
    check("实到 100 片、零超量", d["solution"]["total_pieces"] == 100 and d["solution"]["overage"] == 0)
    check("来源为实际详情地址", items[0]["detail_url"].endswith("/details/mf50-100.html"))
    source_names = sorted(u.rsplit("/", 1)[-1] for u in items[0]["catalog_link_urls"])
    check(
        "明细给出全部真实来源链接",
        source_names == ["mf50-100-alias.html", "mf50-100-old.html", "mf50-100.html"],
        str(source_names),
    )

    # 5) 失效别名（标注 CA-22 的链接跳转后 404）：不得当作已去重商品忽略
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "CA-22", "demand": 200})
    d = r.json()
    check("CA-22/200 状态 scrape_failed", d["status"] == "scrape_failed", d["status"])
    check("无方案、无确定最低价", d["solution"] is None)
    check(
        "上报失效别名原始链接",
        any(u.endswith("/details/ca-22-alias-broken.html") for u in d["failed_detail_pages"]),
        str(d["failed_detail_pages"]),
    )

    # 6) 直接失效的详情链接（BROKEN-1 404）→ 不给确定最低价
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "BROKEN-1", "demand": 10})
    d = r.json()
    check("BROKEN-1 状态 scrape_failed", d["status"] == "scrape_failed")
    check("无方案", d["solution"] is None)
    check("报告失败详情页", d["failed_detail_pages"][0].endswith("/details/broken-404.html"))

    # 7) 无匹配型号：兜底扫描时失效页无法核对，应在响应中如实标注
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "NOPE-9", "demand": 10})
    d = r.json()
    check("NOPE-9 状态 no_matching_products", d["status"] == "no_matching_products")
    check("无方案", d["solution"] is None)
    unverified = d.get("unverified_failed_pages", [])
    check(
        "标注未能核对的失效页面",
        {u.rsplit("/", 1)[-1] for u in unverified}
        == {"ca-22-alias-broken.html", "broken-404.html"},
        str(unverified),
    )

    # 8) 其他型号正常匹配：NY-66 需求 150 → 2 包 100 片装 ¥360
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "NY-66", "demand": 150})
    d = r.json()
    check("NY-66/150 状态 ok", d["status"] == "ok", d.get("message", ""))
    check("总价 ¥360.00", d["solution"]["total_price"] == "360.00")
    check("采购 2 包", d["solution"]["items"][0]["purchased_packs"] == 2)

    # 9) 目录不可达
    r = client.post("/api/v1/quote", json={"catalog_url": f"{base}/no-such-page.html", "model": "MF-50", "demand": 10})
    d = r.json()
    check("目录不存在状态 catalog_unreachable", d["status"] == "catalog_unreachable")

    # 10) 真实 HTTP 302 重定向：同一包装的两个入口按最终 URL 去重
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        (tmp_path / "details").mkdir()
        (tmp_path / "index.html").write_text(
            '<html><body><ul class="product-list">'
            '<li class="product-item"><a class="product-link" href="details/old-302.html">'
            'MCE 微孔滤膜 RED-1（100片/包 旧链接）</a></li>'
            '<li class="product-item"><a class="product-link" href="details/canonical.html">'
            'MCE 微孔滤膜 RED-1（100片/包 现行链接）</a></li>'
            "</ul></body></html>",
            encoding="utf-8",
        )
        detail = (
            '<html><body><h1 class="product-title">MCE 微孔滤膜 RED-1（100片装）</h1>'
            '<table><tr><td class="product-model">RED-1</td>'
            '<td class="pack-size">100</td>'
            '<td class="pack-price">¥200.00</td>'
            '<td class="stock-packs">1</td></tr></table></body></html>'
        )
        (tmp_path / "details" / "canonical.html").write_text(detail, encoding="utf-8")
        httpd2, base2 = make_server(tmp, RedirectHandler)
        cat2 = f"{base2}/index.html"

        r = client.post("/api/v1/quote", json={"catalog_url": cat2, "model": "RED-1", "demand": 200})
        d = r.json()
        check("302 去重后库存 100 → 需求 200 不足", d["status"] == "insufficient_stock", d["status"])
        check("302 后仅识别 canonical 一个包装", len(d["matched_detail_pages"]) == 1)
        check(
            "最终地址为 canonical.html",
            d["matched_detail_pages"][0].endswith("/details/canonical.html"),
        )
        check("302 旧链接计入别名", d["alias_detail_links"][0]["source_link_url"].endswith("/details/old-302.html"))
        httpd2.shutdown()
        httpd2.server_close()

    # 11) 标有目标型号的失效链接与有效链接并存：失效链接不得被去重吞掉，也不能给确定最低价
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        (tmp_path / "details").mkdir()
        (tmp_path / "index.html").write_text(
            '<html><body><ul class="product-list">'
            '<li class="product-item"><a class="product-link" href="details/good.html">'
            'MCE 微孔滤膜 GHOST-9（100片/包 在售）</a></li>'
            '<li class="product-item"><a class="product-link" href="details/ghost.html">'
            'MCE 微孔滤膜 GHOST-9（100片/包 迁移中）</a></li>'
            "</ul></body></html>",
            encoding="utf-8",
        )
        (tmp_path / "details" / "good.html").write_text(
            '<html><body><h1 class="product-title">MCE 微孔滤膜 GHOST-9（100片装）</h1>'
            '<table><tr><td class="product-model">GHOST-9</td>'
            '<td class="pack-size">100</td>'
            '<td class="pack-price">¥200.00</td>'
            '<td class="stock-packs">1</td></tr></table></body></html>',
            encoding="utf-8",
        )
        # details/ghost.html 故意不存在（404）
        httpd3, base3 = make_server(tmp)
        cat3 = f"{base3}/index.html"

        r = client.post("/api/v1/quote", json={"catalog_url": cat3, "model": "GHOST-9", "demand": 100})
        d = r.json()
        check("含失效目标链接 → scrape_failed", d["status"] == "scrape_failed", d["status"])
        check("不给确定最低价", d["solution"] is None)
        check(
            "失效链接按原始 URL 上报",
            any(u.endswith("/details/ghost.html") for u in d["failed_detail_pages"]),
            str(d["failed_detail_pages"]),
        )
        httpd3.shutdown()
        httpd3.server_close()

    httpd.shutdown()
    httpd.server_close()
    print("\n全部端到端检查通过 ✓")


if __name__ == "__main__":
    main()
