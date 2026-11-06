# DataQuery 流水线完整说明

本文说明 DataQuery 从"找到一篇 QTL 论文"到"产出可导入 LocusCompare 的 `qtl_manifest` 行"的整条流水线：每一步做什么、由谁做（程序还是 LLM）、程序强制了哪些约束、文件放在哪里、出问题时怎么看、怎么重新 build。

相关文件：

| 文件 | 内容 |
|---|---|
| `README.md` | 项目简介、LLM 服务商配置、API 一览（英文） |
| `USAGE.md` | 日常使用：安装、启动、页面操作（中文） |
| `MANIFEST_RULES.md` | manifest 各列的填写规则、表头识别、状态与 issue 的定义（中文） |
| `skills/build-manifest/SKILL.md` | **LLM 做 build 时遵循的步骤**，每次 build 都放进 system prompt（英文） |
| 本文 `docs/pipeline.md` | 整条流水线、约束、日志和排错 |

---

## 1. 总览

```mermaid
flowchart LR
  A[搜索 / 输入 DOI] --> B[读论文<br/>finder + extract]
  B --> C[AI 判定<br/>llm + rubric]
  C --> D[保存记录<br/>records.sqlite3]
  D --> E[Process data<br/>agent.py + SKILL.md]
  E --> F[候选行<br/>qtl_manifest_candidates.tsv]
  F --> G[人工审核<br/>Approve / Reject]
  G --> H[qtl_manifest.tsv]
```

| 阶段 | 页面 | 主要模块 | 谁做判断 |
|---|---|---|---|
| 1. 批量搜索论文、找数据位置 | Find QTL data | `finder.py`、`extract.py` | 程序（规则） |
| 2. 判定是否有新 QTL 数据 | Find QTL data | `llm.py`、`rubric.py`、`checks.py` | LLM 回答问题，程序按规则打分 |
| 3. 保存记录（auto saved / human saved） | Find QTL data、Saved records | `records.py` | 程序按规则自动保存，或人手动保存 |
| 4. Process data（build） | Manifest rows | `manifest.py`、`agent.py`、`rebuild.py`、`reasons.py` 等 | LLM 通过工具调查，程序检查每一步 |
| 5. 审核和导出 | Manifest rows | `manifest.py`、`web.py` | 人 |

第 1–3 步（批量搜索、AI 判定、自动保存和人工保存）见第 1b 节，第 4 步（build）从第 2 节开始。

---

## 1b. 批量搜索 → AI 判定 → 自动保存 / 人工保存

这一段决定哪些论文进入 Saved records，只有进了 Saved records 的论文才能 Process data（build）。

### 1b.1 批量搜索（Find QTL data 页面，`GET /api/search`）

1. 在 **Search literature** 输入关键词（例如 `single-cell eQTL immune cells`），可选：
   - 来源：All / Journals / bioRxiv + medRxiv；
   - 发表年份范围（This year / 2 yrs / 5 yrs / Any，或手填）；
   - 排序：Relevance / Newest first；
   - 默认只搜 QTL 相关论文（`qtl_only`）。
2. 后端向 Europe PMC 检索，**每批 50 篇**。页面底部的 "next batch" 按钮用 Europe PMC 的 cursor 读下一批，不会重读上一批；最后一批读完后按钮消失。
3. 每篇论文逐个检查（`finder.inspect`，单篇最多 90 秒），按顺序读：Europe PMC 全文 XML → Europe PMC 文本挖掘的数据编号 → bioRxiv/medRxiv API（已正式发表时连同正式版本一起读）→ 没有全文时读出版社页面。从中找出 Data availability 声明、仓库链接、数据编号（GEO、EGA、dbGaP、Zenodo、figshare、GWAS Catalog、eQTL Catalogue…）和带 QTL 标题的补充文件，每个来源按所在句子评为 high / medium / low（`extract.Source.relevance`，程序规则，不用 LLM）：

   | 评级 | 条件 |
   |---|---|
   | high | 句子讲的是 QTL 或 summary statistics，**不是**复用别人的数据，并且出现在 Data availability 里，或句子说明是"本文的、共享出来的"数据。代码仓库（GitHub 等）只有在句子明确说里面有 summary statistics 时才算 high |
   | medium | 出现在 Data availability 里但不是复用；或来自 Europe PMC 文本挖掘的编号；或讲 QTL 且说明共享。代码仓库出现在 Data availability 里也是 medium |
   | low | 其他（顺带提到的链接、复用的资源、普通代码仓库） |
4. 结果逐篇流式返回，页面边读边显示。
5. 也可以在 **DOI or URL** 里贴一篇论文的 DOI、网址、PMID 或 PMCID，单独检查（`GET /api/inspect`）。

**暂停和继续：**

| 情况 | 结果 |
|---|---|
| 点 **Stop** 或关掉页面 | 正在读的论文全部取消，不再花 LLM 额度、不再自动保存 |
| LLM 出错或额度用完 | 搜索在这篇论文处**暂停**（`paused`），之前的结果保留；进度存在浏览器里，额度恢复后点 **Resume** 从这篇接着做 |
| Europe PMC 暂时不可用 | 提示稍后点 Resume，进度保留 |

### 1b.2 AI 判定（`llm.py`、`rubric.py`）

页面勾选 **AI check for new QTL data**（默认勾选）时，每篇论文交给 LLM。**LLM 不直接给结论和置信度**，只回答三个事实问题，每个答 yes / no / unclear，并附一句论文原文：

| 问题 | 含义 |
|---|---|
| 1. `mapped_qtls` | 作者自己做了 QTL 关联分析吗？（自己的队列，或重新分析公开的基因型 + 表型数据） |
| 2. `reuse_only` | 论文是否只用了别人发表的 QTL 结果？（MR、SMR、共定位、TWAS、对已发表 QTL 做 fine-mapping…） |
| 3. `results_released` | 论文是否说明了自己的 QTL 结果在哪里获取？（包括受控访问、"on request"） |

答 yes 但没有原文引用的，一律按 unclear 处理。LLM 还要从程序找到的链接列表里**按编号**选出存放结果的链接，并标明每个链接是什么（summary statistics、results browser、补充表、原始数据…），所以它编不出不存在的网址。

程序再补两个自己知道的事实，然后按固定规则算出结论和置信度：

| 结论 | 规则 |
|---|---|
| 有新 QTL 数据（yes） | 问题 1 是 yes，且问题 2 不是 yes |
| 没有新 QTL 数据（no） | 问题 1 是 no，或问题 2 是 yes（且问题 1 不是 yes） |
| 不确定（unclear） | 其他情况，包括矛盾（1 和 2 都是 yes） |

结论为 yes 时，置信度 = 满足的标准的权重之和：

| 标准 | 权重 | 由谁判断 |
|---|---|---|
| 作者自己做了 QTL 分析 | 0.35 | LLM（问题 1 = yes，带引文） |
| 不只是复用已发表 QTL | 0.15 | LLM（问题 2 = no） |
| 论文说明了结果在哪 | 0.20 | LLM（问题 3 = yes，带引文） |
| 找到了存放 QTL 结果的下载位置 | 0.20 | 程序（选中的链接里有 summary statistics、results browser 或补充表） |
| 读到了全文 | 0.10 | 程序 |

结论为 no 时，置信度 = 0.40（问题 1 = no）+ 0.40（问题 2 = yes）+ 0.20（读到全文）；unclear 为 0。

其他规则：

- **链接内容的归一化**：LLM 有时用一句话描述链接而不是给固定的类型名，程序按顺序用关键词归类（`rubric.CONTENT_RULES`）：browser / portal / shiny / 网站 → results browser；supplement / supplementary table / additional file / Table S1 → 补充表；summary statistics / nominal / full results / association results → summary statistics；raw / fastq / bam / genotype / controlled / EGA / dbGaP / expression matrix / count → 原始数据；都不是 → other。
- **同一个链接被标了两次**时，已经标成"存放 QTL 结果"的不会被改成别的，保证置信度和自动保存看到的是同一个标签。
- **版本**：当前规则版本 `RUBRIC_VERSION = 2026-10-27`，记在每次判定里；改问题、规则或权重时要改版本号，新旧结果才能区分。
- 网站的 **How it works**（`/about`）页面显示的就是这套问题、规则和权重，和程序用的是同一份定义（`rubric.py`）。

**不重复花 token**（`checks.py`）：每次判定（结论、选中的链接、token 数）都记进数据库的 `llm_checks` 表。以后的搜索再遇到同一篇论文（按 DOI、预印本对应的正式 DOI、PMID 或标题匹配），直接复用最近一次成功的判定，不再调用 LLM；已经在 Saved records 里的论文也跳过。要重新判定，点论文上的 **Re-check with AI**（会花 token）。失败的判定会记录 token，但不会被复用。

### 1b.3 自动保存（auto saved，`records.autosave`）

勾选 **Auto-save when the AI is sure**（默认勾选）时，同时满足下面所有条件的论文自动保存，标为 `saved_by = auto`：

1. 结论是"有新 QTL 数据"；
2. 置信度 ≥ `AUTO_SAVE_MIN_CONFIDENCE`（默认 0.9）。按上面的权重，前四项标准都要满足（0.90）；只缺"读到全文"也能自动保存，所以付费墙或预印本不会拦住真正有新数据的论文；缺前四项中任何一项最多 0.85，不会自动保存；
3. 判定来自主模型，不是备用模型（`AUTO_SAVE_FALLBACK=true` 时也允许备用模型）；
4. 至少有一个选中的链接存放 QTL 结果（summary statistics、results browser 或补充表）。

满足时，把所有这类链接作为下载位置存进这篇论文的记录。不满足时不保存，页面写出原因，例如 "confidence 85% < 90% — not met: Download location for QTL results identified"、"AI unsure"、"judged by the fallback model"、"no download location with QTL results identified"。这些论文留给人看。

### 1b.4 人工保存（human saved）

