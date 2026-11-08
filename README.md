# ai-resume-assistant

AI-powered resume analyzer for job applications

# AI Resume Assistant - AI简历助手

基于 DeepSeek 的求职投递建议流水线：给一批合规来源的岗位，自动产出「值不值得投 + 为什么 + 差什么 + 怎么补 + 打招呼第一句」，人只负责最后提交。

## 架构

- **载体中立**：核心逻辑为纯函数，不含 UI / 平台依赖。
- **四步流水线**：`profile_extract`（简历字段提取）→ `jd_analysis`（岗位需求拆解）→ `company_values`（企业价值观分析）→ `apply_advice`（投递建议）。
- 每个步骤可单独运行，每步落中间产物到 `data/runs/<timestamp>/`。

## 目录

- `prompts/`：四个步骤的 prompt（从 Dify 版语义一致迁移）
- `pipeline/`：ingest / llm / schema / steps / scoring / export / feishu / batch / collect / cli
- `data/`：岗位输入 `jobs.jsonl` 与运行产物 `runs/`
- `AI简历助手.yml`：Dify 版 DSL（已冻结，仅作输出对照基线，不再修改）

## 使用

```bash
# 单次（--title 可选，不给则从 JD 首行机械提取岗位名）
python -m pipeline.cli run --resume <path> --jd <path> [--company <path>] [--title <岗位名>]

# 批量（jobs.jsonl 每行一个岗位：{"title":"岗位名","jd":"file:路径 或 纯文本","company":"..."}）
python -m pipeline.cli batch --resume <path> --jobs data/jobs.jsonl

# 导出
python -m pipeline.cli export --advice data/runs/<ts>/04_apply_advice.json --xlsx out.xlsx [--pdf out.pdf]

# 飞书写入（凭据走 .env）
python -m pipeline.cli feishu --advice data/runs/<ts>/04_apply_advice.json --app-token <t> --table-id <t>
```

## 模型与密钥

- 模型：DeepSeek 官方 API（`base_url=https://api.deepseek.com`，`model=deepseek-chat`）。
- **密钥只在本地 `.env` 填，绝不提交**（本仓库为 public）。

## 技术栈

- Python 3.10+
- DeepSeek API
- pdfplumber / rapidocr-onnxruntime（输入归一化）
- openpyxl（导出）
- Playwright（官网 careers 采集，仅公开页面）
