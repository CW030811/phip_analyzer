"""分析编排器：调度 LLM 完成「横纵分析法」深度研究

阶段流（v2.0）：
  1. extract_cover_info       -> 公司基本信息（封面/概要）
  2. diachronic_analysis      -> 纵向分析：公司发展史叙事（招股书 历史/业务 章节）
  3. industry_landscape       -> 行业速描（横向分析开篇）
  4. business_and_financial   -> 商业模式与财务穿透
  5. peer_scenario_decision   -> 同业场景判定（A/B/C）+ 同业列表
  6. peer_deep_comparison     -> 同业深度对比（按场景展开）
  7. risk_distillation        -> 风险因素结构化提炼
  8. use_of_proceeds          -> 募集资金用途解读
  9. diagonal_synthesis       -> 横纵交汇：投资建议（精华段）

每阶段产出会缓存到 data/cache/analysis/{run_key}/，便于失败后续跑。
"""
from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from tenacity import (retry, retry_if_exception_type, stop_after_attempt,
                      wait_exponential, before_sleep_log)

from . import prompts
from .pdf_parser import ParsedPHIP, Section

logger = logging.getLogger(__name__)


# ============ 数据结构 ============

@dataclass
class AnalysisResult:
    company_name: str
    cover_info: dict = field(default_factory=dict)

    # 一、纵向分析
    diachronic_md: str = ""

    # 二、横向分析
    industry_md: str = ""
    business_financial_md: str = ""
    peers_json: dict = field(default_factory=dict)
    peer_comparison_md: str = ""

    # 风险与募投
    risks_json: dict = field(default_factory=dict)
    use_of_proceeds_md: str = ""

    # 三、横纵交汇（精华段）
    diagonal_md: str = ""

    # 元数据
    section_status: dict[str, str] = field(default_factory=dict)
    prompt_version: str = prompts.PROMPT_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


# ============ 文本清洗 ============

def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return text[:head] + "\n\n[…中间内容因长度限制省略…]\n\n" + text[-tail:]