| 操作 | 在哪 | 结果 |
|---|---|---|
| 点结果行的 **Save** | Find QTL data | 把这个下载位置存进论文的记录，`saved_by = human` |
| 点自动保存记录上的 **Confirm**（AI 判对了） | Saved records（Auto-saved (to check)） | `auto` 变成 `human` |
| 删掉整条记录，或只删其中一个下载位置 | Saved records | 删除 |
| 写策展备注 | Saved records / Manifest rows | 保存在 `curator_note`，build 时交给 AI |
| 人工判定：QTL data / Not QTL data / Needs recheck，附理由 | Manifest rows 的 Human decision 列 | 存在 `qtl_review`；**Not QTL data 的论文以后不再 build** |

记录的规则（`RecordStore.save`）：

- **一篇论文一条记录**。同一篇论文的所有下载位置都合并进同一条记录的 `locations`，不会出现多行；再保存同一个位置就是更新它。
- **自动保存不会覆盖人工保存**：已经是 `human` 的记录，自动保存不改动它；人工保存一条 `auto` 记录，它就变成 `human`。
- 自动保存和修改位置**不会改动人的判定**（`qtl_review`）和已有的策展备注。
- 数据库是 `data/records.sqlite3`（或 `$DATAQUERY_DB`），可以在 Saved records 页面导出 TSV / CSV（列名和 locusview 的 qtl-data-agent 审核表一致）。

### 1b.5 三个页面怎么看

| 页面 | 筛选 | 用途 |
|---|---|---|
| Find QTL data | All / Needs human check / Auto-saved / Saved by you / No new QTL data | 这次搜索的结果；Needs human check 是 AI 没把握或没自动保存的，需要人看 |
| AI-checked papers（`/checked`） | 结论：New QTL data / Unclear / No new QTL data / Check failed；是否已保存 | 所有 AI 判定过的论文（`llm_checks` 里最新的一次），可以逐个位置 Save / Remove，补存漏掉的 |
| Saved records（`/records`） | Everyone / Auto-saved (to check) / Saved by you | 已保存的记录，确认自动保存的、写备注、导出 |

### 1b.6 什么情况下做什么

| 情况 | 结果 / 该怎么做 |
|---|---|
| AI：有新数据，置信度 ≥ 0.9，主模型，找到了结果链接 | 自动保存（auto）；之后在 Saved records 里抽查确认 |
| AI：有新数据，但置信度不够（比如没找到下载位置） | 不自动保存，页面写出缺哪一项；人看论文，找到位置后手动 Save |
| AI：unclear，或问题 1 和 2 矛盾 | 不保存，归到 Needs human check |
| AI：没有新 QTL 数据（只做 MR / 共定位 / TWAS 等） | 不保存；人觉得判错了可以手动 Save |
| 判定来自备用模型 | 默认不自动保存（`AUTO_SAVE_FALLBACK` 可放开） |
| AI 判定失败或额度用完 | 搜索暂停在这篇；恢复后点 Resume |
| 这篇论文以前判定过 | 复用以前的判定，不花 token；要重新判定就点 Re-check with AI |
| 这篇论文已经在 Saved records 里 | 跳过 AI 判定 |
| 人工保存了一条 auto 记录 | 变成 human；以后自动保存不会再改它 |
| 人工判定 Not QTL data | 这篇论文以后不 build（Processing status 显示 Skipped），已下载的文件保留 |
| 非人类物种 | 默认不 build（Manifest rows 页面可以取消 "Species: human only"） |
| 论文已经在 LocusCompare 参考 manifest 里（按 DOI） | 不 build，Processing status 显示 Skipped |

保存下来的记录（不论 auto 还是 human）都会出现在 Manifest rows 页面，可以 build。

---

## 1c. LLM 每次被问什么、必须回答什么

整条流水线里 LLM 在 6 个地方被调用。每处都只让 LLM 回答事实问题或从固定选项里选，结论和是否通过由程序按规则决定；所有引文都会被程序核对。

| # | 在哪 | 什么时候 | 代码 |
|---|---|---|---|
| A | AI 判定（文本） | 搜索 / 输入 DOI 时，每篇没判定过的论文一次 | `llm.Judge._judge_text`，提示词 `llm.SYSTEM` + `rubric.QUESTIONS` |
| B | AI 判定（联网补充） | A 之后，满足条件时再一次 | `llm.Judge.research`，提示词 `llm.SYSTEM_WEB` |
| C | Build（默认） | 每篇论文 build 时，最多 80 轮 | `agent.run`，提示词 `agent.SYSTEM` + `SKILL.md` + 工具定义 |
| D | Processing conversation | Details 里给 AI 发消息时 | `web.post_manifest_chat` |
| E | 旧的规则优先流程（`MANIFEST_AGENT=0` 时） | 每篇论文一次（每批若干张表） | `manifest.SYSTEM` 和 `fetch.PLAN_SYSTEM` |
| F | 检查 key | 页面上 check key、额度恢复检查 | `Judge.ping`："Reply with the word OK." |

### A. AI 判定：这篇论文有没有新 QTL 数据

**LLM 收到的：** 标题、期刊和年份、摘要、Data availability 声明、程序找到的候选链接（编号 S1、S2…，每个带仓库、访问方式、出现位置、链接和所在句子），以及全文摘录（和数据相关的段落优先，最多 `LLM_MAX_TEXT_CHARS` 字符）。

**LLM 必须回答（只返回一个 JSON）：**

| 字段 | 问的是什么 | 可选值 | 程序怎么用 |
|---|---|---|---|
| `answers.mapped_qtls` | 作者自己做了 QTL 关联分析吗？ | yes / no / unclear，附一句原文 `quote` | 结论规则、置信度 0.35 |
| `answers.reuse_only` | 论文是否只用了别人发表的 QTL 结果（MR、SMR、共定位、TWAS…）？ | 同上 | 结论规则、置信度 0.15 |
| `answers.results_released` | 论文是否说明了自己的 QTL 结果在哪里获取（含受控访问、on request）？ | 同上 | 置信度 0.20 |
| `data_sources` | 哪些候选链接存放这篇论文自己的 QTL 结果？ | **只能填候选编号**（`S3`），每个带 `content` 和一句 `note` | 选中的链接成为下载位置；有结果类链接则置信度 +0.20 |
| `data_sources[].content` | 这个链接里是什么 | qtl_summary_statistics / qtl_results_browser / supplementary_table / raw_or_individual_data / other | 前三种算"存放 QTL 结果"，自动保存只存这三种 |
| `download_status` | 结果怎么获取 | open / controlled / on_request / not_found | 记录在保存的记录里 |
| `qtl_types` | 做了哪些 QTL | 例如 `["eQTL", "caQTL"]` | 记录，build 时提示 AI |
| `species` | 物种 | human / mouse / … | 非人类默认不 build |
| `tissues_or_cells` | 组织 / 细胞 | 短文本 | 记录，build 时提示 AI |
| `reason` | 一句话理由 | 文本 | 页面显示 |

规则：答 yes 但没有原文的算 unclear；不在候选列表里的链接一律丢掉，所以 LLM 编不出网址；LLM 不给结论和置信度，见 1b.2。

### B. AI 判定的联网补充（web research）

**什么时候做：** 开了 `LLM_WEB_SEARCH`，A 的结论不是 no，并且（没读到全文）或（A 判为有新数据但没选出任何链接）。

**LLM 收到的：** 标题、DOI / PMID / PMCID、文章网址、期刊、摘要，以及程序已经找到的链接（最多 15 个）。它可以用 web_search 工具，要求去读文章本身（出版社、PMC、Europe PMC、bioRxiv、作者页面）和它指向的数据仓库页面，引文必须来自打开过的页面。

**LLM 必须回答：** A 的三个问题（同样带原文）、`qtl_types`、`species`、`tissues_or_cells`、`reason`、`download_status`，另外：

| 字段 | 内容 | 程序怎么用 |
|---|---|---|
| `full_text_found` | 是否找到并读了全文 | 置信度的"读到全文"一项 |
| `pages_read` | 打开过的页面 | 记录 |
| `data_sources[]` | `url`、`content`、`access`（open / controlled / on_request / unknown）、`quote`（指向它的句子）、`found_on`（那句话所在页面） | **每个网址程序都用 HTTP 请求检查一遍，打不开的丢掉**；留下的加进候选链接 |

### C. Build：LLM 调查并提交 manifest 行

**LLM 收到的：** system prompt（`agent.SYSTEM`：manifest 要什么、目标格式、哪些文件能下载、服务器资源、强制的顺序、各字段的定义和证据规则）+ 完整的 `SKILL.md` + 工具定义；第一条消息里是论文信息、保存的数据位置、文件夹里已有的文件、REBUILD CHECK、以前的日志、策展人的对话和备注、论文摘要和数据声明、参考 manifest 里已有的内容。

**每次调用工具都必须回答：**

| 字段 | 问的是什么 | 检查 |
|---|---|---|
| `why` | 根据已经看到的什么，为什么现在做这一步，预期得到什么 | 少于 25 字符或空话就拒绝（第 6 节） |

**判定类工具还要回答：**

