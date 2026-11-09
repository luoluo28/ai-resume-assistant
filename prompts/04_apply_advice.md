# apply_advice — 投递建议（五段人话 + JSON）

输入占位符：`{{PROFILE_JSON}}`、`{{JD_ANALYSIS_JSON}}`、`{{COMPANY_VALUES_JSON}}`、`{{POSITION}}`

---

# 任务：基于以下信息，产出一份投递建议

请严格按以下步骤执行，只依据给定信息，禁止编造简历内容：

步骤 1【定结论】比较简历与 JD，三选一给出结论：建议投 / 不建议投 / 先补再投。

步骤 2【找理由】给出 3 条理由。每条必须：引用 JD 原文用词（加「」）+ 引用简历原句或字段（加「」），说明匹配或不匹配。

步骤 3【找差距】列出具体差距，例如「缺一个能量化的项目」「缺团队协作证据」。禁止写「需要提升综合能力」这类空话。

步骤 4【给建议】给出本周可执行的一件具体事。

步骤 5【写招呼】写一句用这家公司语言风格的打招呼第一句（使用企业文化标签中的词）。

## 输入

岗位名（这份 JD 对应的岗位）：

{{POSITION}}

候选人画像：

{{PROFILE_JSON}}

岗位需求分析：

{{JD_ANALYSIS_JSON}}

企业价值观分析：

{{COMPANY_VALUES_JSON}}

## 规则

- `position` 必须原样照抄「岗位名」的 `{{POSITION}}`，禁止自行推断或改写。
- `scores` 只允许 technical / experience / behavioral / career 四个键，不要新增维度；地点用 `location_verdict`（PASS/FLAG/FAIL），不打分。
- 结论三选一：建议投 / 不建议投 / 先补再投。
- 每条理由必须同时引用 JD 原词与简历原句。
- 差距必须可对应到具体补救动作。
- 招呼语使用企业文化标签中的词。
- 上游缺字段时在 note 中说明，不补编内容。

## 评分（四维打分 + 地点判定）

先对以下四个维度逐维打分（每维 0–100）：

| 维度 | 依据 |
|---|---|
| technical 技术技能匹配 | 必须/优先技能 vs 简历技能 |
| experience 经验匹配 | 职能与工作内容（不看字面岗位名） |
| behavioral 行为/文化契合 | 企业文化标签 vs 简历行为证据 |
| career 职业契合与动力 | 是否贴合求职方向、是否 energize |

分数带：80-100 核心要求即主要技能；60-79 大部分匹配；40-59 部分匹配；0-39 根本错配。

## 地点判定（不打分）

- PASS：可通勤或远程
- FLAG：需商榷
- FAIL：需 relocate，一票否决（最终结论为「不建议投」）

判定理由写进 `location_note`：一句话说清"岗位在哪个城市 / 你的地点在哪 / 为什么判成这个结果"。禁止只给 PASS/FLAG/FAIL 三个字母就结束。

## 输出格式

严格输出 JSON，不要包含任何其他解释文字：

```json
{
  "position": "岗位名",
  "conclusion": "建议投|不建议投|先补再投",
  "scores": {"technical": 78, "experience": 70, "behavioral": 65, "career": 80},
  "score_reasons": {
    "technical": "一句话：依据什么给出这个分（引用 JD 要求与简历原文）",
    "experience": "一句话：同上",
    "behavioral": "一句话：同上",
    "career": "一句话：同上"
  },
  "location_verdict": "PASS|FLAG|FAIL",
  "location_note": "一句话：岗位城市与你的所在地，以及为什么这样判定",
  "reasons": ["理由1", "理由2", "理由3"],
  "gaps": ["具体差距1", "具体差距2"],
  "advice": "本周可执行的一件具体事",
  "opening_line": "打招呼第一句",
  "note": ""
}
```

补充要求：
- `score_reasons` 必须四个维度齐全，每句都要能对上 `scores` 的数字，禁止写"综合评估"这类没有依据的话。
- 如果上游 `{{PROFILE_JSON}}` 里没有地点信息，`location_note` 要写明"简历未提供地点，按未知处理"，不要编一个城市。
