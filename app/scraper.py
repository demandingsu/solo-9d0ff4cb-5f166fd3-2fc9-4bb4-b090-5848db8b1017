"""供应商目录抓取：同站分页列表 + 匹配型号详情页解析（requests + BeautifulSoup）。

抓取流程：
1. 从目录列表页出发，沿"下一页"链接在同站内翻页（去重、限页数）；
2. 收集列表中的详情链接（原始链接 URL 去重），并按列表文案初筛目标型号；
3. 逐个抓取详情链接，跟随 HTTP 重定向与同站 HTML meta refresh 跳转，
   以**跳转后的实际详情地址**作为包装的唯一标识——同一包装的旧/新链接
   只纳入一次价格与库存；
4. 列表文案已确认匹配目标型号的链接，若跳转或抓取/解析失败，记录到
   failed_details（调用方据此不给出确定最低价），不会被当作已去重商品忽略。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

MAX_PAGES = 50          # 分页抓取上限，防止死循环或异常站点
MAX_REDIRECT_HOPS = 10  # 单条详情链接的最大跳转次数（HTTP 重定向 + meta refresh）
REQUEST_TIMEOUT = 10    # 单次请求超时（秒）
USER_AGENT = "membrane-procurement-bot/1.0"

# 列表页中详情链接的 CSS 选择器，按优先级依次尝试
PRODUCT_LINK_SELECTORS = [
    "a.product-link",
    "li.product-item a[href]",
    ".product-list a[href]",
]
NEXT_TEXT_RE = re.compile(r"(下一页|下页|next|»|›)", re.I)
NUM_RE = re.compile(r"([\d,]+(?:\.\d+)?)")
META_REFRESH_RE = re.compile(
    r"""^\s*\d+\s*;\s*url\s*=\s*(?P<quote>["']?)(?P<url>.*?)(?P=quote)\s*$""",
    re.I,
)


class ScrapeError(Exception):
    """抓取或解析失败。"""


@dataclass
class OfferInfo:
    """一个匹配详情页解析出的采购信息。"""

    detail_url: str                       # 跳转后的实际详情地址（包装唯一标识）
    title: str
    model: str
    pack_size: int                        # 每包片数
    price_fen: int                        # 含税包价（分）
    stock_packs: int                      # 可购包数
    source_link_urls: list[str] = field(default_factory=list)  # 指向本包装的目录原始链接


@dataclass
class AliasInfo:
    """一条指向同一详情页的别名（旧/新）链接。"""

    source_link_url: str   # 目录中出现的原始链接 URL
    final_detail_url: str  # 跳转后的实际详情地址


@dataclass
class CrawlResult:
    list_pages: list[str]           # 实际抓取到的列表页 URL（按顺序）
    matched: list[OfferInfo]        # 匹配型号且解析成功的详情（按最终 URL 去重）
    failed_details: list[str]       # 匹配但跳转/抓取/解析失败的原始链接 URL
    duplicate_details: int          # 列表中原始链接完全重复被跳过的次数
    alias_details: list[AliasInfo]  # 跳转后落到同一详情页的别名链接
    unverified_failed: list[str]    # 兜底扫描时无法抓取、未能核对型号的链接 URL


def normalize_model(text: str) -> str:
    """型号归一化：去空白/连字符/下划线并转大写，用于包含匹配。"""
    return re.sub(r"[\s\-_]+", "", text or "").upper()


def _abs_url(href: str, base: str) -> str:
    return urldefrag(urljoin(base, href))[0]


def _canonical_url(url: str) -> str:
    """统一 URL 形态：去 fragment，空路径补 "/"，使同一页面始终得到同一标识。"""
    url = urldefrag(url)[0]
    parts = urlparse(url)
    path = parts.path or "/"
    return parts._replace(path=path).geturl()


def _same_site(url: str, netloc: str) -> bool:
    return urlparse(url).netloc == netloc


def _get(session: requests.Session, url: str) -> requests.Response:
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise ScrapeError(f"页面抓取失败 {url}: {exc}") from exc
    return resp


def _get_soup(session: requests.Session, url: str) -> BeautifulSoup:
    resp = _get(session, url)
    # 用原始字节交给 BeautifulSoup，按页面 <meta charset> 自动识别编码
    return BeautifulSoup(resp.content, "html.parser")


def _meta_refresh_target(soup: BeautifulSoup, current_url: str, netloc: str) -> str | None:
    """解析 HTML <meta http-equiv="refresh" content="0; url=...">，只接受同站跳转。"""
    for meta in soup.find_all("meta", attrs={"http-equiv": re.compile(r"refresh", re.I)}):
        content = meta.get("content") or ""
        m = META_REFRESH_RE.match(content)
        if not m:
            continue
        target = _abs_url(m.group("url").strip(), current_url)
        if target != current_url and _same_site(target, netloc):
            return target
    return None


def fetch_final_detail(session: requests.Session, start_url: str, netloc: str) -> tuple[str, BeautifulSoup]:
    """跟随 HTTP 重定向与同站 meta refresh 跳转，返回 (最终详情 URL, 详情页 soup)。

    任一跳转或抓取失败均抛 ScrapeError（调用方保留的是原始链接 URL）。
    """
    url = start_url
    hops = 0
    while True:
        resp = _get(session, url)  # requests 默认跟随 HTTP 3xx 重定向
        final_url = _canonical_url(resp.url)
        soup = BeautifulSoup(resp.content, "html.parser")
        target = _meta_refresh_target(soup, final_url, netloc)
        if target is None:
            return final_url, soup
        hops += 1
        if hops > MAX_REDIRECT_HOPS:
            raise ScrapeError(f"页面跳转次数超过上限 {MAX_REDIRECT_HOPS}: {start_url}")
        url = target


def _product_links(soup: BeautifulSoup):
    for sel in PRODUCT_LINK_SELECTORS:
        links = soup.select(sel)
        if links:
            return links
    return []


def _next_page_url(soup: BeautifulSoup, current_url: str, netloc: str):
    """在同站内寻找"下一页"链接：rel=next / class=next / 分页区文本匹配。"""
    candidates = []
    for a in soup.find_all("a", href=True):
        rel = a.get("rel") or []
        classes = a.get("class") or []
        if "next" in rel or "next" in classes:
            candidates.append(a)
    if not candidates:
        for a in soup.select(".pagination a[href]"):
            if NEXT_TEXT_RE.search(a.get_text(strip=True)):
                candidates.append(a)
    for a in candidates:
        url = _abs_url(a["href"], current_url)
        if _same_site(url, netloc) and url != current_url:
            return url
    return None


def _pick_text(soup: BeautifulSoup, selectors: list[str]):
    for sel in selectors:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)
    return None