| 工具 | LLM 必须给出 | 可选值 / 要求 | 检查 |
|---|---|---|---|
| `assess_file` | `contents`：文件里是什么 | qtl_nominal_all / qtl_significant_only / qtl_lead_only / variant_map / readme / posterior_or_downstream / annotation_or_other / unreadable | 必须先 inspect；只有 QTL 关联表能判 ready / yes；variant_map 必须判 companion |
| | `processable`：能否处理成目标格式 | ready（已经是）/ yes（能处理）/ companion（给 fill_alleles 用的 variant map）/ no | |
| | `description` | 列是什么，带表头和一行例子 | |
| | `plan`（yes 时） | chrom/pos/ref/alt/variant_id/p 值/phenotype 各从哪来，缺的由哪个文件或工具补 | yes 没有 plan 就拒绝 |
| | `pval_column`（ready / yes 时） | nominal p 值列名（或用来算 p 的 −log10 p / 卡方 / z 值列） | 必须是格式检查认可的列，否则拒绝 |
| | `missing`、`reason`（no 时） | 缺哪些标准列；为什么不能用 | no 没有 reason 就拒绝 |
| | `evidence` | 从 inspect 结果、README 等逐字引用 | 逐条核对，一条都不对就拒绝 |
| `note_location` | `status` | listed / downloaded / manual / error / not_data / duplicate | |
| | `note` | manual 时必须写：卡在哪、人要做什么、核实过的申请 / 下载页面、需要人提供什么 | |
| | `evidence` | 逐字引文 | 核对 |
| `give_up` | `reason`、`evidence` | 为什么一张表都找不到 | 核对 |
| `submit` | 每行的 27 个 manifest 字段（见下）、`source_url`、`genome_build`、`note`、`scope`（full / non_full） | | 见第 2 节第 5 步 |
| | `evidence`：`biocontext`、`sample_size`、`population`、`genome_build` 各一条 `{"source": "paper/page/file", "quote": "…"}` | 原文逐字 | 引文找不到就退回；样本量必须出现在引文里、人群必须被引文支持，否则标 unverified |
| | non_full 时：`non_full_reason`、`scope_note` | 6 个原因之一；带数字说明测了多少、表里有多少 | 缺就退回 |
| | `skipped_tables[]`：`path`、`url`、`reason` | 每张找到但不用的表 | |
| | `summary` | 两三句：加载了什么、跳过了什么、人还要做什么 | |

`submit` 每行的 27 个字段：`dataset`、`source_project_id`、`use_in_lc2`（程序固定为 FALSE）、`qtl_type`、`biocontext`、`level_1_context`、`level_2_context`、`population`、`sample_size`、`file_path`、`delimiter`、`chrom_col`、`position_col`、`ref_col`、`alt_col`、`beta_col`、`se_col`、`pval_col`、`rsid_col`、`maf_col`、`variant_id_col`、`phenotype_id_col`、`gene_id`（留空）、`gene_id_mode`、`median_tpm`、`url`、`publication`。

**操作类工具**（`read_paper`、`open_url`、`list_files`、`peek_file`、`download`、`inspect_local`、`run_python`、`validate`、`fill_positions`、`fill_alleles`、`liftover`、`add_rsid`、`read_build_history`）只需要填参数（查询词、网址、文件路径、列名、代码和 `purpose`），加上 `why`。

### D. Processing conversation（Details 里的对话）

**LLM 收到的：** 论文信息、本地数据文件夹和文件列表、上次 build 的结果、各数据位置的报告、到目前为止的对话。

**要求：** 用用户的语言（通常中文）回答；帮助解读策展人贴的邮件、README、网址和本地文件，给下一次 build 具体建议；它在对话里没有浏览器、文件和下载工具，**不能说自己打开过网址、看过文件或改过 build**；要区分用户提供的原文和自己的推断。

**必须返回：** `{"reply": "…"}`。对话会保存，下次 build 时交给 C 的 LLM，由它用工具核实。

### E. 旧的规则优先流程（只在 `MANIFEST_AGENT=0` 时）

默认不走这条路。勾选 "skips the AI" 时也走规则流程，但完全不调用 LLM，E1、E2 都不会问。

**E1. 补表格里没有的事实**（`manifest.SYSTEM`）：程序已经列出文件、读了表头、对好了列，LLM 对每个单元（U1、U2…）只补文件里没有的：

| 字段 | 内容 |
|---|---|
| `qtl_type` | eQTL / sQTL / caQTL / pQTL / mQTL / 其他 |
| `holds` | all_pairs（全部检验对）/ subset（只有显著、lead、条件分析、fine-mapping）/ not_qtl / unknown |
| `tissue_or_cell`、`condition` | 组织或细胞；刺激、处理、时间点或疾病状态 |
| `level_1_context`、`level_2_context`、`biocontext` | 按参考 manifest 的词表 |
| `context_quote` | 证明这是这个 context 的逐字原文 |
| `sample_size` | 进入这个 context QTL 分析的基因分型个体数（带引文） |
| `population` | EUR / EAS / AFR / AMR / SAS / ALL（带引文） |
| `genome_build` | GRCh38 / GRCh37 / unknown（带引文） |
| `dataset` | 资源或队列名 |

**E2. 处理方案**（`fetch.PLAN_SYSTEM`）：给出每张表的列名、前几行和规则识别结果，LLM 只能从 4 种操作里选：`split`（拆列）、`neglog10_to_p`、`chisq_to_p`、`z_to_p`，或者回答 `cannot`，并给出各 manifest 字段对应的列。代码执行方案并检查结果，不合格就退回规则方案。

### F. 检查 key

只问 "Reply with the word OK."，用来确认 key 能用、额度是否恢复。

---

## 2. 一篇论文的 build：整体流程

点击 **Process data / Reprocess** 后，后端把论文放进队列（`manifest.Batch`，同时最多 2 篇）。轮到这篇时：

1. **读论文**：`manifest.read_paper` 重新取全文（Europe PMC / PMC / 出版社页面）。
2. **准备会话**：`agent.run` 建立 `Session`，打开这次 build 的日志 `data/.build_logs/<record_id>/<run_id>.md`。
3. **Rebuild 检查**（程序，LLM 开始前）：`rebuild.prepare`，见第 5 节。
4. **LLM 调查循环**：LLM 按 `SKILL.md` 的 6 个步骤调用工具，直到 `submit` 或 `give_up`。每次调用都先过 `reasons.check`（第 6 节），再执行，再记日志。
5. **检查提交**：`agent.check_submission` 逐行核对，不合格就退回让 LLM 改（最多 3 轮）。
6. **保存**：`ManifestStore.save_run` 写入数据库，并重写 `manifest/qtl_manifest_candidates.tsv`。人已经 Approve / Reject / 编辑过的行不会被覆盖。

   **只有 AI 正常结束（submit 或 give_up）才保存。** LLM 连不上、AI 停着不提交、轮数用完都没提交时，这次 build 什么都不保存，上一次 build 的行和数据位置报告原样保留，结果记为 `failed` 并写明原因（页面显示 Stopped early · Failed）。AI 额度用完时同样不保存，队列每小时检查一次，恢复后接着做这篇。

预算（环境变量可改）：

| 限制 | 默认 | 变量 |
|---|---|---|
| 每篇论文的 LLM 轮数 | 80 | `MANIFEST_AGENT_STEPS` |
| 每篇论文的 token | 4,000,000 | `MANIFEST_AGENT_TOKENS` |
| 单次 `run_python` 最长时间 | 6 小时 | `MANIFEST_RUN_MAX_SECONDS` |
| 每篇论文下载上限 | 由页面 "up to … GB per paper" 决定，上限 `MANIFEST_MAX_GB` | `MANIFEST_MAX_GB` |

用完轮数或 token 时，程序要求 LLM 立刻用已通过 `validate` 的行提交。AI 服务额度用完时（`usage_limit`），队列每小时检查一次，恢复后从这篇继续。

---

## 3. LLM 的步骤（SKILL.md 的 6 步）

`skills/build-manifest/` 是 LLM 的操作手册，改它就能改 LLM 的工作方式，不需要改代码。每次处理开始时都会读最新版本。分三部分：

| 文件 | 内容 | 什么时候给 LLM |
|---|---|---|
| `SKILL.md`（核心，约 6,300 token） | 理由和证据规则、模块表、决策表、6 个步骤的主流程、目标格式、提交清单 | 每次处理，放在 system prompt 里 |
| `modules/<name>.md`（10 个） | 特殊情况的具体做法 | 需要时：程序在对应动作之前强制要求先读，AI 也可以随时用 `read_skill` 读 |
| `papers/<record_id>.md` | 只针对某一篇论文的经验（以前处理时踩过的坑） | 只在处理那一篇时，放进第一条消息 |

**模块和"先读后做"（程序强制）：** 每个模块文件开头写明 `name`、`when`（什么时候该读）和 `required_before`（哪些动作之前必须读过）。AI 调用这些动作时，如果对应模块还没读过，程序拒绝执行，并把模块全文放在拒绝信息里；AI 读完再调用一次就能通过。读过的模块留在对话里，不会被裁剪。这样 AI 不可能没看就自己编一套做法。

| 模块 | 什么时候读 | 哪些动作之前必须读过 |
|---|---|---|
| `resume-rebuild` | 以前处理过（有 REBUILD CHECK 或历史日志） | 自动放进第一条消息 |
| `table-identity` | 判断文件里是什么、是哪种 QTL | `assess_file` |
| `processing-cookbook` | 写生成 `_processed` 表的脚本 | 写 `_processed` 的 `run_python` |
| `variant-maps` | 缺另一个等位基因或缺位置，或研究提供了 SNP info / variant map | `fill_alleles`、`fill_positions`、把文件判为 `variant_map` 的 `assess_file` |
| `liftover` | 坐标是 GRCh37 / hg19，或版本不清楚 | `liftover` |
| `add-rsid` | processed 表没有 rsID 列，或 dbSNP 里有的 rsID 没填 | `add_rsid` |
| `manifest-columns` | 填一行的列名 | `validate`、`submit` |
| `facts` | 样本量、人群、基因组版本、context 标签 | `submit` |
| `full-vs-nonfull` | 某种 QTL 没有保留全量表 | 含 `non_full` 行或 0 行的 `submit`、`give_up` |
| `access` | 某个位置需要人处理，或下载 / 网页失败 | 状态为 `manual` / `error` 的 `note_location`、`give_up` |

有测试保证每个模块写的动作名都是程序认识的（拼错会让检查失效），并且核心的模块表列出了每个模块。

处理一篇论文分 6 步（`SKILL.md` 的 Step 1–6；页面的 Processing progress 也按这 6 步显示）：

