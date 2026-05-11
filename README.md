# PHIP Auto Analyzer

港交所 PHIP（聆讯后资料集）自动监控、下载与深度行业/公司分析工具。

## 功能

1. **监控**：定时扫描 HKEXnews 上的 Application Proof / PHIP 列表，识别新增项目
2. **下载**：下载招股书 PDF，存入本地
3. **解析**：用 PyMuPDF 抽取文本、目录、关键章节
4. **分析**：调用 Claude API（claude-opus-4-7）做多阶段深度分析
   - 公司业务与商业模式
   - 行业市场规模、竞争格局、监管环境
   - 财务表现、关键比率、增长驱动
   - 同业对比（自动识别 + 公开资料补充）
   - 风险因素提炼
   - 投资亮点与关注点
5. **报告**：生成 20+ 页 Word 深度研究报告

## 快速开始

```bash
# 1. 安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. 配置
cp .env.example .env
# 编辑 .env，填入 ANTHROPIC_API_KEY

# 3. 单次跑通（测试）
# 用一个已知 PHIP URL 测试
python main.py analyze --pdf-url "https://www1.hkexnews.hk/app/sehk/2024/.../sehk20241001001.pdf" \
                      --company "示例公司" --stock-code "1234"

# 或者扫一遍 HKEX 找新增项目
python main.py scan

# 4. 配置定时任务（每天 8:00 运行）
bash scripts/setup_cron.sh
```

## 目录结构

```
phip_analyzer/
├── main.py                    # CLI 入口
├── requirements.txt
├── .env.example
├── config.yaml                # 运行配置
├── src/
│   ├── config.py              # 配置加载
│   ├── db.py                  # SQLite 状态库
│   ├── monitor.py             # HKEX 抓取
│   ├── downloader.py          # PDF 下载
│   ├── pdf_parser.py          # 招股书解析
│   ├── analyzer.py            # Claude API 分析
│   ├── peer_finder.py         # 同业对比
│   ├── report_builder.py      # Word 报告生成
│   └── prompts.py             # 分析 Prompt 模板
├── prompts/                   # 可外置的 Prompt 文件
├── scripts/
│   ├── setup_cron.sh
│   └── run_once.sh
├── data/
│   ├── pdfs/                  # 下载的 PHIP
│   ├── reports/               # 生成的报告
│   └── cache/                 # 章节抽取缓存
└── logs/
```

## 配置说明

`config.yaml` 关键字段：

- `monitor.boards`: `["main", "gem"]` 监控主板/创业板
- `monitor.sources`: HKEX 的入口 URL（含主备）
- `analyzer.model`: 默认 `claude-opus-4-7`
- `analyzer.peer_model`: 同业筛选用 `claude-sonnet-4-6` 节省成本
- `report.depth`: `deep` (20+ 页含同业对比) / `standard` / `brief`

## 注意事项

- **HKEXnews 抓取**：HKEX 网站偶有改版，`monitor.py` 内置多种抓取策略（JSON 探测 → HTML 解析 → Playwright 渲染回退）。如全部失效，可手动用 `python main.py analyze --pdf-url <url>` 处理单个项目
- **PDF 大小**：PHIP 经常超过 100 页 / 32 MB，超出 Claude API 单次上限。本工具默认采用「文本抽取 → 分章送审」方式，避免直接上传 PDF
- **成本**：单个 PHIP 完整深度分析大约消耗 200K-500K tokens（Opus 输入 ~$3/M, 输出 ~$15/M），单份报告成本约 $1-3
- **法律声明**：本工具仅生成研究参考，不构成投资建议
