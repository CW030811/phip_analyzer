"""PDF 解析器：基于 PyMuPDF 抽取招股书的结构化内容

招股书通常 400-1000 页，直接全文送 LLM 不经济也容易超限。本模块负责：
1. 抽取目录（TOC）作为骨架
2. 用别名表把目录条目映射到 8 大标准章节
3. 按页面切分，给每章独立的文本块
4. 同时保留全文（兜底）和分章节（主用）两份输出
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)


@dataclass
class Section:
    """单个章节抽取结果"""
    key: str                # business / industry / ... / unknown
    title: str              # 在目录中的原始标题
    start_page: int         # 1-based
    end_page: int
    text: str = ""
    char_count: int = 0


@dataclass
class ParsedPHIP:
    pdf_path: str
    total_pages: int
    metadata: dict[str, Any] = field(default_factory=dict)
    toc: list[dict] = field(default_factory=list)        # 原始 TOC
    sections: dict[str, Section] = field(default_factory=dict)
    # 全文（截断到 max_chars）
    full_text: str = ""
    full_text_truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "pdf_path": self.pdf_path,
            "total_pages": self.total_pages,
            "metadata": self.metadata,
            "toc": self.toc,
            "sections": {k: asdict(v) for k, v in self.sections.items()},
            "full_text_len": len(self.full_text),
            "full_text_truncated": self.full_text_truncated,
        }


def _normalize(s: str) -> str:
    return re.sub(r"\s+", "", s).upper()


def _build_alias_lookup(aliases_cfg: dict) -> dict[str, str]:
    """{normalized_alias: section_key}"""
    lookup: dict[str, str] = {}
    for key, alias_list in aliases_cfg.items():
        for a in alias_list:
            lookup[_normalize(a)] = key
    return lookup


def _match_section_key(title: str, alias_lookup: dict[str, str]) -> str | None:
    norm = _normalize(title)
    # 精确匹配优先
    if norm in alias_lookup:
        return alias_lookup[norm]
    # 包含匹配（处理"行业概览-XXX 行业"这种长标题）
    for alias_norm, key in alias_lookup.items():
        if alias_norm and alias_norm in norm:
            return key
    return None


def _extract_metadata(doc: fitz.Document) -> dict:
    """招股书前几页通常有公司名、上市编号、保荐人等关键信息。"""
    meta = dict(doc.metadata or {})
    # 抽取前 3 页文本，供后续 LLM 抽取关键信息
    cover_text = ""
    for i in range(min(3, doc.page_count)):
        try:
            cover_text += doc[i].get_text("text") + "\n\n"
        except Exception:
            continue
    meta["cover_text"] = cover_text[:8000]
    return meta


def _extract_toc(doc: fitz.Document) -> list[list]:
    """fitz 返回 [[level, title, page_number], ...]，page 是 1-based"""
    toc = doc.get_toc(simple=True)
    return toc or []


def _segment_by_toc(doc: fitz.Document, toc: list[list],
                    alias_lookup: dict[str, str]) -> dict[str, Section]:
    """根据 TOC 切分章节文本"""
    sections: dict[str, Section] = {}
    if not toc:
        return sections

    # 把 TOC 转为 [(key, title, start_page)]，仅保留命中标准章节的条目
    matched: list[tuple[str, str, int]] = []
    for entry in toc:
        if len(entry) < 3:
            continue
        level, title, page = entry[0], entry[1], entry[2]
        if not title:
            continue
        key = _match_section_key(title, alias_lookup)
        if key:
            matched.append((key, title, max(1, page)))

    # 按页码排序
    matched.sort(key=lambda x: x[2])

    # 同一章节可能有多次命中（子目录），保留首次出现的页码作为 start
    seen: dict[str, tuple[str, int]] = {}
    for key, title, page in matched:
        if key not in seen:
            seen[key] = (title, page)

    # 计算每章节的 end_page：取下一个不同章节的 start - 1
    # 先把所有标准章节起始页扁平化排序
    starts = sorted(seen.values(), key=lambda x: x[1])
    page_to_next: dict[int, int] = {}
    for i, (_, start) in enumerate(starts):
        end = (starts[i + 1][1] - 1) if i + 1 < len(starts) else doc.page_count
        page_to_next[start] = end

    for key, (title, start) in seen.items():
        end = page_to_next.get(start, min(start + 30, doc.page_count))
        # 抽取该章节文本
        text_parts = []
        for p in range(start - 1, min(end, doc.page_count)):
            try:
                text_parts.append(doc[p].get_text("text"))
            except Exception:
                continue
        text = "\n\n".join(text_parts).strip()
        sections[key] = Section(
            key=key, title=title,
            start_page=start, end_page=end,
            text=text, char_count=len(text),
        )
    return sections


def _fallback_segment_by_keyword(doc: fitz.Document,
                                 alias_lookup: dict[str, str]) -> dict[str, Section]:
    """当 TOC 缺失时，扫描每页第一行，匹配章节大标题"""
    sections: dict[str, Section] = {}
    page_count = doc.page_count
    matched_starts: list[tuple[str, str, int]] = []
    for i in range(page_count):
        try:
            txt = doc[i].get_text("text")
        except Exception:
            continue
        first_lines = "\n".join(txt.strip().splitlines()[:3])
        for alias_norm, key in alias_lookup.items():
            if alias_norm and alias_norm in _normalize(first_lines):
                matched_starts.append((key, first_lines.strip().splitlines()[0][:60], i + 1))
                break

    if not matched_starts:
        return sections

    # 同上：每章只取首次命中
    seen: dict[str, tuple[str, int]] = {}
    for key, title, page in matched_starts:
        if key not in seen:
            seen[key] = (title, page)
    starts = sorted(seen.values(), key=lambda x: x[1])
    page_to_next = {}
    for i, (_, s) in enumerate(starts):
        page_to_next[s] = (starts[i + 1][1] - 1) if i + 1 < len(starts) else page_count

    for key, (title, start) in seen.items():
        end = page_to_next.get(start, min(start + 30, page_count))
        text = "\n\n".join(doc[p].get_text("text") for p in range(start - 1, end))
        sections[key] = Section(
            key=key, title=title, start_page=start, end_page=end,
            text=text, char_count=len(text),
        )
    return sections


def _extract_full_text(doc: fitz.Document, max_chars: int) -> tuple[str, bool]:
    parts = []
    total = 0
    for i in range(doc.page_count):
        try:
            t = doc[i].get_text("text")
        except Exception:
            continue
        parts.append(t)
        total += len(t)
        if total >= max_chars:
            return "".join(parts)[:max_chars], True
    return "".join(parts), False


def parse_phip(pdf_path: str | Path, config) -> ParsedPHIP:
    pdf_path = str(pdf_path)
    aliases_cfg = config.get("parser", "section_aliases", default={})
    alias_lookup = _build_alias_lookup(aliases_cfg)
    max_chars = config.get("parser", "max_chars", default=2_000_000)
    use_toc = config.get("parser", "use_toc", default=True)

    logger.info("打开 PDF: %s", pdf_path)
    doc = fitz.open(pdf_path)
    try:
        result = ParsedPHIP(pdf_path=pdf_path, total_pages=doc.page_count)
        result.metadata = _extract_metadata(doc)

        if use_toc:
            toc = _extract_toc(doc)
            result.toc = [{"level": e[0], "title": e[1], "page": e[2]} for e in toc]
            sections = _segment_by_toc(doc, toc, alias_lookup) if toc else {}
        else:
            sections = {}

        if not sections:
            logger.info("TOC 缺失或未命中，使用关键词回退切分")
            sections = _fallback_segment_by_keyword(doc, alias_lookup)
        result.sections = sections

        result.full_text, result.full_text_truncated = _extract_full_text(doc, max_chars)

        logger.info(
            "解析完成: %d 页, %d 个标准章节命中, 全文 %d 字符%s",
            result.total_pages, len(sections), len(result.full_text),
            "（已截断）" if result.full_text_truncated else "",
        )
        return result
    finally:
        doc.close()


def cache_parsed(parsed: ParsedPHIP, cache_dir: str | Path, key: str) -> Path:
    """把解析结果缓存到磁盘，便于二次运行不重复解析"""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"{key}.json"
    with open(out, "w", encoding="utf-8") as f:
        # Section 内容较大，单独写入子文件
        meta = parsed.to_dict()
        for k, v in meta["sections"].items():
            sub = cache_dir / f"{key}__{k}.txt"
            sub.write_text(v["text"], encoding="utf-8")
            v["text"] = f"<file:{sub.name}>"
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return out