| 步骤 | 做什么 | 分支 | 主要工具 | 相关模块 |
|---|---|---|---|---|
| 1 来源和磁盘上的文件 | 查数据位置（下载链接）：需不需要下、能不能下；文件夹里是否已经有下载好的、处理好的；按文件名、表头、README 选候选表（全量的数字核对在第 4 步） | 已处理且格式合格 → 不再处理：做完第 3 步（事实）和第 4 步（判定源文件，复用的判定也算）后**直接到第 6 步**；只缺 rsID → 第 3、4 步后，第 5 步只跑 add_rsid，再到第 6 步；处理文件被删 / 只有源文件 → 第 4 步；需要申请或只能浏览器下载 → `manual`，报告给人 | `read_paper`、`open_url`、`list_files`、`peek_file`、`note_location`、`read_build_history` | `resume-rebuild`、`access`、`full-vs-nonfull` |
| 2 下载 | 只下需要、而且还不在磁盘上的；中断的下载续传 | — | `download` | — |
| 3 manifest 需要的事实 | 看 manifest 需要什么，从文章里找：QTL 类型、context、样本量、人群、基因组版本、dataset 名，每项带原文；补充表格里的事实用 `read_table` 读（它返回的行算证据） | 找不到 → 标 unknown 并写明查过哪里 | `read_paper`、`read_table` | `manifest-columns`、`facts` |
| 4 格式和计划 | 用数字核对是不是全量（和第 3 步论文里的检验数对比）；看每个文件现在是什么格式、有哪些列、能不能转成标准格式；写计划：用哪些固定程序和模块、写什么代码 | **都不能转换 → 停下汇报**（0 行提交，每张表写明原因） | `inspect_local`、`assess_file` | `table-identity`、`variant-maps`（判为 variant_map 时） |
| 5 处理 | 按计划写脚本转换，再按顺序跑固定程序：fill_positions → fill_alleles（研究 map）→ liftover → fill_alleles（dbSNP）→ add_rsid | — | `run_python`、`fill_positions`、`fill_alleles`、`liftover`、`add_rsid` | `processing-cookbook`、`variant-maps`、`liftover`、`add-rsid` |
| 6 检查并写 manifest | validate 每个处理好的文件；通过就提交 | **失败 → 回第 5 步重新处理再验证；同一个文件失败两次 → 程序不再验证，写进 skipped_tables 并报告错误**（只因 rsID 为空的失败不计次数，跑 add_rsid 即可；次数按实际文件计，换一种路径写法也算同一个） | `validate`、`submit`、`give_up` | `manifest-columns`、`facts`、`full-vs-nonfull` |

### 工具一览

| 工具 | 作用 | 程序强制的约束 |
|---|---|---|
| `read_paper` | 按关键词搜索或顺序读论文全文 | — |
| `open_url` | 打开网页 / DOI，返回文字和链接 | — |
| `list_files` | 列出仓库（Zenodo、figshare、GitHub、HF、OSF、网页）的文件 | — |
| `peek_file` | 不下载，读远程文件 / 压缩包成员 / xlsx 的表头和前几行 | — |
| `download` | 下载到 `data/<论文文件夹>/`，可续传；同一 URL 被别的论文下载过就复用 | 每篇大小上限、磁盘剩余空间 |
| `inspect_local` | 本地文件开头几行，程序附带格式检查 `format_check` | — |
| `assess_file` | 判定文件是什么、能否处理（可以用 glob 一次判定表头相同的一组文件） | 必须先 `inspect_local`；`ready/yes` 必须指明格式检查认可的 p 值列；必须带 `evidence` |
| `run_python` | 在论文文件夹里运行 LLM 写的 Python（无网络） | 禁止网络、子进程、删除、绝对路径；**所有下载文件都判定完之前，不能写 `_processed` 文件**；有 variant map 时不能自己映射等位基因 |
| `liftover` | 固定程序：UCSC chain 把 GRCh37 转成 GRCh38 | 每个文件只能转一次，必须在 `add_rsid` 之前 |
| `fill_positions` | 固定程序：只有 rsID 的表，用研究自带的 SNP map 按 rsID 补染色体和位置 | 只接受 assess 为 variant_map 的文件；rsID 在 map 里唯一对应一个位置才补；在 `fill_alleles`、`liftover` 之前 |
| `fill_alleles` | 固定程序：用研究自带的 SNP map 或 dbSNP 补非效应等位基因 | 只补双等位位点，不猜 |
| `add_rsid` | 固定程序：用本地 dbSNP 按 chrom/pos/ref/alt 补 rsID，经共用缓存 | 只写 `_processed` 表；只适用于 GRCh38；**已经补齐、之后没改过的文件自动跳过**（列在 `skipped_complete`） |
| `read_table` | 由程序读本地表格或 xlsx 的某个 sheet，可按关键词找行 | 返回的行算 AI 看过的内容，引文可以通过核对；AI 自己 `run_python` 打印的内容**不算** |
| `validate` | 按导入脚本读前 2 万行并逐列检查（第 4 节） | 不通过不能提交 |
| `note_location` | 记录一个数据位置的状态（listed / downloaded / manual / error / not_data / duplicate） | 必须带 `evidence` |
| `submit` | 提交 manifest 行和 skipped_tables | 见第 2 节第 5 步 |
| `give_up` | 完全找不到表时放弃 | 必须带 `evidence` |
| `read_build_history` | 读这篇论文以前的日志和脚本 | 只能读本论文的 |
| `read_skill` | 读一个流程模块 | 模块名写错时列出所有模块 |

**所有工具都必须带 `why`**，见第 6 节。

`SKILL.md` 里还有一节 "Curated failure checks"，来自策展人整理的问题表（`processed&needsyou.xlsx` Sheet1 第 2–61 行，2026-11-02 审阅），把以前 build 出过的错写成了检查项，例如：软件名（TensorQTL、SAIGE-QTL、sc-pcQTL）不等于 QTL 类型；GEO 上要先读 README 再判断伴随文件有没有用（Natri_2024_lung 的 SNPinfo map）；样本量、人群、基因组版本要从正确的版本和章节找；超时 / 连接重置不等于需要申请；fine-mapping、共定位等不是全量统计量。这些是线索，要对照实际来源核实，不能直接当证据引用。

---

## 4. 目标格式（程序强制）

处理后的文件是 tab 分隔的 `<原名>_processed.tsv.gz`，保留原始所有列，再追加标准列。和标准列重名的原始列改名为 `<列名>_original`。

| 列 | 格式 | 对 | 错 |
|---|---|---|---|
| `chrom` | 1–22 的数字，**不带 `chr`** | `1` | `chr1`、`01`、`X` |
| `pos` | 纯数字 | `32523` | `32523.0`、`3.2e4` |
| `ref`、`alt` | 大写 A/C/G/T/N；ref 补不出来时可以为空 | `A` | `a` |
| `variant_id` | `chr<chrom>_<pos>_<ref>_<alt>`，下划线连接，**带 `chr`**，和同一行四列一致 | `chr1_32523_A_T` | `1_32523_A_T`、`chr1:32523:A:T`、`chr1_32523_A_T_b38`、`rs123` |
| caQTL `phenotype_id`（peak） | `chr<chrom>_<start>_<end>`，下划线连接，**带 `chr`**，start < end | `chr1_817095_817594` | `chr1-817095-817594`、`chr1:817095-817594`、`1_817095_817594` |
| `rsid` | `rs<数字>`；**dbSNP 里有这个变异就必须填**，查不到才能留空 | `rs1164613250` | 空（dbSNP 里有时）、`1_702040_C_T` |
| p 值、beta、se、maf | 数字；缺失为空，不能写 `NA` 以外的占位词 | `0.023` | `not_available` |
| 基因组版本 | GRCh38（GRCh37 必须先 `liftover`） | | |

`validate`（`agent.validate_file`）的检查：

- glob 匹配到的**每个文件**都读前 2 万行，检查上表的格式（`format_problems`、`peak_problems`）；
- 前两个和最后一个文件还会检查各角色列的取值、导入后保留的比例、基因 id 是否为 Ensembl；
- rsID 和 dbSNP 对照（`rsid_problems`）：在 rsID 为空的行里抽最多 500 个变异，用和 `add_rsid` 一样的方法查本地 dbSNP。只要查到一个，`validate` 就不通过，提示先跑 `add_rsid`。本地没有 dbSNP 时跳过这项，note 里要说明。

`submit` 时，没通过 `validate` 的文件、列和 validate 时不一致、缺 `rsid_col`、GRCh37 没转换的行都会被退回。

### 参考数据（所有论文共用，LLM 不能自己下载）

| 数据 | 位置 | 用途 |
|---|---|---|
| NCBI dbSNP GRCh38 build 157 + tabix 索引 | `data/reference/dbsnp/GCF_000001405.40.gz`（或 `$DBSNP_VCF`） | `add_rsid`、`fill_alleles`、validate 的 rsID 检查 |
| rsID 缓存 | `data/reference/dbsnp/rsid_cache.sqlite`（程序自动建） | `add_rsid` 查过的每个变异（chrom、pos、ref、alt → rsID，dbSNP 没有的记为空）。同一研究的各个文件、以及以后的论文遇到同一变异直接读缓存，不再查 dbSNP（实测快约 26 倍）。删掉它只会让下次变慢，不影响结果 |
| UCSC `hg19ToHg38.over.chain.gz` | `data/reference/liftover/`（或 `$LIFTOVER_CHAIN`） | `liftover` |
| LocusCompare 参考 manifest | `$LC2_QTL_MANIFEST` | 已有的论文、dataset 名称和 level_1 context |

---

## 4b. 在什么情况下做什么（决策表）

这张表和 `SKILL.md` 开头的 "Situation → what to do" 一致。"程序强制"一列为空表示靠 LLM 按 skill 执行，程序不拦。

### 表的内容