def _to_fen(num_str: str) -> int:
    d = Decimal(num_str.replace(",", ""))
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_detail_page(url: str, soup: BeautifulSoup) -> OfferInfo:
    """解析详情页：型号、每包片数、含税包价、可购包数。缺关键字段视为解析失败。"""
    page_text = soup.get_text("\n", strip=True)

    title = _pick_text(soup, ["h1.product-title", "h1"])
    if not title and soup.title:
        title = soup.title.get_text(strip=True)
    title = title or ""

    model = _pick_text(soup, [".product-model", ".model", '[data-field="model"]'])
    if not model:
        m = re.search(r"型号\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9\-_]*)", page_text)
        model = m.group(1) if m else ""

    pack_size = None
    raw = _pick_text(soup, [".pack-size", '[data-field="pack-size"]'])
    if raw:
        m = NUM_RE.search(raw)
        pack_size = int(m.group(1).replace(",", "")) if m else None
    if pack_size is None:
        m = re.search(r"每包片数\s*[:：]?\s*(\d+)", page_text) or re.search(r"(\d+)\s*片\s*/\s*包", page_text)
        pack_size = int(m.group(1)) if m else None

    price_fen = None
    raw = _pick_text(soup, [".pack-price", ".price", '[data-field="price"]'])
    if raw:
        m = NUM_RE.search(raw)
        price_fen = _to_fen(m.group(1)) if m else None
    if price_fen is None:
        m = re.search(r"含税包价\s*[:：]?\s*[¥￥]?\s*([\d,]+(?:\.\d+)?)", page_text)
        price_fen = _to_fen(m.group(1)) if m else None

    stock_packs = None
    raw = _pick_text(soup, [".stock-packs", ".stock", '[data-field="stock"]'])
    if raw:
        m = NUM_RE.search(raw)
        stock_packs = int(m.group(1).replace(",", "")) if m else None
    if stock_packs is None:
        m = re.search(r"可购包数\s*[:：]?\s*(\d+)", page_text)
        stock_packs = int(m.group(1)) if m else None

    missing = [
        name
        for name, val in [("每包片数", pack_size), ("含税包价", price_fen), ("可购包数", stock_packs)]
        if val is None
    ]
    if missing:
        raise ScrapeError(f"详情页解析失败 {url}: 缺少字段 {'、'.join(missing)}")

    return OfferInfo(
        detail_url=url,
        title=title,
        model=model,
        pack_size=pack_size,      # type: ignore[arg-type]
        price_fen=price_fen,      # type: ignore[arg-type]
        stock_packs=stock_packs,  # type: ignore[arg-type]
    )


