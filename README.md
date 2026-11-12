# 滤膜采购组合优化 API

实验室耗材采购场景：给定供应商**目录 URL**、**滤膜型号**和**需求片数**，API 用
`requests` + `BeautifulSoup` 抓取同站分页列表与匹配型号的详情页，读取每种包装的
**每包片数 / 含税包价 / 可购包数**，在库存约束内求**总价最低的整包组合**（不同包装可混购；
同价时优先超量更少的组合），并返回各包装采购包数、实到片数、总价与详情来源。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

包含两个服务：

| 服务 | 地址 | 说明 |
|---|---|---|
| `api` | http://localhost:8000 | FastAPI 服务（文档见 `/docs`） |
| `sample-site` | http://localhost:8080 | 离线分页样本站（3 页目录 + 6 个详情/跳转页） |

调用示例（注意：`catalog_url` 由 API 容器访问，Compose 网络内请用服务名
`http://sample-site:8080/`，而不是 `localhost:8080`）：

```bash
curl -X POST http://localhost:8000/api/v1/quote \
  -H 'Content-Type: application/json' \
  -d '{"catalog_url": "http://sample-site:8080/", "model": "MF-50", "demand": 200}'
```

样本站中同一 `MF-50` 100 片装在目录里有**旧链接、现行链接、新别名**三个入口，旧链接与
新别名都跳转到同一个详情页，而该包装**仅剩 1 包（100 片）**。需求 200 片时返回
`insufficient_stock`（库存只按跳转后的实际详情地址计算一次，不会被三个入口重复累计）：

```json
{
  "status": "insufficient_stock",
  "model": "MF-50",
  "demand": 200,
  "matched_detail_pages": ["http://sample-site:8080/details/mf50-100.html"],
  "duplicate_detail_links_skipped": 1,
  "alias_detail_links": [
    {"source_link_url": ".../details/mf50-100-old.html", "final_detail_url": ".../details/mf50-100.html"},
    {"source_link_url": ".../details/mf50-100-alias.html", "final_detail_url": ".../details/mf50-100.html"}
  ],
  "total_available_pieces": 100,
  "solution": null
}
```

需求降到 100 片时可成交，报价明细同时给出**实际详情地址**与目录中指向它的**全部真实来源链接**：

```json
{
  "status": "ok",
  "solution": {
    "items": [
      {
        "detail_url": "http://sample-site:8080/details/mf50-100.html",
        "catalog_link_urls": [
          "http://sample-site:8080/details/mf50-100-old.html",
          "http://sample-site:8080/details/mf50-100.html",
          "http://sample-site:8080/details/mf50-100-alias.html"
        ],
        "pack_size": 100,
        "pack_price": "225.00",
        "stock_packs": 1,
        "purchased_packs": 1,
        "delivered_pieces": 100,
        "subtotal_price": "225.00"
      }
    ],
    "total_pieces": 100,
    "overage": 0,
    "total_price": "225.00",
    "currency": "CNY"
  }
}
```

## API

### `POST /api/v1/quote`

请求体：

| 字段 | 类型 | 说明 |
|---|---|---|
| `catalog_url` | string | 供应商目录列表页 URL（任意一页均可，自动沿"下一页"翻页） |
| `model` | string | 滤膜型号，如 `MF-50`（忽略大小写、空格与连字符的包含匹配） |
| `demand` | int ≥ 1 | 需求片数 |

响应 `status` 语义：

| status | 含义 | 是否给出确定最低价 |
|---|---|---|
| `ok` | 已找到库存内总价最低的整包组合 | ✅ `solution` |
| `insufficient_stock` | 可购总量 < 需求（附 `total_available_pieces`） | ❌ |
| `scrape_failed` | 匹配型号的链接跳转/抓取/解析失败（附 `failed_detail_pages`） | ❌ |
| `no_matching_products` | 目录中无匹配型号（若有页面未能核对会附 `unverified_failed_pages`） | ❌ |
| `catalog_unreachable` | 目录列表页抓取失败 | ❌ |
| `demand_too_large` | 需求超出求解器安全上限 | ❌ |

响应中与来源/去重相关的字段：

| 字段 | 含义 |
|---|---|
| `matched_detail_pages` | 去重后实际参与报价的**跳转后详情地址**清单 |
| `duplicate_detail_links_skipped` | 目录中原始链接完全相同、跨页重复出现被跳过的次数 |
| `alias_detail_links` | 原始链接经跳转落到同一详情页的别名记录（`source_link_url` → `final_detail_url`） |
| `solution.items[].detail_url` | 该包装跳转后的实际详情地址（包装唯一标识） |
| `solution.items[].catalog_link_urls` | 目录中指向该包装的全部原始链接（真实报价来源） |
| `failed_detail_pages` | 标有目标型号却跳转/抓取/解析失败的**原始链接**，不会被去重吞掉 |

