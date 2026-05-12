"""HKEX PHIP/AP 监控抓取

HKEXnews 的 appindex.html 是 JS 渲染的，本模块采用多策略：
1. 直接抓取已知的 JSON 数据接口（HKEX 内部 JS 加载的数据文件）
2. HTML 静态解析（如果未来网站结构变化）
3. Playwright 动态渲染（最稳的回退方案）

对外接口：scan_new_phips(config, db) -> list[dict]
返回格式：
    [
      {
        "source_url": "https://www1.hkexnews.hk/.../sehk20251104012.pdf",
        "company_name": "示例控股有限公司",
        "stock_code": "1234",
        "board": "main",
        "document_type": "PHIP",
        "publish_date": "2025-11-04",
        "sponsor": "中金公司"
      },
      ...
    ]
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# HKEX 当前已知的几个数据文件（按优先级排序）。HKEX 偶有改版，请定期通过浏览器
# 开发者工具的 Network 面板核对实际 URL，更新本列表。
#
# 2026-05 实测：旧的 /app/appindex_active_*.json 接口已失效（返回 404）。
# 当前 appindex.html 页面通过 /ncms/json/eds/ 下按文件名模板拉取：
#   appactive_{app|appphip}_{sehk|gem}_{c|e}.json
# - app/appphip：是否只要申请版本(app)还是仅 PHIP(appphip)
# - sehk：主板；gem：创业板
# - c/e：中文/英文版本（公司名字段的语言）
KNOWN_JSON_ENDPOINTS = {
    "main": [
        # 只拉中文版本，英文版本是同一批公司的英文 PDF，会重复消耗 token
        "https://www1.hkexnews.hk/ncms/json/eds/appactive_appphip_sehk_c.json",
        # 旧 endpoint 作为兜底保留（已知 404，仅用于历史兼容）
        "https://www1.hkexnews.hk/app/appindex_active_main_c.json",
    ],
    "gem": [
        "https://www1.hkexnews.hk/ncms/json/eds/appactive_appphip_gem_c.json",
        "https://www1.hkexnews.hk/app/appindex_active_gem_c.json",
    ],
}

# 当同时需要处理 AP（申请版本）时使用
KNOWN_JSON_ENDPOINTS_AP = {
    "main": [
        "https://www1.hkexnews.hk/ncms/json/eds/appactive_app_sehk_c.json",
    ],
    "gem": [
        "https://www1.hkexnews.hk/ncms/json/eds/appactive_app_gem_c.json",
    ],
}

HKEX_DOC_BASE = "https://www1.hkexnews.hk/app/"

# PHIP / AP 文件名识别（HKEX 的 PDF URL 通常含 `phip` 或 `app` 关键字）
PHIP_PATTERN = re.compile(r"phip", re.IGNORECASE)
AP_PATTERN = re.compile(r"sehk\d{8}\d+\.pdf", re.IGNORECASE)


def _http_get(url: str, headers: dict, timeout: int = 30) -> requests.Response:
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r


def _build_headers(user_agent: str) -> dict:
    return {
        "User-Agent": user_agent,
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
        "Referer": "https://www1.hkexnews.hk/app/appindex.html",
    }


def _parse_json_endpoint(data: Any, board: str, doc_types: list[str]) -> list[dict]:
    """通用 JSON 解析。HKEX 数据格式可能为 {documents:[...]} 或 [{...}]，做容错。"""
    items = []

    # HKEX 2024+ 新格式：{"app":[{...}]}，字段简写（a=name, d=date, ls=[{u1=url, nF=type}]）
    if isinstance(data, dict) and isinstance(data.get("app"), list):
        return _parse_hkex_app_format(data["app"], board, doc_types)

    candidates = []
    if isinstance(data, dict):
        # 探测常见字段
        for key in ("documents", "data", "items", "rows", "list", "result"):
            if isinstance(data.get(key), list):
                candidates = data[key]
                break
        if not candidates:
            # 也许整个 dict 就是"按公司分组"，把 values 拍平
            for v in data.values():
                if isinstance(v, list):
                    candidates.extend(v)
    elif isinstance(data, list):
        candidates = data

    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        # 抽取常见字段名
        company = (entry.get("companyName") or entry.get("company") or
                   entry.get("issuer") or entry.get("name") or "").strip()
        stock_code = (entry.get("stockCode") or entry.get("code") or "").strip()
        sponsor = (entry.get("sponsor") or "").strip()
        publish = (entry.get("publishDate") or entry.get("date") or
                   entry.get("uploadDate") or "").strip()

        # 文档列表/单文档兼容
        docs = entry.get("docs") or entry.get("files") or [entry]
        for d in docs:
            if not isinstance(d, dict):
                continue
            url = (d.get("docUrl") or d.get("url") or d.get("href") or "").strip()
            if not url:
                continue
            if url.startswith("/"):
                url = urljoin("https://www1.hkexnews.hk", url)

            doc_type = _detect_doc_type(url, d.get("type") or d.get("docType") or "")
            if doc_type not in doc_types:
                continue

            items.append({
                "source_url": url,
                "company_name": company or _company_from_filename(url),
                "stock_code": stock_code or None,
                "board": board,
                "document_type": doc_type,
                "publish_date": _normalize_date(publish or d.get("date", "")),
                "sponsor": sponsor or None,
            })
    return items


def _parse_hkex_app_format(app_list: list, board: str,
                           doc_types: list[str]) -> list[dict]:
    """解析 HKEX appactive_*.json 新格式。

    示例条目：
      {"id": 107860, "d": "03/12/2025",
       "a": "嘉和生物藥業(開曼)控股有限公司",
       "w": "sehk/2025/107860/documents/warn25120302778_c.pdf",
       "ls": [
         {"d": "03/12/2025", "nF": "聆訊後資料集（第一次呈交）",
          "u1": "sehk/2025/107860/documents/sehk25120302780_c.pdf"},
         ...
       ]}
    HKEX 的 "nF" 字段内容：聆訊後資料集 -> PHIP；申請版本 -> AP。

    去重策略：同一公司（按 entry id）同一文档类型 同时存在中文(_c.pdf)和英文版本时
    只保留中文版本，避免下游分析重复消耗 token。
    """
    items: list[dict] = []
    # 先按 (entry_id, doc_type) 分组，再选出"最佳" PDF
    by_group: dict[tuple, dict] = {}
    for entry in app_list:
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get("id")
        company = (entry.get("a") or entry.get("name") or "").strip()
        entry_date = entry.get("d", "")
        files = entry.get("ls") or []
        for f in files:
            if not isinstance(f, dict):
                continue
            url_rel = (f.get("u1") or "").strip()
            if not url_rel or not url_rel.lower().endswith(".pdf"):
                continue
            full_url = urljoin(HKEX_DOC_BASE, url_rel)
            type_label = (f.get("nF") or "") + " " + (f.get("nS1") or "")
            doc_type = _detect_hkex_doc_type(type_label, full_url)
            if doc_type not in doc_types:
                continue
            # 同一份文档可能有多次提交（_第一次呈交_/_第二次呈交_），按提交日选最新
            key = (entry_id, doc_type)
            is_chinese = url_rel.lower().endswith("_c.pdf")
            candidate = {
                "source_url": full_url,
                "company_name": company or _company_from_filename(full_url),
                "stock_code": None,
                "board": board,
                "document_type": doc_type,
                "publish_date": _normalize_date(f.get("d") or entry_date),
                "sponsor": None,
                "_is_chinese": is_chinese,
                "_submission_label": f.get("nF", ""),
            }
            existing = by_group.get(key)
            if existing is None:
                by_group[key] = candidate
                continue
            # 已有同组：优先级 中文 > 英文；同语言时按 publish_date 取新
            if candidate["_is_chinese"] and not existing["_is_chinese"]:
                by_group[key] = candidate
            elif candidate["_is_chinese"] == existing["_is_chinese"]:
                if (candidate["publish_date"] or "") > (existing["publish_date"] or ""):
                    by_group[key] = candidate
    for v in by_group.values():
        v.pop("_is_chinese", None)
        v.pop("_submission_label", None)
        items.append(v)
    return items


def _detect_hkex_doc_type(label: str, url: str) -> str:
    """根据中文 / 英文 label 判定文档类型，label 形如
    "聆訊後資料集（第一次呈交） 全文檔案" 或 "PHIP (1st submission) Full Version"。
    """
    blob = (label + " " + url).lower()
    if "聆訊後" in label or "聆讯后" in label or "phip" in blob or "post-hearing" in blob:
        return "PHIP"
    if "申請版本" in label or "申请版本" in label or "application proof" in blob:
        return "AP"
    return _detect_doc_type(url, label)


def _detect_doc_type(url: str, hint: str = "") -> str:
    blob = (url + " " + hint).lower()
    if "phip" in blob or "post-hearing" in blob:
        return "PHIP"
    if "appendix" in blob:
        return "AP_APPENDIX"
    return "AP"


def _company_from_filename(url: str) -> str:
    name = urlparse(url).path.rsplit("/", 1)[-1]
    return name.replace(".pdf", "")


def _normalize_date(s: str) -> str | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y%m%d", "%d-%m-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s  # 原样返回，后续阶段不依赖严格格式


# ---------- 策略 1: JSON 接口 ----------

def fetch_via_json(board: str, doc_types: list[str], headers: dict) -> list[dict]:
    endpoints = list(KNOWN_JSON_ENDPOINTS.get(board, []))
    # 若需要处理 AP，也加上 AP 端点
    if "AP" in doc_types:
        endpoints.extend(KNOWN_JSON_ENDPOINTS_AP.get(board, []))

    merged: dict[str, dict] = {}
    for endpoint in endpoints:
        try:
            r = _http_get(endpoint, headers)
            data = r.json()
            items = _parse_json_endpoint(data, board, doc_types)
            if items:
                logger.info("JSON 接口命中 %s，获取 %d 条", endpoint, len(items))
                for it in items:
                    merged.setdefault(it["source_url"], it)
        except (requests.RequestException, json.JSONDecodeError) as e:
            logger.debug("JSON 接口 %s 失败: %s", endpoint, e)
            continue
    return list(merged.values())


# ---------- 策略 2: HTML 解析 (www2 站点) ----------

WWW2_LIST_URLS = {
    "main": "https://www2.hkexnews.hk/New-Listings/Application-Proof-and-PHIP/Active/Main-Board?sc_lang=zh-HK",
    "gem": "https://www2.hkexnews.hk/New-Listings/Application-Proof-and-PHIP/Active/GEM?sc_lang=zh-HK",
}


def fetch_via_html(board: str, doc_types: list[str], headers: dict) -> list[dict]:
    url = WWW2_LIST_URLS.get(board)
    if not url:
        return []
    try:
        r = _http_get(url, headers)
        soup = BeautifulSoup(r.text, "lxml")
    except Exception as e:
        logger.debug("HTML 抓取 %s 失败: %s", url, e)
        return []

    items = []
    # www2 站点是 ASP.NET 风格的表格，逐行解析所有 <a href="*.pdf">
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.lower().endswith(".pdf"):
            continue
        full_url = urljoin(url, href)
        doc_type = _detect_doc_type(full_url, a.get_text())
        if doc_type not in doc_types:
            continue

        # 从同一行 / 父节点尝试提取公司名
        company = ""
        row = a.find_parent("tr") or a.find_parent("li") or a.parent
        if row:
            text = row.get_text(" ", strip=True)
            m = re.search(r"([\u4e00-\u9fffA-Za-z][^|]+?)(?:\(|\sLtd|\s有限公司|\.pdf)", text)
            if m:
                company = m.group(1).strip()

        items.append({
            "source_url": full_url,
            "company_name": company or _company_from_filename(full_url),
            "stock_code": None,
            "board": board,
            "document_type": doc_type,
            "publish_date": None,
            "sponsor": None,
        })
    if items:
        logger.info("HTML 解析获取 %d 条 (%s)", len(items), url)
    return items


# ---------- 策略 3: Playwright 动态渲染 ----------

def fetch_via_playwright(board: str, doc_types: list[str], appindex_url: str) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright 未安装，跳过动态渲染回退")
        return []

    items: list[dict] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.goto(appindex_url, wait_until="networkidle", timeout=60000)
            # 接受 warning statement（如果出现）
            for sel in ('text="ACCEPT"', 'text="接受"', 'text="同意"'):
                btn = page.query_selector(sel)
                if btn:
                    try:
                        btn.click()
                        page.wait_for_load_state("networkidle", timeout=20000)
                    except Exception:
                        pass
                    break
            html = page.content()
            browser.close()
    except Exception as e:
        logger.warning("Playwright 渲染失败: %s", e)
        return []

    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href.lower().endswith(".pdf"):
            continue
        full = urljoin(appindex_url, href)
        doc_type = _detect_doc_type(full, a.get_text())
        if doc_type not in doc_types:
            continue
        items.append({
            "source_url": full,
            "company_name": _company_from_filename(full),
            "stock_code": None,
            "board": board,
            "document_type": doc_type,
            "publish_date": None,
            "sponsor": None,
        })
    if items:
        logger.info("Playwright 渲染获取 %d 条 (%s)", len(items), appindex_url)
    return items


# ---------- 主入口 ----------

def scan_new_phips(config, db) -> list[dict]:
    boards = config.get("monitor", "boards", default=["main", "gem"])
    doc_types = config.get("monitor", "document_types", default=["PHIP"])
    use_pw = config.get("monitor", "use_playwright_fallback", default=True)
    headers = _build_headers(config.get("monitor", "user_agent",
                                        default="Mozilla/5.0"))

    all_items: dict[str, dict] = {}
    active_urls_by_board: dict[str, list[str]] = {}

    for board in boards:
        # 多策略合并去重
        items = fetch_via_json(board, doc_types, headers)
        if not items:
            items = fetch_via_html(board, doc_types, headers)
        if not items and use_pw:
            sources = config.get("monitor", "sources", default={}).get(board, [])
            if sources:
                items = fetch_via_playwright(board, doc_types, sources[0])

        for it in items:
            all_items[it["source_url"]] = it
        if items:
            active_urls_by_board[board] = [it["source_url"] for it in items]

    for board, active_urls in active_urls_by_board.items():
        inactive_count = db.sync_active_sources(active_urls, boards=[board], doc_types=doc_types)
        if inactive_count:
            logger.info(
                "同步 HKEX Active 状态：%d 个历史 PHIP 已不在当前 %s Active 列表",
                inactive_count,
                board,
            )

    new_records = []
    for url, item in all_items.items():
        is_new = db.upsert_discovered(**item)
        if is_new:
            new_records.append(item)
            logger.info("发现新 %s: %s | %s", item["document_type"],
                        item.get("company_name"), url)

    logger.info("本次扫描共发现 %d 个新 %s 条目", len(new_records),
                "/".join(doc_types))
    return new_records