def _strip_codeblock(text: str) -> str:
    """去掉 LLM 偶尔残留的 ```json``` 围栏"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _safe_json_parse(text: str) -> dict:
    cleaned = _strip_codeblock(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    logger.warning("JSON 解析失败，返回原始文本: %s", cleaned[:200])
    return {"_raw": cleaned}


# ============ LLM Client（多后端：Kimi / Anthropic / OpenAI）============

class LLMClient:
    """统一的 LLM 客户端接口。具体实现按 provider 选择。"""

    def complete(self, *, system: str, user: str, model: str,
                 max_tokens: int = 8000, temperature: float = 0.3) -> str:
        raise NotImplementedError


class _OpenAICompatibleClient(LLMClient):
    """OpenAI 兼容协议（Kimi / Moonshot / DeepSeek / OpenAI 官方 / 任何兼容端点）。"""

    # 部分模型对 temperature 有强制取值（如 Kimi K2.6 只支持 1.0）。
    # 若发请求被 reject，会自动回退到该 family 的强制值后重试一次。
    _FIXED_TEMPERATURE_MODELS = {
        "kimi-k2.6": 1.0,
        "kimi-k2.5": 1.0,
    }

    # Reasoning 模型：reasoning_content 会消耗大量 token，需要把 max_tokens
    # 倍增以避免输出被截断。这里的倍数会乘到调用方传入的 max_tokens 上。
    _REASONING_MODEL_MULTIPLIER = {
        "kimi-k2.6": 4.0,
        "kimi-k2.5": 3.0,
    }
    # 单次 max_tokens 硬上限（避免触发后端拒绝）
    _MAX_TOKENS_HARD_CAP = 32_000

    def __init__(self, api_key: str, base_url: str | None = None,
                 provider_name: str = "openai-compatible"):
        from openai import (OpenAI, APIError, APIStatusError, APITimeoutError,
                            APIConnectionError, RateLimitError, BadRequestError)
        self._exc_types = (APIError, APIStatusError, APITimeoutError,
                           APIConnectionError, RateLimitError)
        self._BadRequestError = BadRequestError
        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)
        self.provider_name = provider_name

    def complete(self, *, system: str, user: str, model: str,
                 max_tokens: int = 8000, temperature: float = 0.3) -> str:

        # 已知 fixed-temperature 模型直接用强制值
        forced = self._FIXED_TEMPERATURE_MODELS.get(model)
        if forced is not None:
            temperature = forced

        # Reasoning 模型给 reasoning_content 预留空间
        multiplier = self._REASONING_MODEL_MULTIPLIER.get(model, 1.0)
        if multiplier > 1.0:
            effective_max_tokens = min(int(max_tokens * multiplier),
                                       self._MAX_TOKENS_HARD_CAP)
        else:
            effective_max_tokens = min(max_tokens, self._MAX_TOKENS_HARD_CAP)

        @retry(
            stop=stop_after_attempt(4),
            wait=wait_exponential(min=4, max=60),
            retry=retry_if_exception_type(self._exc_types),
            before_sleep=before_sleep_log(logger, logging.WARNING),
        )
        def _call(temp: float):
            resp = self.client.chat.completions.create(
                model=model,
                max_tokens=effective_max_tokens,
                temperature=temp,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            usage = getattr(resp, "usage", None)
            if usage:
                logger.debug("provider=%s model=%s in=%d out=%d",
                             self.provider_name, model,
                             getattr(usage, "prompt_tokens", 0),
                             getattr(usage, "completion_tokens", 0))
            choice = resp.choices[0]
            content = (choice.message.content or "").strip()
            # 推理模型有时把内容只放在 reasoning_content（如被截断时）
            if not content:
                rc = getattr(choice.message, "reasoning_content", "") or ""
                if rc:
                    logger.warning("model=%s content 为空，回退使用 reasoning_content",
                                   model)
                    content = rc.strip()
            return content

        try:
            return _call(temperature)
        except self._BadRequestError as e:
            # 对 invalid temperature 错误做一次回退（允许只用 1.0）
            msg = str(e)
            if "temperature" in msg.lower():
                logger.warning("model=%s 拒绝 temperature=%s，回退 1.0 重试",
                               model, temperature)
                return _call(1.0)
            raise


class _AnthropicClient(LLMClient):
    """Anthropic Claude 原生 SDK。"""

    def __init__(self, api_key: str | None = None,
                 auth_token: str | None = None,
                 base_url: str | None = None):
        from anthropic import (Anthropic, APIError, APIStatusError,
                               APITimeoutError)
        self._exc_types = (APIError, APIStatusError, APITimeoutError)
        kwargs: dict = {}
        if base_url:
            kwargs["base_url"] = base_url
        if auth_token:
            kwargs["auth_token"] = auth_token
        elif api_key:
            kwargs["api_key"] = api_key
        self.client = Anthropic(**kwargs)

    def complete(self, *, system: str, user: str, model: str,
                 max_tokens: int = 8000, temperature: float = 0.3) -> str:

        @retry(
            stop=stop_after_attempt(4),
            wait=wait_exponential(min=4, max=60),
            retry=retry_if_exception_type(self._exc_types),
            before_sleep=before_sleep_log(logger, logging.WARNING),
        )
        def _call():
            resp = self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            out = []
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    out.append(block.text)
            usage = getattr(resp, "usage", None)
            if usage:
                logger.debug("provider=anthropic model=%s in=%d out=%d", model,
                             usage.input_tokens, usage.output_tokens)
            return "\n".join(out).strip()

        return _call()


def build_llm_client(config) -> LLMClient:
    """根据 config / 环境变量构造 LLM 客户端。

    优先级：环境变量 LLM_PROVIDER > config.yaml analyzer.provider > 默认 'kimi'

    Provider 取值：
      - kimi / moonshot：使用 Moonshot 平台（OpenAI 兼容）
      - openai：使用 OpenAI 官方
      - anthropic / claude：使用 Anthropic 原生 SDK 或本地代理
    """
    provider = (os.getenv("LLM_PROVIDER", "").strip().lower()
                or str(config.get("analyzer", "provider", default="kimi")).lower())

    if provider in ("kimi", "moonshot"):
        api_key = (os.getenv("KIMI_API_KEY")
                   or os.getenv("MOONSHOT_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("KIMI_API_KEY 未配置（请在 .env 中设置）")
        base_url = os.getenv("KIMI_BASE_URL", "").strip() or "https://api.moonshot.cn/v1"
        logger.info("LLM provider=Kimi (base_url=%s)", base_url)
        return _OpenAICompatibleClient(api_key=api_key, base_url=base_url,
                                       provider_name="kimi")

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY 未配置")
        base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
        logger.info("LLM provider=OpenAI")
        return _OpenAICompatibleClient(api_key=api_key, base_url=base_url,
                                       provider_name="openai")

    if provider in ("anthropic", "claude"):
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        auth_token = os.getenv("ANTHROPIC_AUTH_TOKEN", "").strip()
        base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip() or None
        if not (api_key or auth_token):
            raise RuntimeError("ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN 至少配一个")
        logger.info("LLM provider=Anthropic")
        return _AnthropicClient(api_key=api_key or None,
                                auth_token=auth_token or None,
                                base_url=base_url)

    raise RuntimeError(f"未知的 LLM provider: {provider}")


# ============ 各阶段实现 ============

# 单段输入字符上限（约 25K tokens 中文）。叙事类需要更长上下文，提到 110K。
SECTION_INPUT_CAP = 90_000
NARRATIVE_INPUT_CAP = 110_000


def stage_cover_info(client: LLMClient, parsed: ParsedPHIP, light_model: str) -> dict:
    cover = parsed.metadata.get("cover_text", "")
    summary_sec = parsed.sections.get("summary")
    summary_head = summary_sec.text[:6000] if summary_sec and summary_sec.text else ""
    history_sec = parsed.sections.get("history")
    history_head = history_sec.text[:3000] if history_sec and history_sec.text else ""
    combined = (cover + "\n\n[摘自概要章节]\n" + summary_head +
                "\n\n[摘自历史章节]\n" + history_head).strip()
    user = prompts.EXTRACT_COVER_INFO.format(cover_text=_truncate(combined, 18_000))
    text = client.complete(
        system=prompts.SYSTEM_EXTRACTOR,
        user=user, model=light_model, max_tokens=1500, temperature=0,
    )
    return _safe_json_parse(text)


def stage_diachronic(client: LLMClient, parsed: ParsedPHIP,
                     cover: dict, model: str) -> str:
    """纵向分析：发展史叙事"""
    history_sec = parsed.sections.get("history")
    business_sec = parsed.sections.get("business")
    if not history_sec or not history_sec.text.strip():
        # 如果 history 章节缺失，退而用 summary + business 头部凑
        summary_sec = parsed.sections.get("summary")
        history_text = (summary_sec.text if summary_sec else "") + "\n\n" + \
                       (business_sec.text[:20000] if business_sec else "")
    else:
        history_text = history_sec.text

    business_excerpt = (business_sec.text[:15000] if business_sec and business_sec.text
                        else "（业务章节未抽取到）")

    founders_list = cover.get("founders") or []
    if isinstance(founders_list, list):
        founders_str = "、".join(founders_list) if founders_list else "招股书未明确披露"
    else:
        founders_str = str(founders_list)

    user = prompts.DIACHRONIC_ANALYSIS.format(
        company_name=cover.get("company_name_cn") or cover.get("company_name_en") or "（未知公司）",
        industry=cover.get("industry_secondary") or cover.get("industry_primary") or "未分类",
        founding_year=cover.get("founding_year") or "招股书未明确披露",
        founders=founders_str,
        history_text=_truncate(history_text, NARRATIVE_INPUT_CAP),
        business_text_excerpt=_truncate(business_excerpt, 15_000),
    )
    return client.complete(system=prompts.SYSTEM_ANALYST, user=user,
                           model=model, max_tokens=10_000, temperature=0.4)


def stage_industry(client: LLMClient, parsed: ParsedPHIP,
                   cover: dict, model: str) -> str:
    sec = parsed.sections.get("industry")
    if not sec or not sec.text.strip():
        return "_行业章节未抽取到内容_"
    user = prompts.INDUSTRY_LANDSCAPE.format(
        company_name=cover.get("company_name_cn") or "（未知公司）",
        industry=cover.get("industry_secondary") or cover.get("industry_primary") or "未分类",
        industry_text=_truncate(sec.text, SECTION_INPUT_CAP),
    )
    return client.complete(system=prompts.SYSTEM_ANALYST, user=user,
                           model=model, max_tokens=6000, temperature=0.3)


def stage_business_financial(client: LLMClient, parsed: ParsedPHIP,
                             cover: dict, model: str) -> str:
    """合并的商业模式与财务穿透分析"""
    business_sec = parsed.sections.get("business")
    financial_sec = parsed.sections.get("financial")
    business_text = (business_sec.text if business_sec and business_sec.text
                     else "（业务章节未抽取到）")
    financial_text = (financial_sec.text if financial_sec and financial_sec.text
                      else "（财务章节未抽取到）")

    user = prompts.BUSINESS_AND_FINANCIAL.format(
        company_name=cover.get("company_name_cn") or "（未知公司）",
        industry=cover.get("industry_secondary") or cover.get("industry_primary") or "未分类",
        business_text=_truncate(business_text, 70_000),
        financial_text=_truncate(financial_text, 70_000),
    )
    return client.complete(system=prompts.SYSTEM_ANALYST, user=user,
                           model=model, max_tokens=8000, temperature=0.3)


def stage_peers_decision(client: LLMClient, cover: dict,
                         business_md: str, light_model: str) -> dict:
    """场景判断 + 可比公司识别"""
    user = prompts.PEER_SCENARIO_DECISION.format(
        company_name=cover.get("company_name_cn") or "（未知公司）",
        industry_primary=cover.get("industry_primary") or "",
        industry_secondary=cover.get("industry_secondary") or "",
        business_summary=_truncate(business_md, 4000),
    )
    text = client.complete(system=prompts.SYSTEM_EXTRACTOR, user=user,
                           model=light_model, max_tokens=2500, temperature=0.2)
    return _safe_json_parse(text)


def stage_peer_comparison(client: LLMClient, cover: dict,
                          peers_data: dict, business_md: str,
                          financial_md: str, model: str) -> str:
    """同业深度对比（按场景 A/B/C 展开）"""
    scenario = peers_data.get("scenario", "C")
    rationale = peers_data.get("scenario_rationale", "")
    peer_list = peers_data.get("peers", [])

    user = prompts.PEER_DEEP_COMPARISON.format(
        target_company=cover.get("company_name_cn") or "目标公司",
        scenario=scenario,
        scenario_rationale=rationale,
        peers_json=json.dumps(peer_list, ensure_ascii=False, indent=2),
        business_summary=_truncate(business_md, 5000),
        financial_summary=_truncate(financial_md, 5000),
    )
    return client.complete(system=prompts.SYSTEM_ANALYST, user=user,
                           model=model, max_tokens=10_000, temperature=0.4)


def stage_risks(client: LLMClient, parsed: ParsedPHIP,
                cover: dict, model: str) -> dict:
    sec = parsed.sections.get("risk")
    if not sec or not sec.text.strip():
        return {"risks": [], "_note": "风险章节未抽取到内容"}
    user = prompts.RISK_DISTILLATION.format(
        company_name=cover.get("company_name_cn") or "（未知公司）",
        risk_text=_truncate(sec.text, SECTION_INPUT_CAP),
    )
    text = client.complete(system=prompts.SYSTEM_EXTRACTOR, user=user,
                           model=model, max_tokens=4000, temperature=0.1)
    return _safe_json_parse(text)


def stage_use_of_proceeds(client: LLMClient, parsed: ParsedPHIP,
                          cover: dict, model: str) -> str:
    sec = parsed.sections.get("use_of_proceeds")
    if not sec or not sec.text.strip():
        return "_募集资金用途章节未抽取到内容_"
    user = prompts.USE_OF_PROCEEDS.format(
        company_name=cover.get("company_name_cn") or "（未知公司）",
        uop_text=_truncate(sec.text, 60_000),
    )
    return client.complete(system=prompts.SYSTEM_ANALYST, user=user,
                           model=model, max_tokens=4000, temperature=0.3)


def stage_diagonal(client: LLMClient, *, cover: dict,
                   diachronic_md: str, industry_md: str,
                   peer_comparison_md: str, business_financial_md: str,
                   risks_json: dict, uop_md: str, model: str) -> str:
    """横纵交汇：投资建议（精华段）"""
    risks_summary = "\n".join(
        f"- {r.get('title', '')}: {r.get('description', '')}"
        for r in risks_json.get("risks", [])[:8]
    ) or "（风险数据缺失）"

    user = prompts.DIAGONAL_SYNTHESIS.format(
        company_name=cover.get("company_name_cn") or "（未知公司）",
        industry=cover.get("industry_secondary") or cover.get("industry_primary") or "未分类",
        diachronic_summary=_truncate(diachronic_md, 8000),
        industry_summary=_truncate(industry_md, 5000),
        peer_comparison_summary=_truncate(peer_comparison_md, 8000),
        business_financial_summary=_truncate(business_financial_md, 8000),
        risk_summary=risks_summary,
        uop_summary=_truncate(uop_md, 3000),
    )
    return client.complete(system=prompts.SYSTEM_ANALYST, user=user,
                           model=model, max_tokens=8000, temperature=0.5)


# ============ 主编排 ============

def run_analysis(parsed: ParsedPHIP, *, config, cache_dir: Path | None = None,
                 run_key: str | None = None) -> AnalysisResult:
    main_model = config.get("analyzer", "model",
                            default="kimi-k2-0711-preview")
    light_model = config.get("analyzer", "light_model",
                             default="moonshot-v1-32k")
    concurrency = max(1, config.get("analyzer", "concurrency", default=3))

    client = build_llm_client(config)
    result = AnalysisResult(company_name="")

    cache_path = None
    if cache_dir and run_key:
        cache_path = Path(cache_dir) / run_key
        cache_path.mkdir(parents=True, exist_ok=True)

    def cache_save(name: str, content: Any) -> None:
        if not cache_path:
            return
        f = (cache_path / f"{name}.md") if isinstance(content, str) \
            else (cache_path / f"{name}.json")
        if isinstance(content, str):
            f.write_text(content, encoding="utf-8")
        else:
            f.write_text(json.dumps(content, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    def cache_load(name: str, *, as_json: bool = False) -> Any | None:
        if not cache_path:
            return None
        f = cache_path / f"{name}.{'json' if as_json else 'md'}"
        if not f.exists() or f.stat().st_size == 0:
            return None
        try:
            if as_json:
                return json.loads(f.read_text(encoding="utf-8"))
            return f.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("analysis cache load failed for %s: %s", name, e)
            return None

    logger.info("[1/9] extract cover info (%s)...", light_model)
    try:
        cached_cover = cache_load("cover", as_json=True)
        result.cover_info = cached_cover or stage_cover_info(client, parsed, light_model)
        result.company_name = (result.cover_info.get("company_name_cn") or
                               result.cover_info.get("company_name_en") or "unknown company")
        result.section_status["cover"] = "ok(cache)" if cached_cover else "ok"
        if not cached_cover:
            cache_save("cover", result.cover_info)
    except Exception as e:
        logger.exception("cover info extraction failed")
        result.section_status["cover"] = f"failed: {e}"

    cover = result.cover_info

    parallel_tasks = [
        ("diachronic", stage_diachronic),
        ("industry", stage_industry),
        ("business_financial", stage_business_financial),
        ("risks", stage_risks),
        ("use_of_proceeds", stage_use_of_proceeds),
    ]

    logger.info("[2-6/9] run parallel analysis stages (concurrency=%d, model=%s)...",
                concurrency, main_model)
    parallel_results: dict[str, Any] = {}
    for name, _ in parallel_tasks:
        cached = cache_load(name, as_json=(name == "risks"))
        if cached is not None:
            parallel_results[name] = cached
            result.section_status[name] = "ok(cache)"

    missing_parallel = [(name, fn) for name, fn in parallel_tasks
                        if name not in parallel_results]
    if missing_parallel:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futures = {
                ex.submit(fn, client, parsed, cover, main_model): name
                for name, fn in missing_parallel
            }
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    parallel_results[name] = fut.result()
                    result.section_status[name] = "ok"
                    size = len(parallel_results[name]) if isinstance(parallel_results[name], str) \
                        else len(json.dumps(parallel_results[name], ensure_ascii=False))
                    logger.info("  %s completed (%d chars)", name, size)
                    cache_save(name, parallel_results[name])
                except Exception as e:
                    logger.exception("stage %s failed", name)
                    if name in ("risks",):
                        parallel_results[name] = {"risks": [], "_note": f"failed: {e}"}
                    else:
                        parallel_results[name] = f"_{name} stage failed: {e}_"
                    result.section_status[name] = f"failed: {e}"

    result.diachronic_md = parallel_results.get("diachronic", "")
    result.industry_md = parallel_results.get("industry", "")
    result.business_financial_md = parallel_results.get("business_financial", "")
    result.risks_json = parallel_results.get("risks", {})
    result.use_of_proceeds_md = parallel_results.get("use_of_proceeds", "")

    logger.info("[7/9] peer scenario decision (%s)...", light_model)
    try:
        cached_peers = cache_load("peers", as_json=True)
        result.peers_json = cached_peers or stage_peers_decision(
            client, cover, result.business_financial_md, light_model)
        result.section_status["peers"] = "ok(cache)" if cached_peers else "ok"
        if not cached_peers:
            cache_save("peers", result.peers_json)
    except Exception as e:
        logger.exception("peer scenario decision failed")
        result.section_status["peers"] = f"failed: {e}"
        result.peers_json = {"scenario": "C", "peers": [], "_note": str(e)}

    logger.info("[8/9] peer comparison (%s, scenario=%s)...",
                main_model, result.peers_json.get("scenario", "?"))
    try:
        cached_peer_comparison = cache_load("peer_comparison")
        result.peer_comparison_md = cached_peer_comparison or stage_peer_comparison(
            client, cover, result.peers_json,
            result.business_financial_md, result.business_financial_md, main_model)
        result.section_status["peer_comparison"] = (
            "ok(cache)" if cached_peer_comparison else "ok"
        )
        if not cached_peer_comparison:
            cache_save("peer_comparison", result.peer_comparison_md)
    except Exception as e:
        logger.exception("peer comparison failed")
        result.section_status["peer_comparison"] = f"failed: {e}"

    logger.info("[9/9] diagonal synthesis (%s)...", main_model)
    try:
        cached_diagonal = cache_load("diagonal")
        result.diagonal_md = cached_diagonal or stage_diagonal(
            client, cover=cover,
            diachronic_md=result.diachronic_md,
            industry_md=result.industry_md,
            peer_comparison_md=result.peer_comparison_md,
            business_financial_md=result.business_financial_md,
            risks_json=result.risks_json,
            uop_md=result.use_of_proceeds_md,
            model=main_model,
        )
        result.section_status["diagonal"] = "ok(cache)" if cached_diagonal else "ok"
        if not cached_diagonal:
            cache_save("diagonal", result.diagonal_md)
    except Exception as e:
        logger.exception("diagonal synthesis failed")
        result.section_status["diagonal"] = f"failed: {e}"

    logger.info("analysis status: %s", result.section_status)
    return result

