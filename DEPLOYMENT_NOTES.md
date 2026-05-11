# PHIP Analyzer 部署说明（Windows）

## 部署状态（2026-05-07 已端到端跑通）

- Python：`D:\12.2\conda\python.exe`（3.12.7，全部依赖已就绪）
- API：复用本地代理 `ANTHROPIC_BASE_URL=http://127.0.0.1:18100`、`ANTHROPIC_AUTH_TOKEN=code-switch-r`
- HKEX JSON 端点已升级为 `/ncms/json/eds/appactive_appphip_{sehk|gem}_{c|e}.json`（旧端点已 404）
- 文档相对路径基址修正为 `https://www1.hkexnews.hk/app/`
- SQLite 数据库：`data/phip_state.db`
- 日志目录：`logs/`

## 已修复的 bug

1. **HKEX 旧 JSON 接口失效**：`src/monitor.py` 改用新的 `/ncms/json/eds/appactive_*.json`，新增 `_parse_hkex_app_format()` 适配新数据结构。
2. **PDF 下载 404**：相对路径基址从根域名改为 `/app/`。
3. **EXTRACT_COVER_INFO Prompt 模板 KeyError**：JSON 示例中的 `{` / `}` 全部转义为 `{{` / `}}`。
4. **章节切分丢失**：`config.yaml` 中 `section_aliases` 补全繁体关键词（業務／行業概覽／財務資料／風險因素／未來計劃／歷史及公司架構 等），9 章命中率从 3 提升到 9。
5. **封面识别字段全为 null**：`stage_cover_info()` 拼上 summary 章节首 6000 字一起喂给抽取模型；`main._process_one()` 在仍为「未知公司」时回退到 `--company` 入参。
6. **本地代理鉴权**：`ClaudeClient.__init__` 优先使用 `ANTHROPIC_AUTH_TOKEN` 走 Bearer，没有时再退回 API Key。

## 手动运行

```
set PYTHONIOENCODING=utf-8

REM 仅扫描，不分析
python main.py scan

REM 看数据库状态
python main.py status

REM 处理至多 N 个 pending（扫描 + 下载 + 解析 + Claude 分析 + 生成 Word 报告）
python main.py run --max-items 3

REM 直接处理一个指定 PHIP
python main.py analyze --pdf-url "https://www1.hkexnews.hk/app/sehk/2025/107893/documents/sehk26050602726_c.pdf" --company "上海拓璞數控科技股份有限公司"

REM 处理本地 PDF
python main.py analyze --pdf-path "data/pdfs/xxx.pdf" --company "公司名"
```

## 端到端验证案例

- 公司：上海拓璞數控科技股份有限公司（航空航天五轴数控机床）
- PHIP 422 页 / 5.7 MB
- 9 章节均命中，9 个分析阶段全部 ok
- 用时约 8 分钟
- 报告：`data/reports/20260507_NA_上海拓璞數控科技股份有限公司_深度研报.docx`（375 段、12 表）

## 注意事项

- **本地代理依赖**：当前 `.env` 复用 Claude Code 自带本地路由 `127.0.0.1:18100`。Claude Code 退出后该端口失效，分析会失败。改用真 Key 时把 `.env` 中的 `ANTHROPIC_AUTH_TOKEN` 换成 `sk-ant-...`，并删除 `ANTHROPIC_BASE_URL`。
- **HKEX 接口若再失效**：可考虑安装 Playwright 启用动态渲染回退：`pip install playwright && playwright install chromium`。
- **未配置定时任务**：当前为手动运行模式。如需定时，可后续通过 Windows 任务计划程序调度 `python main.py run`。