| 情况 | 怎么做 | 程序强制 |
|---|---|---|
| 全部检验过的 variant–phenotype 对（nominal / allpairs） | 保留，需要时处理 | — |
| 只有显著 / lead / eGene / permutation 结果 | 放进 skipped_tables；按模块 `full-vs-nonfull` 找遍了也没有全量表时，才保留最完整的一个，标 `scope: non_full` | submit 要求 `non_full_reason` 和带数字的 `scope_note` |
| fine-mapping、共定位、条件分析、富集、后验结果 | skipped_tables（不是关联统计量） | — |
| QTL 类型不是 eQTL / sQTL / caQTL / pQTL（mQTL、isoQTL、apaQTL…） | skipped_tables："qtl_type not in the manifest"；不能改标成别的类型 | submit 拒绝其他类型 |
| 论文复用的资源（GTEx、eQTLGen…）、非人类样本 | skipped_tables | — |
| 基因型、表达矩阵、counts、peak 列表、协变量 | 不算表；note_location 标 `not_data` | — |
| 论文已在参考 manifest 里（按 DOI 匹配） | **不 build**：LocusCompare 已有这篇的数据，Processing status 显示 Skipped | 程序直接跳过（`manifest.already_exists`） |
| 人工标记为 `not_qtl` | 不 build | 队列跳过 |

### 缺列时怎么办

| 情况 | 怎么做 | 程序强制 |
|---|---|---|
| **没有 nominal p 值**，也没有可换算的 −log10 p、1 自由度卡方或 z 值 | **无法处理**：assess_file 判 `processable: no`，进 skipped_tables | 格式检查认可的 p 值 / z 值列都不存在时，assess_file 拒绝判 ready / yes |
| 只有 FDR / q 值 / 校正后 / permutation p | 无法处理（不是 nominal p） | 格式检查不把这些列算作 p 值 |
| 有 −log10 p 或 1 自由度卡方，没有 p | 处理时换算：p = 10^−x；p = erfc(√(x/2)) | — |
| 有 z 值，没有 p | 处理时换算双侧 p：p = erfc(\|z\| / √2)；assess_file 的 `pval_column` 填 z 列 | 格式检查列出 z 值列，assess_file 接受 |
| 只有 t 统计量，或只有 beta + se | 无法处理（t 需要自由度；beta/se 要假设正态近似） | assess_file 拒绝判 ready / yes |
| **没有 rsID**，或部分为空 | **用 `add_rsid` 补**：按 chrom/pos/ref/alt 查本地 dbSNP，dbSNP 没有的才留空 | 抽到 dbSNP 有但 rsID 为空的变异，validate 就不通过 |
| 叫 rsid / snp 的列里是 `1_123_A_G` 这种 id，不是 rs 号 | 当作 variant id 的来源（拆开），不能填成 `rsid_col`；rsID 用 add_rsid 补 | validate：rsID 只能是 `rs<数字>` 或空 |
| 只有效应等位基因，缺另一个 | 效应等位基因写进 `alt`，`ref` 留空，调用 **`fill_alleles`**：优先用研究自带的 SNP map，没有才用 dbSNP | 写 `_processed` 的 run_python 引用 map 文件会被拒绝 |
| 多等位 / 不确定的位点 | 这一行 `ref` 和 `variant_id` 留空，保留该行；不因此放弃整张表 | validate 允许 ref 为空（variant_id 也为空） |
| 只有 rsID，没有染色体 / 位置 | 有研究自带的 SNP info / variant map（README 说明、assess 为 variant_map）时，用 **`fill_positions`** 按 rsID 补 chrom / pos；map 里没有或对应多个位置的 rsID 无法定位，这些行删除并计数。没有这样的 map → 无法处理（skipped_tables 写明原因），不用 dbSNP 等其他来源 | run_python 引用 map 会被拒绝；fill_positions 只接受 assess 为 variant_map 的文件 |
| variant id 是打包的（`1:123:A:G`、`chr1_123_A_G_b38`） | 拆成 chrom / pos / ref / alt，重建 `variant_id` | validate |
| 染色体写成 `chr1` | chrom 写 `1`；variant id 保留 `chr`（`chr1_123_A_G`） | validate |
| caQTL peak 是 `chr1-100-200` / `chr1:100-200` | 改成 `chr1_100_200`，原值留在 `phenotype_id_original` | validate |
| peak 只有名字（`peak_123`） | 需要研究自带的 peak 坐标文件；不能猜 | validate |
| 有 AF / EAF，没有 MAF | 确认是 0–1 之间的频率，新增 `maf = min(af, 1 − af)` | — |
| 没有 beta / se / maf | 不填该列（导入时记为 0） | — |
| 基因名、ENST / ENSE 而不是 ENSG | 保留原 id；只用研究自带的、有文档的对应表转换；note 里说明 | validate 警告，行标 `phenotype_id_not_ensembl` |
| 没说明效应等位基因（A1 / A2） | A1 当 alt，note 里说明 | — |
| 没有表头 | 按论文 / README 的说明命名列；没有说明 → skipped_tables "no header and no column description" | — |
| X / Y / MT 行、indel | 可以保留；导入脚本只保留常染色体单碱基 SNV | — |

### 坐标和文件

| 情况 | 怎么做 | 程序强制 |
|---|---|---|
| GRCh37 / hg19 坐标 | **`liftover`**，每个文件一次，在 add_rsid 之前 | submit 拒绝 GRCh37 的行 |
| 不知道基因组版本 | 从文件名、表头、README、Methods 里找；不能假设 | 行标 `genome_build_unknown` |
| 每条染色体一个文件 | 分别处理，提交一行，file_path 用 `chr*` glob | — |
| 同一 context 有几种方法的结果 | 用论文的主要分析，其余进 skipped_tables | submit 标出重复的 qtl_type + biocontext |
| 压缩包（zip / tar） | 直接读成员，每个成员写一个 processed 文件 | — |
| Excel（xlsx） | 把工作表转成 tab 分隔的表 | — |
| 超过下载上限 | 先下主要的 context，其余进 skipped_tables "over the download limit" | download 超过上限或磁盘空间时拒绝 |

### 访问、失败和 rebuild

| 情况 | 怎么做 | 程序强制 |
|---|---|---|
| 需要登录、申请、"on request"、只能浏览器下载 | note_location `manual`，写明人要做什么、去哪个页面 | — |
| 超时、连接重置、下载不完整 | 最多重试两次；仍失败就 note_location `error` 写明错误（不能当成"需要申请"） | — |
| 出版社页面 HTTP 403 | 重试一次；再用正式发表版本、PMC / Europe PMC、仓库 API | — |
| Rebuild：processed 文件截断或格式不对 | 程序已删除，从源文件重新处理 | REBUILD CHECK |
| Rebuild：processed 文件格式对、只缺 rsID | 跑 add_rsid，不重新处理 | REBUILD CHECK、validate |
| Rebuild：下载文件和上次判定时一样 | 复用判定，不重新下载 | REBUILD CHECK |
| 任何一步 | 带 `why`；assess_file、note_location、give_up 还要带 `evidence`（逐字引文） | 不带就拒绝 |

z → p 和按 rsID 补位置（`fill_positions`）在 2026-11-04 加入，代码、skill 和本文一致。

---

## 4c. 全量还是非全量（第 1 步，模块 `full-vs-nonfull`）

manifest 要的是**全量 nominal summary statistics**：每一对检验过的 variant–phenotype。文件名叫 "summary statistics"、"allpairs"、"nominal" 都不能证明是全量，必须用数字核对（在下载的文件上用 run_python 计数）：

| 检查 | 全量 | 不是全量 |
|---|---|---|
| 表里的 phenotype 数 vs 论文 Methods 里检验的数（"N genes / proteins / peaks were tested"） | 差不多 | 少得多 |
| 同一个数 vs 论文的显著数（eGenes、pGenes…） | 更大 | 相等 → 只有显著的 phenotype |
| 每个 phenotype 的变异数 vs cis 窗口（±1 Mb 通常几千个） | 几千，接近窗口内全部 | 几个，或固定的少数 |
| p 值分布 | 0–1 都有（中位数约 0.4–0.5） | 全部低于某个阈值 → 只有显著对 |
| 文件名、README、读这个文件的脚本 | — | demo、example、test、illustrative、"not completely included" → 示例摘录 |
| 大小 | 每个 context 压缩后通常 > 100 MB | 很小 |

**找不到全量表时（模块 `full-vs-nonfull`）**，先像策展人一样重读论文，再下结论（最多约 15 次调用）：

1. 按顺序读 Data / Code availability、QTL 分析的 Methods、提到资源 / 门户 / 仓库的 Results 句子、补充数据说明；
2. 依次查：出版社页面（常比全文副本多链接）→ 论文的另一个版本（预印本 ↔ 正式发表）→ 已知仓库的新版本、README、release → 论文提到的每个编号的页面；
3. 新找到的位置照样走第 1 步（note_location、先 peek_file 再下载）。

**仍然没有全量表时：**

1. note_location，url 填 `(paper)`，status `manual`，引用论文对全量结果的说法（"available on request"、"controlled access via EGA…" 或 "no statement found"）；
2. 每个 QTL 类型 + context 保留**最完整的一张**非全量表，优先顺序：显著 phenotype 的全部变异 → 全部显著对 → 只有 lead 变异。条件分析、fine-mapping、共定位结果不保留；
3. **只能来自一个来源**：一个文件，或同一个 release 按染色体 / context 拆开的同一张表。不能把不同文件、仓库、版本、分析的行拼起来；
4. 和全量表一样下载、处理、validate，提交时 `scope: non_full`，`non_full_reason` 选一个：

   | 原因 | 表里有什么 |
   |---|---|
   | `significant_phenotypes_only` | 只有显著 phenotype（如 eGenes），但它们的变异是全的 |
   | `significant_pairs_only` | 只有通过阈值的 variant–phenotype 对 |
   | `lead_variants_only` | 每个 phenotype 只有 lead 变异 |
   | `example_subset` | 示例或截断的摘录 |
   | `some_chromosomes_or_regions` | 只有部分染色体或区域 |
   | `selected_phenotypes` | 因显著性以外的原因选出的部分 phenotype |

   `scope_note` 用一两句话、带数字说明：论文检验了多少、表里有多少、怎么知道的、全量结果在哪。例如："Holds nominal results for the 36 significant pGenes only (108,872 pairs); the paper tested 5,192 proteins (Methods). Full results: available on request (Data availability)."
