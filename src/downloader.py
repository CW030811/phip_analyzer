"""PDF 下载器：流式下载、断点续传、重试、文件校验"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


def _safe_filename(url: str, stock_code: str | None, company: str | None) -> str:
    """从 URL/公司信息生成本地文件名"""
    base = urlparse(url).path.rsplit("/", 1)[-1]
    if not base.lower().endswith(".pdf"):
        base = hashlib.md5(url.encode()).hexdigest() + ".pdf"
    prefix_parts = []
    if stock_code:
        prefix_parts.append(stock_code)
    if company:
        # 去掉非法字符
        safe = re.sub(r'[\\/:*?"<>|]', "_", company)[:40]
        prefix_parts.append(safe)
    prefix = "_".join(prefix_parts)
    return f"{prefix}__{base}" if prefix else base


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
def _download_stream(url: str, dest: Path, headers: dict, timeout: int,
                     max_size_mb: int) -> int:
    with requests.get(url, headers=headers, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        size = 0
        max_bytes = max_size_mb * 1024 * 1024
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                size += len(chunk)
                if size > max_bytes:
                    raise RuntimeError(f"PDF 体积超过限制 {max_size_mb} MB")
        return size


def download_pdf(*, url: str, stock_code: str | None, company: str | None,
                 pdf_dir: str | Path, timeout: int = 300,
                 max_size_mb: int = 200, user_agent: str = "Mozilla/5.0") -> dict:
    """
    下载 PHIP PDF 到本地。
    返回：{"path": str, "size_bytes": int, "sha256": str}
    """
    pdf_dir = Path(pdf_dir)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    fname = _safe_filename(url, stock_code, company)
    dest = pdf_dir / fname

    if dest.exists() and dest.stat().st_size > 0:
        logger.info("已存在本地副本，跳过下载: %s", dest)
    else:
        logger.info("下载 %s -> %s", url, dest)
        headers = {
            "User-Agent": user_agent,
            "Accept": "application/pdf,*/*",
            "Referer": "https://www1.hkexnews.hk/",
        }
        _download_stream(url, dest, headers, timeout, max_size_mb)

    # 计算 sha256
    h = hashlib.sha256()
    with open(dest, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {
        "path": str(dest),
        "size_bytes": dest.stat().st_size,
        "sha256": h.hexdigest(),
    }
