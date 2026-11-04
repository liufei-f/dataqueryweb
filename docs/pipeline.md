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
  D --> E[Build manifest<br/>agent.py + SKILL.md]
  E --> F[候选行<br/>qtl_manifest_candidates.tsv]
  F --> G[人工审核<br/>Approve / Reject]
  G --> H[qtl_manifest.tsv]
```

| 阶段 | 页面 | 主要模块 | 谁做判断 |
|---|---|---|---|
| 1. 找论文、找数据位置 | Find QTL data | `finder.py`、`extract.py` | 程序（规则） |
| 2. 判定是否有新 QTL 数据 | Find QTL data | `llm.py`、`rubric.py`、`checks.py` | LLM 回答问题，程序按规则打分 |
| 3. 保存记录 | Saved records | `records.py` | 人，或高置信度时自动保存 |
| 4. Build manifest | Manifest rows | `manifest.py`、`agent.py`、`rebuild.py`、`reasons.py` 等 | LLM 通过工具调查，程序检查每一步 |
| 5. 审核和导出 | Manifest rows | `manifest.py`、`web.py` | 人 |

本文重点是第 4 步（build），前 3 步见 `README.md` 和 `USAGE.md`。

---

## 2. 一篇论文的 build：整体流程

点击 **Build manifest / Rebuild** 后，后端把论文放进队列（`manifest.Batch`，同时最多 2 篇）。轮到这篇时：

1. **读论文**：`manifest.read_paper` 重新取全文（Europe PMC / PMC / 出版社页面）。
2. **准备会话**：`agent.run` 建立 `Session`，打开这次 build 的日志 `data/.build_logs/<record_id>/<run_id>.md`。
3. **Rebuild 检查**（程序，LLM 开始前）：`rebuild.prepare`，见第 5 节。
4. **LLM 调查循环**：LLM 按 `SKILL.md` 的 Phase 0–7 调用工具，直到 `submit` 或 `give_up`。每次调用都先过 `reasons.check`（第 6 节），再执行，再记日志。
5. **检查提交**：`agent.check_submission` 逐行核对，不合格就退回让 LLM 改（最多 3 轮）。
6. **保存**：`ManifestStore.save_run` 写入数据库，并重写 `manifest/qtl_manifest_candidates.tsv`。人已经 Approve / Reject / 编辑过的行不会被覆盖。

预算（环境变量可改）：

| 限制 | 默认 | 变量 |
|---|---|---|
| 每篇论文的 LLM 轮数 | 80 | `MANIFEST_AGENT_STEPS` |
| 每篇论文的 token | 4,000,000 | `MANIFEST_AGENT_TOKENS` |
| 单次 `run_python` 最长时间 | 6 小时 | `MANIFEST_RUN_MAX_SECONDS` |
| 每篇论文下载上限 | 由页面 "up to … GB per paper" 决定，上限 `MANIFEST_MAX_GB` | `MANIFEST_MAX_GB` |

用完轮数或 token 时，程序要求 LLM 立刻用已通过 `validate` 的行提交。AI 服务额度用完时（`usage_limit`），队列每小时检查一次，恢复后从这篇继续。

---

## 3. LLM 的步骤（SKILL.md 的 Phase）

`skills/build-manifest/SKILL.md` 是 LLM 的操作手册，改它就能改 LLM 的工作方式，不需要改代码。每次 build 开始时都会读最新版本。

| Phase | 做什么 | 主要工具 |
|---|---|---|
| 0 | 续跑检查：读 rebuild 检查结果、以前的日志和脚本，定续跑计划 | `read_build_history`、`note_location` |
| 1 | 读论文：样本、人群、组织/细胞、基因组版本、数据声明 | `read_paper` |
| 2 | 列出所有数据位置 | `open_url`、`note_location` |
| 3 | 选择全量 nominal summary statistics 表（3b：找不到全量时才考虑非全量表） | `list_files`、`peek_file` |
| 4 | 下载 | `download` |
| 4b | 检查每个下载的文件，判断内容和能否处理 | `inspect_local`、`assess_file` |
| 5 | 处理成目标格式，写 `<原名>_processed.tsv.gz` | `run_python` |
| 5a | GRCh37 → GRCh38；只有效应等位基因时补另一个等位基因 | `liftover`、`fill_alleles` |
| 5b | 补 rsID | `add_rsid` |
| 6 | 按导入脚本的方式验证 | `validate` |
| 7 | 提交 manifest 行（或放弃） | `submit`、`give_up` |

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
| `fill_alleles` | 固定程序：用研究自带的 SNP map 或 dbSNP 补非效应等位基因 | 只补双等位位点，不猜 |
| `add_rsid` | 固定程序：用本地 dbSNP 按 chrom/pos/ref/alt 补 rsID | 只写 `_processed` 表；只适用于 GRCh38 |
| `validate` | 按导入脚本读前 2 万行并逐列检查（第 4 节） | 不通过不能提交 |
| `note_location` | 记录一个数据位置的状态（listed / downloaded / manual / error / not_data / duplicate） | 必须带 `evidence` |
| `submit` | 提交 manifest 行和 skipped_tables | 见第 2 节第 5 步 |
| `give_up` | 完全找不到表时放弃 | 必须带 `evidence` |
| `read_build_history` | 读这篇论文以前的日志和脚本 | 只能读本论文的 |

**所有工具都必须带 `why`**，见第 6 节。

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
| UCSC `hg19ToHg38.over.chain.gz` | `data/reference/liftover/`（或 `$LIFTOVER_CHAIN`） | `liftover` |
| LocusCompare 参考 manifest | `$LC2_QTL_MANIFEST` | 已有的论文、dataset 名称和 level_1 context |

---

## 4b. 在什么情况下做什么（决策表）

这张表和 `SKILL.md` 开头的 "Situation → what to do" 一致。"程序强制"一列为空表示靠 LLM 按 skill 执行，程序不拦。

### 表的内容

| 情况 | 怎么做 | 程序强制 |
|---|---|---|
| 全部检验过的 variant–phenotype 对（nominal / allpairs） | 保留，需要时处理 | — |
| 只有显著 / lead / eGene / permutation 结果 | 放进 skipped_tables；Phase 3b 找遍了也没有全量表时，才保留最完整的一个，标 `scope: non_full` | submit 要求 `non_full_reason` 和带数字的 `scope_note` |
| fine-mapping、共定位、条件分析、富集、后验结果 | skipped_tables（不是关联统计量） | — |
| QTL 类型不是 eQTL / sQTL / caQTL / pQTL（mQTL、isoQTL、apaQTL…） | skipped_tables："qtl_type not in the manifest"；不能改标成别的类型 | submit 拒绝其他类型 |
| 论文复用的资源（GTEx、eQTLGen…）、非人类样本 | skipped_tables | — |
| 基因型、表达矩阵、counts、peak 列表、协变量 | 不算表；note_location 标 `not_data` | — |
| 论文已在参考 manifest 里 | 只补缺的 QTL 类型 / context，dataset 名用参考 manifest 的 | 不检查（第一条消息里列出已有的） |
| 人工标记为 `not_qtl` | 不 build | 队列跳过 |

### 缺列时怎么办

| 情况 | 怎么做 | 程序强制 |
|---|---|---|
| **没有 nominal p 值**，也没有可换算的 −log10 p 或 1 自由度卡方 | **无法处理**：assess_file 判 `processable: no`，进 skipped_tables | 格式检查认可的 p 值列不存在时，assess_file 拒绝判 ready / yes |
| 只有 FDR / q 值 / 校正后 / permutation p | 无法处理（不是 nominal p） | 格式检查不把这些列算作 p 值 |
| 有 −log10 p 或 1 自由度卡方，没有 p | 处理时换算：p = 10^−x；p = erfc(√(x/2)) | — |
| 只有 z 或 t 统计量（或只有 beta + se） | **目前算无法处理**：格式检查只认 p、−log10 p、卡方 | assess_file 拒绝判 ready / yes |
| **没有 rsID**，或部分为空 | **用 `add_rsid` 补**：按 chrom/pos/ref/alt 查本地 dbSNP，dbSNP 没有的才留空 | 抽到 dbSNP 有但 rsID 为空的变异，validate 就不通过 |
| 叫 rsid / snp 的列里是 `1_123_A_G` 这种 id，不是 rs 号 | 当作 variant id 的来源（拆开），不能填成 `rsid_col`；rsID 用 add_rsid 补 | validate：rsID 只能是 `rs<数字>` 或空 |
| 只有效应等位基因，缺另一个 | 效应等位基因写进 `alt`，`ref` 留空，调用 **`fill_alleles`**：优先用研究自带的 SNP map，没有才用 dbSNP | 写 `_processed` 的 run_python 引用 map 文件会被拒绝 |
| 多等位 / 不确定的位点 | 这一行 `ref` 和 `variant_id` 留空，保留该行；不因此放弃整张表 | validate 允许 ref 为空（variant_id 也为空） |
| 只有 rsID，没有染色体 / 位置 | 找 README 里说明的同研究 variant map；**目前没有工具能从 rsID 查位置**，拿不到坐标就无法处理（skipped_tables 写明原因） | — |
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
| Rebuild：下载文件和上次判定时一样 | 复用判定；只有 skill 变动影响到它时才重新判定 | REBUILD CHECK |
| 任何一步 | 带 `why`；assess_file、note_location、give_up 还要带 `evidence`（逐字引文） | 不带就拒绝 |

**目前和 skill 不完全一致的地方：** skill 的处理示例里有 "z → 双侧 p" 的换算，但 `assess_file` 只认 p、−log10 p、卡方列，所以只有 z / t 统计量的表目前会被判为无法处理。只有 rsID、没有坐标的表，也没有工具能补坐标。这两种情况要不要支持，需要决定。

---

## 5. Rebuild：先检查已有文件（`rebuild.py`）

论文文件夹里已经有文件时，程序在 LLM 第一步之前做下面的检查，结果放在 LLM 第一条消息开头的 **REBUILD CHECK** 里。

### 5.1 skill 有什么变动

每次 build 用的 skill 全文存成 `data/.build_logs/<record_id>/<run_id>.skill.txt`。rebuild 时和上一次 build 的副本做 diff，给 LLM 看哪些规则是新的。没有旧副本时（这个机制之前的 build），程序告诉 LLM 所有规则都可能是新的，要按当前全部规则检查以前的产出。

### 5.2 已下载的文件

- 每个下载的文件由程序先跑一遍 `inspect_local` 的格式检查，LLM 不必再逐个 inspect；
- 以前 build 对**未改动**的文件（大小和修改时间都不变）做的 `assess_file` 判定，从 `data/.build_logs/<record_id>/assessments.json` 复用；改动过的文件要重新判定；
- `assess_file` 可以用 glob，一次判定表头和内容相同的一组文件（例如 `Kanai_2026_pbmc/finngen_multiome_v1.eQTL.*.cis_nominal.tsv.gz`）。

### 5.3 已处理的文件

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
│   ├── <run_id>.skill.txt              # 这次 build 用的 skill 全文
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
| `Build investigation finished` | build 正常结束 |

日志在网页 Details 里的 **Full build logs (.md)** 可以打开和下载，也可以用 `GET /api/manifest/<record_id>/logs` 列出。

---

## 8. 网页上怎么看 build 进度和结果

`progress.py` 读每篇论文最新的日志，`/api/manifest` 返回 `progress`。日志是逐条写的，即使服务器或机器重启，也能看出停在哪一步。

### Build status（6 个值）

| 值 | 含义 |
|---|---|
| Not built | 还没 build 过 |
| Building | 正在 build，单元格显示当前 Phase 和步骤 |
| Queued | 在队列里等，显示排第几 |
| Finished | 上次 build 跑完了（submit 或 give up） |
| Stopped early | 上次没跑完。小字写原因：Interrupted（日志突然结束，服务器或机器停了）、Stopped by a person、Failed、Waiting for AI quota。Rebuild 会接着做 |
| Skipped | 有意不 build：人工标记为非 QTL 数据，或已经在参考 manifest 里 |

单元格还显示：停在哪个 Phase、最后一步的工具和理由、是否做完。

### Data result（5 个值）

| 值 | 含义 |
|---|---|
| Ready | 每行都有数据，而且是全量 summary statistics（仍需人工审核行） |
| Partial | 有可用的行，但不是每行都有数据，或只有部分统计量 |
| Needs you | 需要人：访问申请、只能浏览器下载、批准更大的下载量 |
| No usable data | build 跑完了但没有可用数据，小字写原因 |
| No result yet | 没 build、正在 build，或在出结果前就停了 |

筛选栏上方的 "What the Build status and Data result values mean" 有同样的说明，每个选项悬停也有说明。

### Details 面板

顺序：

1. **Build progress**：11 个 Phase 的进度（✓ 做过、■ 停在这里、▶ 正在做）、停在哪一步及理由和最后输出、最后的问题（之后没有成功步骤时才显示）、rebuild 下一步做什么、AI 步数和 token、最近 8 步；
2. Full build logs；
3. 需要人做的事（访问申请页面、其他瓶颈和处理方法）、其他原因；
4. 策展人备注、Build conversation（可以贴邮件、README、本地路径给 AI）；
5. Source reports（每个数据位置的报告，默认折叠）；
6. 候选 manifest 行表格（Review rows 逐行 Approve / Reject）。

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
- **重启会中断正在跑的 build**（包括 LLM 写的长时间 `run_python`）。重启前先看 `/api/manifest/run`。被中断的 build 在页面上显示 Stopped early / Interrupted，点 Rebuild 接着做。

### Rebuild 一篇论文

1. 页面上点 **Rebuild**；
2. 如果需要给 AI 补充信息（访问邮件、本地文件路径），先在 Details 的 Build conversation 里写，然后点 "Continue build with this context"；
3. 等程序的 rebuild 检查（处理过的大文件第一次要完整读一遍）；
4. 在 Build status 看 Phase 进度，在 Details 看每一步的理由；
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
| 页面显示 Stopped early / Interrupted | Details → Build progress：停在哪个 Phase、最后一步 | 点 Rebuild；程序会先检查已有文件，删掉写了一半的 |
| LLM 一直被拒绝 | `decisions.jsonl` 里 `outcome: refused` 的行和 `result` | 常见原因：`why` 太短、`evidence` 引文对不上、还有文件没 `assess_file`、validate 没通过 |
| validate 报 `rsid_col … is empty for N of M sampled variants that dbSNP holds` | validate 结果 | 对该文件跑 `add_rsid`，再 validate |
| validate 报 chrom 带 `chr`、variant_id 缺 `chr`、peak 格式不对 | validate 结果里的例子 | 修处理脚本，重新处理该文件 |
| processed 文件被 rebuild 删除 | 日志 `Rebuild check of processed files`，`decisions.jsonl` 里 `by: program` 的行 | 正常：说明文件截断或格式不对，LLM 会重新处理 |
| 某个 `run_python` 跑了很久 | 日志里的 `Python stdout / stderr`（脚本应逐文件打印进度） | 超过 `MANIFEST_RUN_MAX_SECONDS` 会被终止，脚本应能从已完成的文件续跑 |
| 机器内存小（当前 3 GB） | `server.log`、`dmesg` | 处理脚本要分块读（每块不超过 20 万行），SKILL 里已有要求 |

### 例：Kanai_2026_pbmc（#639）

- 2026-11-03 09:14 开始的 build，停在 Phase 5 的 `run_python`（逐个处理 eQTL nominal 文件）：输出文件最后写入是 11-04 04:14，机器在 04:33 重新启动，日志没有结束事件；
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
| `allelemap.py` | 用研究自带的 SNP map 补等位基因 |
| `liftover.py` | GRCh37 → GRCh38 |
| `src/frontend/static/js/manifest.js` | Manifest rows 页面（状态、筛选、Details） |
| `src/frontend/static/js/manifest-status.js` | 从位置报告和行推出"需要人做什么" |

测试在 `tests/`：`test_agent.py`（工具、validate、提交）、`test_rebuild.py`、`test_reasons.py`、`test_progress.py` 等。

---

## 13. 已知局限

- 格式检查只读每个文件的前 2 万行。处理脚本对每行用同样的逻辑，所以前面对后面一般也对；完整逐行检查几亿行要好几个小时，没有做。
- rebuild 第一次完整读取大的 processed 文件很慢（每 GB 约 20 秒每线程），之后靠缓存。
- `evidence` 核对的是"引文确实出现在 LLM 看过的内容里"，不能保证 LLM 对引文的解读正确；判定结果仍需人工审核行。
- 2026-11-04 之前的 build 没有保存 skill 副本、`assess_file` 判定和 decisions 记录，rebuild 时这些只能从头开始。