5. summary 里写明人要怎么拿到全量结果（联系作者、申请、等数据发布）。

这样的行带 issue `non_full_summary_statistics`，页面 Data result 显示 Partial。

## 4d. 证据怎么核对

LLM 给的每条引文，程序都要在它**看过的内容**里找到（`manifest_rules.verify_quote`）：

- 比较时忽略大小写、空格、标点和印刷变体（各种破折号、引号、连字）；
- 词和数字必须按原顺序出现在**同一段**文本里；LLM 用 "…" 省略时，每一段都要按顺序找到；
- 字母和数字加起来少于 12 个的引文不算证据；
- "看过的内容"包括：论文全文和摘要、打开过的网页、文件列表、文件开头、`read_table` 返回的行、rebuild 检查报告、下载和处理过的文件路径。AI 自己写的 `run_python` 打印出来的内容不算（代码可以打印任何东西）。

`submit` 时对四个事实还有额外要求：

| 事实 | 通过的条件 | 不通过时 |
|---|---|---|
| biocontext | 引文找得到，且填了 level_1_context | issue `context_unverified` |
| sample_size | 引文找得到，**并且数字本身出现在引文里**（"1,267 individuals" 支持 1267，不支持 126） | `sample_size_missing` / `sample_size_unverified` |
| population | 引文找得到，**并且引文里有这个人群的词**（EUR：European、Finnish、British、white…；ALL：multi-ancestry、diverse、admixed，或同时出现几个人群） | `population_missing` / `population_unverified` |
| genome_build | 引文找得到；文件路径或表头里写了 build（`hg38`、`b37`…）时直接算核实；做过 liftover 的自动记为 GRCh38 并写明转换了多少 | `genome_build_unknown`；GRCh37 → 退回要求 liftover |

引文找不到时 submit 会退回让 LLM 改（最多 3 轮），第 3 轮以后带着 issue 保存，交给人审核。

## 4e. context 词表和命名

| 字段 | 规则 |
|---|---|
| `level_1_context` | 大类组织，从参考 manifest 已有的值里选，再加 Blood、Brain、Neural、Adipose、Muscle、Skin、Pancreas、Placenta、Cartilage、Synovium。**Blood** 包括全血、PBMC、所有免疫细胞、血浆和 LCL。用新值时标 `new_level_1_context` |
| `level_2_context` | 细胞类型、亚区或细胞类型 + 条件（`CD14_Mono`、`macrophage_IFNg`）；bulk 组织留空 |
| `biocontext` | 在这个 dataset 里区分 context 的标签：优先用文件自己的标签，否则用 level_2_context，否则用组织 |
| 格式 | 字母或数字开头，只能有字母、数字和 `_ + . -`，**不能有空格**；不符合标 `label_format` |
| 重复 | 同一个 dataset + QTL 类型里不能有两行 biocontext 相同（`duplicate_biocontext`）；不能为了避开重复而编造组织标签 |
| 不要合并 | 实际的脑区、细胞类型、条件要保留（皮层、尾状核、小脑不能合成一个 brain） |
| `dataset` | 论文用的资源或队列名（OneK1K、SingleBrain、GALA_II…），否则 `FirstAuthor_Year`；参考 manifest 已有这篇时用它的名字；同一篇论文每行一样 |
| `source_project_id` | 和 dataset 相同，除非论文给了项目编号 |

## 4f. 能下载什么、不能下载什么

| 可以 | 不可以 |
|---|---|
| 这篇论文自己的 QTL 关联表（先 peek_file 或看清 README 确认是 QTL 表） | 参考数据：dbSNP、基因注释、HGNC、Ensembl/GENCODE、基因组 FASTA、chain 文件、芯片注释（共用的 dbSNP 和 chain 由程序提供） |
| 同一研究、同一 release 的 variant map / SNP info 文件，**仅当**论文或 README 明确说明它和关联表的关系、关联表引用它的键（如 rsID）时；只用来补这些关联行的变异信息 | 基因型、表达矩阵、peak 列表、counts、协变量 |
| | 不相关的补充数据包、网页、XML、PDF（网页用 open_url 读） |
| | 论文复用的公共资源（GTEx、eQTLGen、eQTL Catalogue…，LocusCompare 已有） |
| | 只凭文件名判断的 map 文件 |

下载受每篇论文的大小上限和磁盘剩余空间限制；同一 URL 被别的论文下载过就直接复用；已经存在的文件不重复下载。`run_python` 没有网络，只能读论文文件夹里的文件；variant map 只能由 `fill_alleles` 读，处理代码里引用 map 会被拒绝。

---

## 5. Reprocess：先检查已有文件（`rebuild.py`）

页面上的 **Reprocess** 按钮在代码里叫 rebuild（`rebuild.py`、日志里的 REBUILD CHECK），是同一件事。

论文文件夹里已经有文件时，程序在 LLM 第一步之前做下面的检查，结果放在 LLM 第一条消息开头的 **REBUILD CHECK** 里。

### 5.1 已下载的文件

- 每个下载的文件由程序先跑一遍 `inspect_local` 的格式检查，LLM 不必再逐个 inspect；
- 以前 build 对**未改动**的文件（大小和修改时间都不变）做的 `assess_file` 判定，从 `data/.build_logs/<record_id>/assessments.json` 复用；改动过的文件要重新判定；
- `assess_file` 可以用 glob，一次判定表头和内容相同的一组文件（例如 `Kanai_2026_pbmc/finngen_multiome_v1.eQTL.*.cis_nominal.tsv.gz`）。

### 5.2 已处理的文件

每个 `*_processed.tsv(.gz)`：

1. **完整解压读到结尾**。中断写入（重启、超时、被杀）会留下截断的 gzip，只看开头发现不了。结果按大小和修改时间缓存在 `<论文文件夹>/.processed_checks.json`，之后的 rebuild 不再重读没变的文件。第一次读很慢：约 50 MB/s 每线程，Kanai 的 49 个文件（121 GB）大约 20–40 分钟。
2. 读前 2 万行，检查第 4 节的目标格式和 rsID。

| 结果 | 程序怎么做 | LLM 要做什么 |
|---|---|---|
| 截断、读不了、缺标准列、格式不对 | **程序直接删除**（论文文件夹里没有下载的源文件时不删，只报告） | 从源文件重新处理成目标格式；自己的 checkpoint / stats 文件里列着这些文件也不算完成 |
| 格式对，只缺 rsID | 保留 | 跑 `add_rsid`，不重新处理（重新处理也不会填 rsID） |
| 全部合格 | 保留 | 复用，提交前仍要 `validate` |

残留的 `*.tmp`（`add_rsid` / `fill_alleles` / `liftover` 写了一半的临时文件）一并删除。删除和保留的清单、原因写进日志。

---

## 6. 每一步都要有理由，判定要有证据（`reasons.py`）

LLM 不能凭空下结论。程序在执行每个工具调用之前检查：

- **`why`（所有工具）**：根据已经看到的什么（哪个工具结果、文件、论文段落），为什么现在做这一步、预期得到什么。少于 25 个字符，或者是 "next"、"continue" 这类空话，直接拒绝，不执行。
- **`evidence`（判定类工具 `assess_file`、`note_location`、`give_up`）**：从 LLM 看过的内容（论文、网页、文件列表、文件开头、工具结果、rebuild 检查报告）**逐字**复制的引文，每条带出处，至少 12 个字母或数字。程序用 `manifest_rules.verify_quote` 逐条核对（忽略大小写、空格和标点），一条都对不上就拒绝，判定不记录。
- `submit` 的每行事实（context、样本量、人群、基因组版本）仍然用原来的引文核对。

每次调用，无论接受还是拒绝，都记录两处：

1. 日志 `<run_id>.md` 里的 `Decision: <工具> (<结果>)` 一节；
2. `data/.build_logs/<record_id>/<run_id>.decisions.jsonl`，每行一个 JSON（下面是格式示例）：

```json
{"at": "2026-11-04T06:10:00+00:00", "step": 12, "by": "model", "tool": "assess_file",
 "why": "inspect_local 显示表头含 MarkerID、p.value，README 说这是全部 cis 检验对",
 "evidence": [{"quote": "Every tested variant-gene pair, tabix-indexed", "source": "README.md",
               "verified": true, "found_in": "file Kanai_2026_pbmc/README.md"}],
 "args": {"path": "Kanai_2026_pbmc/finngen_multiome_v1.eQTL.*.cis_nominal.tsv.gz", "...": "..."},
 "outcome": "ok", "result": "{\"recorded\": 33, ...}"}
```

`outcome` 是 `ok` / `refused` / `error` / `rejected`。程序自己做的决定（rebuild 删除或保留 processed 文件）也写进去，`by` 为 `program`，`why` 是程序给的原因。

---

## 7. 日志和中间文件

```
data/
├── records.sqlite3                     # 保存的记录、manifest 行、位置报告、build 结果（manifest_runs）
├── .downloads.json                     # URL → 本地文件的下载索引（所有论文共用）
├── reference/                          # dbSNP、liftover chain
├── .build_logs/<record_id>/
│   ├── <run_id>.md                     # 这次 build 的完整日志，边做边写，重启也不丢
│   ├── <run_id>.decisions.jsonl        # 每一步的理由、证据、结果
│   └── assessments.json                # assess_file 判定，供下次 build 复用
├── .agent_scripts/<record_id>/<run_id>/stepNN.py   # LLM 写的每个 Python 脚本
└── <论文文件夹>/                        # 例如 Kanai_2026_pbmc/
    ├── 下载的原始文件
    ├── <原名>_processed.tsv.gz          # 处理后的目标格式文件
    └── .processed_checks.json          # rebuild 完整读取的缓存
manifest/
├── qtl_manifest_candidates.tsv         # 所有候选行，带状态和证据
└── qtl_manifest.tsv                    # 已 Approve 的行，27 列
```