def crawl_catalog(catalog_url: str, model: str) -> CrawlResult:
    """抓取目录分页并收集匹配型号的详情。列表页抓取失败会抛 ScrapeError。"""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    netloc = urlparse(catalog_url).netloc
    target = normalize_model(model)

    list_pages: list[str] = []
    entries: list[tuple[str, str]] = []  # (详情原始链接 URL, 列表文案)
    seen_links: set[str] = set()
    # 每条原始链接在目录中出现过的所有 URL（完全重复时不再抓取，但仍计入真实来源）
    link_occurrences: dict[str, list[str]] = {}
    duplicates = 0

    url: str | None = catalog_url
    seen_pages: set[str] = set()
    while url and url not in seen_pages and len(list_pages) < MAX_PAGES:
        seen_pages.add(url)
        soup = _get_soup(session, url)  # 列表页失败：整个目录不可用，直接抛错
        list_pages.append(url)
        for a in _product_links(soup):
            href = a.get("href")
            if not href:
                continue
            link_url = _canonical_url(_abs_url(href, url))
            if not _same_site(link_url, netloc):
                continue  # 只接受同站详情链接
            if link_url in seen_links:
                duplicates += 1  # 原始链接完全重复，先跳过；失败的链接不会在此阶段被吞掉
                link_occurrences[link_url].append(link_url)
                continue
            seen_links.add(link_url)
            link_occurrences[link_url] = [link_url]
            entries.append((link_url, a.get_text(" ", strip=True)))
        url = _next_page_url(soup, url, netloc)

    # 先用列表文案匹配型号；若一个都匹配不上，退回逐个抓取详情页核对型号
    matched_entries = [(u, t) for u, t in entries if target in normalize_model(t)]
    fallback_scan = not matched_entries and bool(entries)
    candidates = entries if fallback_scan else matched_entries

    matched: list[OfferInfo] = []
    offers_by_url: dict[str, OfferInfo] = {}
    failed: list[str] = []
    unverified: list[str] = []
    aliases: list[AliasInfo] = []

    try:
        for link_url, _text in candidates:
            try:
                # 关键：以跳转后的实际详情地址识别包装，而非目录里的原始链接
                final_url, soup = fetch_final_detail(session, link_url, netloc)
                offer = parse_detail_page(final_url, soup)
            except ScrapeError:
                if fallback_scan:
                    # 兜底扫描时无法确认该页是否匹配，单独记录为"未能核对"
                    unverified.append(link_url)
                else:
                    # 列表文案已确认匹配的链接跳转/抓取/解析失败：
                    # 必须如实上报，不能当作已去重商品忽略，也不能给确定最低价
                    failed.append(link_url)
                continue

            # 列表文案匹配但详情型号不符的，视为非目标商品，静默跳过
            if target not in normalize_model(offer.model) and target not in normalize_model(offer.title):
                continue

            existing = offers_by_url.get(final_url)
            is_alias = link_url != final_url  # 原始链接经跳转才到实际详情页 → 别名入口
            if existing is None:
                offer.detail_url = final_url
                # 真实来源包含该链接在目录中的全部出现（含跨页完全重复的原始链接）
                offer.source_link_urls = list(link_occurrences.get(link_url, [link_url]))
                offers_by_url[final_url] = offer
                matched.append(offer)
            else:
                # 旧/新链接跳到同一详情页：同一包装只纳入一次价格与库存
                existing.source_link_urls.extend(link_occurrences.get(link_url, [link_url]))
            if is_alias:
                # 别名记录与是否首次出现无关：每个跳转入口都如实登记
                aliases.append(AliasInfo(source_link_url=link_url, final_detail_url=final_url))
    finally:
        session.close()

    for offer in matched:
        # 去重但保留首次出现顺序，作为该包装在目录中的真实来源清单
        offer.source_link_urls = list(dict.fromkeys(offer.source_link_urls))

    return CrawlResult(
        list_pages=list_pages,
        matched=matched,
        failed_details=failed,
        duplicate_details=duplicates,
        alias_details=aliases,
        unverified_failed=unverified,
    )