## 规则与实现要点

- **同站分页**：从 `catalog_url` 出发，识别 `rel="next"` / `class="next"` / 分页区
  "下一页"链接，仅跟随与目录同站点（同 host）的链接；visited 集合防环，上限 50 页。
- **以跳转后的实际详情地址识别包装**：抓取详情链接时同时跟随 **HTTP 3xx 重定向**与
  同站 **HTML `<meta http-equiv="refresh">` 跳转**（跳数上限 10，含跳转环保护），
  并对 URL 做统一规范化（去 fragment、空路径补 `/`）。同一实际详情地址只解析一次，
  旧/新链接等别名入口只累计**一次**价格与库存，别名登记在 `alias_detail_links`。
- **原始链接去重与真实来源**：目录中完全相同的原始链接跨页重复出现时不再重复抓取
  （计入 `duplicate_detail_links_skipped`），但其出现仍并入该包装的
  `catalog_link_urls`，保证报价来源完整可追溯。
- **失效链接不被去重吞掉**：列表文案已确认匹配目标型号的链接，只要跳转、抓取或解析
  失败，就按原始链接记入 `failed_detail_pages` 并返回 `scrape_failed`——既不会被当作
  "已去重的同一商品"忽略，也不会在信息不全时给出确定最低价。
- **型号匹配**：先按列表文案匹配；一个都匹配不上时退回逐个抓取详情页核对型号
  （此时若有页面抓取失败，因无法确认是否漏掉更优匹配，同样不给确定最低价）。
- **详情解析**：优先 CSS 选择器（`.product-model` / `.pack-size` / `.pack-price` /
  `.stock-packs`），回退到中文标签正则（`型号：`、`每包片数：`、`含税包价：`、
  `可购包数：`）；缺任一关键字段即视为该详情解析失败。价格以"分"为整数运算，
  避免浮点误差。
- **组合优化**（`app/optimizer.py`）：有界背包 → 二进制拆分转 0/1 背包，对
  "恰好 j 片"做最小费用 DP，再按 **(总价, 总片数)** 字典序取最优——即总价最低，
  同价时超量更少。可证最优解总片数 < 需求 + 最大包装片数，DP 状态有界。
- **不给确定最低价的情形**：库存总量不足；标有目标型号的链接跳转/抓取/解析失败。

## 样本站说明（`sample_site/`）

- 3 页分页目录，`index.html → page2.html → page3.html`；
- `MF-50` 只保留 **100 片装**（¥225，**仅剩 1 包**）一个包装，目录中有三个入口：
  - `details/mf50-100-old.html`：旧链接，meta refresh 跳转到 `mf50-100.html`；
  - `details/mf50-100.html`：现行链接（在第 2、3 页重复出现，验证跨页去重）；
  - `details/mf50-100-alias.html`：改版后的新别名，同样跳转到 `mf50-100.html`；
  - 三个入口共享同一库存，需求 200 片 → `insufficient_stock`，需求 100 片 → 成交 1 包；
- 失效链接两类，均不得被去重忽略：
  - `details/ca-22-alias-broken.html`：标注 `CA-22` 的别名，跳转到不存在的详情页；
  - `details/broken-404.html`：标注 `BROKEN-1`，直接 404；
- 另有 `NY-66`、`CA-22` 两个其他型号商品。

试几组：`MF-50 demand=200` → `insufficient_stock`（可购 100 片）；`demand=100` →
成交 1 包 ¥225；`CA-22` / `BROKEN-1` → `scrape_failed`；`NY-66 demand=150` → 2 包
100 片装 ¥360。

## 本地开发与测试

```bash
pip install -r app/requirements.txt
python -m unittest discover -s tests -v   # 优化器 + 抓取器单元测试
python tests/e2e_check.py                 # 端到端检查（本地起样本站，走完整 API 链路）
```

- `tests/test_optimizer.py`：组合优化器的库存约束、同价超量、库存不足等；
- `tests/test_scraper.py`：HTTP 302 / meta refresh 跳转、跳转环保护、按最终 URL 去重、
  失效目标链接不被去重吞掉、兜底扫描的"未能核对"语义；
- `tests/e2e_check.py`：FastAPI 完整链路，含样本站别名去重（需求 200 库存不足）、
  真实 302 重定向去重、失效链接与目录不可达等场景。

## 目录结构

```
app/
  main.py        FastAPI 入口与响应组装
  scraper.py     分页抓取 + 重定向/meta refresh 跟随 + 详情解析（requests + BeautifulSoup）
  optimizer.py   库存约束下的最低总价整包组合（DP）
  requirements.txt / Dockerfile
sample_site/     离线分页样本站（静态 HTML，含跳转别名与失效链接）
tests/           单元测试 + 端到端检查
docker-compose.yml
```