日志 `<run_id>.md` 的事件类型：

| 事件 | 内容 |
|---|---|
| `Build started` | record、标题、run_id |
| `Rebuild check of processed files` | 删除 / 保留清单 |
| `AI response` | LLM 返回的消息和累计 token |
| `Tool call: <工具>` | 参数（含 why、evidence） |
| `Decision: <工具> (<结果>)` | 理由、证据核对结果、结果摘要 |
| `Tool result: <工具>` | 工具返回 |
| `Python command` / `Python stdout / stderr` | `run_python` 的脚本和输出 |
| `Checkpoint` | 进度消息（下载进度除外） |
| `Tool interrupted: <工具>` | 工具执行中被取消 |
| `Build investigation finished` | AI 的调查结束（提交、放弃、或没提交就结束） |
| `Build stopped` | **每次处理的最后一条记录：停在哪一步、为什么停**。内容：`outcome`（submitted / gave_up / no_submission / llm_failed / usage_limit / stopped / error / interrupted）、`step` 和 `step_name`（最后一次工具调用所在的步骤，Step 1–6）、`last_tool`、`reason`（原因原文） |

日志在网页 Details 里的 **Full processing logs (.md)** 可以打开和下载，也可以用 `GET /api/manifest/<record_id>/logs` 列出。

---

## 8. 网页上怎么看 build 进度和结果

`progress.py` 读每篇论文最新的日志，`/api/manifest` 返回 `progress`。日志是逐条写的，即使服务器或机器重启，也能看出停在哪一步。

**停在哪一步、为什么停**：每次处理结束时，程序都会在日志最后写一条 `Build stopped`：
- 正常提交 → `submitted`，原因写提交了几行和 summary；放弃 → `gave_up`，原因写 give_up 的理由；
- LLM 连不上 → `llm_failed`；AI 额度用完 → `usage_limit`；AI 没提交就停了或轮数用完 → `no_submission`；
- 有人点了 Stop → `stopped`；程序出错 → `error`，写出错误信息；
- **服务器或机器重启**时来不及写，服务下次启动时程序会给所有"没有结束记录"的最新日志补写一条 `interrupted`，步骤取最后一次工具调用，原因写"服务器或机器停止，当时 xxx 正在运行"。

页面上的 Processing status 单元格和 Details → Processing progress 都直接显示这条记录：**Stopped at / Ended at：Step N · 名称 · 最后的工具**，以及 **Reason：原因原文**。2026-11-05 之前的旧日志没有这条记录，页面退回用保存的结果说明。

### Processing status（6 个值）

| 值 | 含义 |
|---|---|
| Not processed | 还没处理过 |
| Processing | 正在处理，单元格显示当前在第几步（Step 1–6）和正在调用的工具 |
| Queued | 在队列里等，显示排第几 |
| Finished | 上次处理跑完了（submit 或 give up） |
| Stopped early | 上次没跑完。小字写原因：Interrupted（日志突然结束，服务器或机器停了）、Stopped by a person、Failed、Waiting for AI quota。点 Reprocess 会接着做 |
| Skipped | 有意不处理：人工标记为非 QTL 数据，或已经在参考 manifest 里 |

单元格还显示：停在第几步、最后一次调用的工具和理由、是否做完。

### Data result（5 个值）

| 值 | 含义 |
|---|---|
| Ready | 每行都有数据，而且是全量 summary statistics（仍需人工审核行） |
| Partial | 有可用的行，但不是每行都有数据，或只有部分统计量 |
| Needs you | 需要人：访问申请、只能浏览器下载、批准更大的下载量 |
| No usable data | build 跑完了但没有可用数据，小字写原因 |
| No result yet | 没 build、正在 build，或在出结果前就停了 |

筛选栏上方的 "What the Processing status and Data result values mean" 有同样的说明，每个选项悬停也有说明。

### Details 面板

点开 Details 时，页面调用 `GET /api/manifest/{record_id}/details`（`details.py`），从磁盘上的日志、判定、处理结果算出下面的内容；论文正在处理时每 15 秒刷新一次。

1. **标题栏**：Processing progress 的状态、Full processing logs (.md) 按钮、run_id、AI 步数和 token。
2. **六步，纵向排列**，每一步一个状态：
   - **Running**：正在做这一步，显示当前在做什么；
   - **Pending**：还没到；
   - **Complete**：做完了（没用到的步骤注明 "not needed in this run"）；
   - **Failed**：停在这一步。下面写 **Stopped**（结束方式）、**Reason**（原因原文，来自日志的 `Build stopped` 记录）、**Solution**（根据原因给出的解决办法）、最后一次调用和最后的问题。
   - **适用时放在对应的步骤下**：访问申请页面和说明、Needs you（只能浏览器下载、超过下载上限等）放在它们所属的步骤（申请 / 浏览器下载 → Step 1，超过上限 → Step 2）；没有可用数据又需要人处理时，那一步直接标 Failed。其他相关问题（网络、工具……）和 **Processing conversation** 放在 Failed 的那一步下；没有 Failed 的步骤时，对话框放在页面下方。
3. **Recent steps**（折叠）：最近 8 次调用。
4. **Files（可折叠，展开状态会记住）**：论文的每个文件一行：
   - **Download**：✓ 已下载 / ✗ 下载失败 / ✗ 没下载（比如超过下载上限）；
   - **Assessed (step 4)**：判定结果和原因。README、补充表格这类非 QTL 表判为不可处理时显示 –（不需要），不算失败；只有 QTL 表转换不了才算失败；
   - **Processed (step 5)**：生成了几张 processed 表，或者"已经是标准格式"、"是 variant map"；
   - **Reached / Failed at**：走到了哪一阶段、在哪一阶段失败及原因。
   - 每个源文件下面可以再展开它生成的 processed 表，每张表的阶段：**written → liftover（只有 GRCh37 时）→ rs ids → validate → in manifest**，✓ 完成、✗ 失败、… 待做、– 不需要；鼠标悬停看原因（比如 validate 的问题和第几次）。找不到对应源文件的 processed 表单独列在后面。
5. **Manifest rows**：候选行表格，点 Check 逐行审核（同第 8b 节）。
6. 策展人备注、对话框（没有 Failed 步骤时）、Source reports（折叠）。

## 8b. 候选行的状态、issue、审核和导出

### 状态

每一行的状态由它**最严重的 issue** 决定（`manifest_rules.status_of`）：

| 状态 | 含义 | 由哪些 issue 触发 |
|---|---|---|
| `not_loadable` | 导入脚本读不了，或不是要的数据 | `not_full_summary_statistics`、`required_column_missing`、`header_not_read` |
| `needs_processing` | 数据还不能直接导入 | `columns_need_deriving`、`no_header`、`format_needs_conversion`、`peak_id_needs_reformat`、`peak_id_not_chr_start_end`、`needs_liftover`、`data_not_downloaded`、`data_too_large`、`download_failed`、`processing_failed` |
| `needs_review` | 能导入，但有需要人核对的地方 | 其他任何 issue |
| `ready` | 没有 issue | — |

### issue 一览

| 类别 | issue | 含义 |
|---|---|---|
| 范围 | `not_full_summary_statistics` | 文件是筛选过的子集，manifest 要全部检验对 |
| | `non_full_summary_statistics` | 不是全量，但找不到全量表所以保留；note 说明里面有什么 |
| | `scope_unknown` | 看不出是全量还是子集 |
| 列和格式 | `required_column_missing` | 缺导入必需的列（chrom、position、ref、alt、p 值、phenotype） |
| | `columns_need_deriving` | 必需列要先推出来（拆 variant id、−log10 p / 卡方换算） |
| | `no_header` / `header_inferred_from_sibling` / `header_not_read` / `mixed_headers` | 没有表头 / 表头借用同组文件 / 表头读不了 / 同组文件表头不一致 |
| | `format_needs_conversion` | 不是分隔文本（parquet、Excel、压缩包成员） |
| | `rsid_not_added` | 没有 rsID 列，或 dbSNP 有的 rsID 没填 |
| | `allele_order_ambiguous` | 等位基因只写 A1/A2，要确认效应等位基因 |
| | `maf_col_is_allele_frequency` | maf 列其实是等位基因频率 |
| | `effect_scale_check` | 效应列看起来不像回归系数（取值都在 0–1） |
| phenotype | `phenotype_id_not_ensembl` / `phenotype_values_not_seen` | 不是 Ensembl 基因 id / 没读到取值 |
| | `peak_id_needs_reformat` / `peak_id_not_chr_start_end` | peak id 要改成 chr_start_end / 没有坐标 |
| QTL 类型 | `qtl_type_unknown` / `qtl_type_not_in_manifest` | 不知道类型 / 不是 eQTL、sQTL、caQTL、pQTL |
| 事实和证据 | `context_unverified`、`sample_size_missing`、`sample_size_unverified`、`sample_size_study_level`、`population_missing`、`population_unverified`、`genome_build_unknown`、`paper_full_text_not_read`、`llm_facts_missing` | 见 4d |
| 坐标 | `needs_liftover` | GRCh37，要转 GRCh38 |
| 命名 | `new_level_1_context`、`label_format`、`duplicate_biocontext` | 见 4e |
| 数据 | `data_not_downloaded`、`data_too_large`、`download_failed`、`processing_failed`、`access_not_open` | 没下载 / 超过上限 / 下载失败 / 处理失败 / 不是公开下载 |
| 其他 | `publication_already_in_manifest` | 参考 manifest 已有这篇（旧流程） |

页面上每个 issue 都有一句说明（`manifest_rules.ISSUES`），Review 列的 "Not ready because" 会列出最多的三种。

### 审核

在 Manifest rows 页面点 **Review rows**，逐行看证据，然后：

| 操作 | 结果 |
|---|---|
| **Approve** | 进入导出的 `manifest/qtl_manifest.tsv`。行的状态是 not_loadable 或 needs_processing 时会先弹出确认，写明导入脚本不能直接读的原因和 issue |
| **Reject** | 不导出 |
| 编辑字段、写审核备注 | 记在 `edited`、`reviewer_note` 里 |

**Rebuild 不会覆盖人处理过的行**：已 Approve、Reject 或编辑过的行保留，只替换还在 pending 且没编辑过的行。

### 导出

| 文件 / 按钮 | 内容 |
|---|---|
| `manifest/qtl_manifest.tsv`、**Download approved (manifest TSV)** | 已 Approve 的行，27 列，按 dataset、QTL 类型、biocontext 排序，可以直接交给 LocusCompare |
| `manifest/qtl_manifest_candidates.tsv` | 所有候选行，带状态和证据（和 Download all with evidence 内容相同）；每次保存 build 结果或审核后重写，数据库才是原始记录 |
| **Download all with evidence** | 所有行，加上状态、issue、来源文件和一行证据摘要 |
| `GET /api/manifest/export?scope=approved\|ready\|all&fmt=manifest\|review` | 同上，`ready` = 已 Approve 的加上 ready 且没被 Reject 的 |

---

## 9. 日常操作

### 服务

服务由 systemd 管理（`dataqueryweb.service`，端口 8010，日志追加到 `server.log`）：

```bash
systemctl status dataqueryweb
systemctl restart dataqueryweb      # 改了 Python 代码或 SKILL 以外的后端文件后
tail -f /root/codes/dataqueryweb/server.log
curl -s 127.0.0.1:8010/api/manifest/run   # 当前队列："job": null 表示没有 build 在跑
```

- 改 `SKILL.md` 不需要重启，下次 build 自动读最新版本；
- 改前端（`src/frontend/`）通常刷新浏览器即可，重启会更新静态文件的版本号；
- **重启会中断正在跑的 build**（包括 LLM 写的长时间 `run_python`）。重启前先看 `/api/manifest/run`。被中断的 build 在页面上显示 Stopped early / Interrupted，点 Reprocess 接着做。

### Rebuild 一篇论文

1. 页面上点 **Reprocess**；
2. 如果需要给 AI 补充信息（访问邮件、本地文件路径），先在 Details 的 Processing conversation 里写，然后点 "Continue processing with this context"；
3. 等程序的 rebuild 检查（处理过的大文件第一次要完整读一遍）；
4. 在 Processing status 看进度（Step 1–6），在 Details 看每一步的理由；
5. 结束后在 Review rows 里逐行审核。

### 命令行

```bash
uv run dataqueryweb-manifest run      # 对保存的记录批量生成候选行（见 USAGE.md）
uv run pytest -q tests                # 测试
uv run ruff check src tests           # 代码风格
```

---

## 10. 配置（环境变量）

| 变量 | 作用 |
|---|---|
| `MANIFEST_AGENT` | `0` 时用旧的规则优先流程（不让 LLM 自己调查） |
| `MANIFEST_SKILL` | 指向另一份 skill 文件 |
| `MANIFEST_AGENT_STEPS` / `MANIFEST_AGENT_TOKENS` | 每篇论文的轮数 / token 上限 |
| `MANIFEST_RUN_MAX_SECONDS` | 单次 `run_python` 的最长时间 |
| `MANIFEST_MAX_GB` | 每篇论文下载量的上限 |
| `MANIFEST_AGENT_TRACE` | 把每一步打印到 stderr |
| `MANIFEST_QUOTA_CHECK_SECONDS` / `MANIFEST_QUOTA_WAITS` | AI 额度用完后多久检查一次、最多等几次 |
| `LC2_QTL_MANIFEST` | LocusCompare 参考 manifest 的路径 |
| `DBSNP_VCF` / `LIFTOVER_CHAIN` | 参考数据位置 |
| `DATAQUERY_DATA_DIR` / `DATAQUERY_DB` / `DATAQUERY_MANIFEST_DIR` | 数据目录、数据库、manifest 输出目录 |
| `OPENROUTER_API_KEY` / `GROQ_API_KEY` / `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | LLM 服务（见 README） |
| `GITHUB_TOKEN` | 列 GitHub 仓库文件时用，避免限流 |

---

## 11. 排错

| 现象 | 看哪里 | 处理 |
|---|---|---|
| 页面显示 Stopped early / Interrupted | Details → Processing progress：停在第几步、最后一次调用 | 点 Reprocess；程序会先检查已有文件，删掉写了一半的 |
| 页面显示 Stopped early · Failed，原因 "LLM step failed: …ConnectError" | Processing status 小字、`server.log`；`ss -tn \| grep <LLM 端口>` 看连接是否停在 SYN-SENT | LLM 接口连不上（`.env` 里的 `LLM_BASE_URL`）。上次的行没有被覆盖；接口恢复或换了 LLM 后点 Reprocess |
| LLM 一直被拒绝 | `decisions.jsonl` 里 `outcome: refused` 的行和 `result` | 常见原因：`why` 太短、`evidence` 引文对不上、还有文件没 `assess_file`、validate 没通过 |
| validate 报 `rsid_col … is empty for N of M sampled variants that dbSNP holds` | validate 结果 | 对该文件跑 `add_rsid`，再 validate |
| validate 报 chrom 带 `chr`、variant_id 缺 `chr`、peak 格式不对 | validate 结果里的例子 | 修处理脚本，重新处理该文件 |
| processed 文件被 rebuild 删除 | 日志 `Rebuild check of processed files`，`decisions.jsonl` 里 `by: program` 的行 | 正常：说明文件截断或格式不对，LLM 会重新处理 |
| 某个 `run_python` 跑了很久 | 日志里的 `Python stdout / stderr`（脚本应逐文件打印进度） | 超过 `MANIFEST_RUN_MAX_SECONDS` 会被终止，脚本应能从已完成的文件续跑 |
| 机器内存小（当前 3 GB） | `server.log`、`dmesg` | 处理脚本要分块读（每块不超过 20 万行），SKILL 里已有要求 |

### 例：Kanai_2026_pbmc（#639）

- 2026-11-03 09:14 开始的 build，停在处理步骤的 `run_python`（逐个处理 eQTL nominal 文件）：输出文件最后写入是 11-04 04:14，机器在 04:33 重新启动，日志没有结束事件；
- 当时 caQTL 33/33、eQTL 15/33 已处理，`eQTL.l2.CD4_Naive` 只写了一半，另外 17 个 eQTL 没开始；
- 所有已处理文件格式正确，但 rsID 列为空（处理脚本只建了空列，没有跑 `add_rsid`）；
- Rebuild 时：程序完整读取 49 个 processed 文件，删除截断的 `CD4_Naive`，其余保留并标为"缺 rsID"；LLM 处理剩下的 eQTL，对全部 processed 文件跑 `add_rsid`，再 validate 和 submit。

---

## 12. 代码结构

| 模块 | 作用 |
|---|---|
| `web.py` | FastAPI 应用：页面和所有 `/api/...` 接口 |
| `finder.py` | 搜索 Europe PMC、解析 DOI/URL/PMID、读一篇论文 |
| `extract.py` | 从全文 XML / 出版社页面里找数据声明、仓库链接和编号（纯文本解析） |
| `llm.py`、`rubric.py`、`checks.py` | AI 判定：LLM 回答固定问题，程序打分；每次判定和 token 记账 |
| `records.py` | 保存的记录（SQLite） |
| `manifest.py` | 候选 manifest 行的存储、build 队列（`Batch`）、旧的规则优先流程 |
| `manifest_rules.py` | manifest 列、表头识别、状态和 issue、引文核对 `verify_quote` |
| `probe.py` | 不下载地列出远程文件、读表头 |
| `fetch.py` | 下载、文件夹命名、旧流程的固定处理操作 |
| `agent.py` | LLM 调查循环：工具定义、`Session`、validate、提交检查 |
| `rebuild.py` | rebuild 前的检查：skill diff、下载文件、processed 文件 |
| `reasons.py` | 每一步的理由和证据检查、decisions 记账 |
| `progress.py` | 从日志读 build 进度，给网页用 |
| `dbsnp.py` | 本地 dbSNP 查询：`add_rsid`、`fill_alleles`、rsID 检查 |
| `allelemap.py` | 用研究自带的 SNP map 补等位基因（`fill_alleles`）和按 rsID 补位置（`fill_positions`） |
| `liftover.py` | GRCh37 → GRCh38 |
| `src/frontend/static/js/manifest.js` | Manifest rows 页面（状态、筛选、Details） |
| `src/frontend/static/js/manifest-status.js` | 从位置报告和行推出"需要人做什么" |

测试在 `tests/`：`test_agent.py`（工具、validate、提交）、`test_rebuild.py`、`test_reasons.py`、`test_progress.py` 等。

---

## 13. 已知局限

- 格式检查只读每个文件的前 2 万行。处理脚本对每行用同样的逻辑，所以前面对后面一般也对；完整逐行检查几亿行要好几个小时，没有做。
- rebuild 第一次完整读取大的 processed 文件很慢（每 GB 约 20 秒每线程），之后靠缓存。
- `evidence` 核对的是"引文确实出现在 LLM 看过的内容里"，不能保证 LLM 对引文的解读正确；判定结果仍需人工审核行。
- 2026-11-04 之前的 build 没有保存 `assess_file` 判定和 decisions 记录，rebuild 时这些只能从头开始。
- 只有 t 统计量或只有 beta + se 的表不能处理（2026-11-04 决定：只放开 z → p）。
- 只有 rsID、没有坐标的表，只能用研究自带的 variant map 补位置（2026-11-04 决定：不用 dbSNP 查位置）。
- rebuild 检查不预先检查 tar 压缩包（列出成员要解压整个文件），由 AI 自己 inspect。
