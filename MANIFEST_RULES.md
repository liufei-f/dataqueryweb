# QTL manifest 候选行：规则说明

规则版本 `2026-10-12`（`manifest_rules.RULES_VERSION`）。改规则时同步改这份文档和版本号。

## 1. 目标

把 Saved records 里 AI 自动确认的论文，变成与 LocusCompare `scripts/qtl_manifest.tsv` 同格式的**候选行**，保存在本仓库的 `manifest/qtl_manifest.tsv`（LocusCompare 那份只作参考，不写入）：
一行 = 一个数据集 × 一种 QTL 类型 × 一个 context，指向一张完整的 nominal summary statistics 表
（或一组按染色体拆分的 `chr*` 文件），并写明导入脚本要读的列名。

候选行要人工 Approve 后才进入 `manifest/qtl_manifest.tsv`。每个字段都带来源和证据，人工在 `/manifest` 页面核对、修改、Approve；全部候选行连同证据另存在 `manifest/qtl_manifest_candidates.tsv`。

## 2. 流程

| 步骤 | 模块 | 做什么 | 是否用 LLM |
|---|---|---|---|
| 1 取文件 | `probe.py` | 列出每个下载位置里的文件；只读每张表的前 256 KB 拿到表头和前 5 行 | 否 |
| 2 文件规则 | `manifest_rules.py` | 表头 → manifest 列；判断表里是什么；合并按染色体拆分的文件；找出兄弟文件之间变化的那段名字 | 否 |
| 3 读论文 | `finder.py` | 重新读论文全文（Europe PMC / 出版商页面） | 否 |
| 4 问 LLM | `manifest.py` | 每篇论文一次调用（表多时每 30 张一次，失败重试一次）：context、样本量、祖源、基因组版本、数据集名，每项附原文引用 | 是 |
| 5 核对与组装 | `manifest.py` | 引用必须能在给 LLM 的文本里找到才算证据；按规则给出状态 | 否 |

LLM 只回答文件本身说不出来的事实。列名、文件范围、路径、状态全部由固定规则决定，同样的输入总是得到同样的结果。

## 3. 27 列各自怎么填

| 列 | 来源 | 规则 |
|---|---|---|
| `dataset` | 参考 manifest → LLM → 规则 | 该论文 DOI 已在参考 manifest 里：沿用那里的名字。否则用论文或仓库点名的资源名（需引用核实）。都没有：`<第一作者姓>_<年份>`，与 eQTL Catalogue 的 `Alasoo_2018` 写法一致 |
| `source_project_id` | 规则 | 等于 `dataset`（单篇论文即一个项目） |
| `use_in_lc2` | 规则 | 一律 `FALSE`，由人决定是否启用 |
| `qtl_type` | 路径 → LLM → 记录 | 路径里写了 `eQTL/sQTL/caQTL/pQTL/mQTL` 就用它；否则用 LLM 的判断；否则用记录里唯一的类型 |
| `biocontext` | LLM + 规则 | 数据集内标识这个 context 的标签。优先用文件自带的标签（兄弟文件间变化的那段），否则 `level_2_context`，否则组织名。只含 QTL 类型或 `summary/statistics/results` 之类字样的标签不算 context，回退到 level 2 / level 1 |
| `level_1_context` | LLM，词表受限 | 大类组织。词表取自参考 manifest 现有的值（Blood、Brain、Neural、Adipose、Muscle、Skin、Pancreas、Placenta、Cartilage、Synovium 及 GTEx 组织名）。全血、PBMC、各类免疫细胞、血浆、LCL 都归 Blood。用了词表外的值会标 `new_level_1_context` |
| `level_2_context` | LLM | 细胞类型、亚区或"细胞+条件"，下划线连接（`CD14_Mono`、`macrophage_IFNg`）；bulk 组织没有更细一层时留空 |
| `population` | LLM | `EUR/EAS/AFR/AMR/SAS`，多祖源队列用 `ALL`；文本没写就留空并标 `population_missing` |
| `sample_size` | 见 §6 | 进入该 context QTL mapping 的基因分型个体数（不是细胞数、不是 QC 前样本数、不是 GWAS 队列） |
| `file_path` | 规则 | `/data/qtl_data/<dataset 小写>/<源路径>`，按染色体拆分的写成 `chr*`。这是建议路径，文件仍需人工下载 |
| `delimiter` | 文件 | 从前几行判断：`\t`、`,`、`;` 或空格 |
| `chrom_col` … `phenotype_id_col` | 文件表头 | 见 §4 |
| `gene_id` | 规则 | 一律留空（与现有 manifest 一致） |
| `gene_id_mode` | 文件取样值 | eQTL/sQTL/pQTL：取样值全是 `ENSG…` → `self`；最后一个 `:` 段是 `ENSG…` → `phenotype_last_segment`；都不是 → 标 `phenotype_id_not_ensembl`。caQTL 留空，但要求 peak id 是 `chr_start_end` |
| `median_tpm` | 文件表头 | 有 `median_tpm` 列就填列名，否则留空 |
| `url` | 仓库 | 下载位置的落地页 |
| `publication` | 记录 | `https://doi.org/<DOI>` |

另外保存但不属于 manifest 的字段：`genome_build`、`status`、`issues`、源文件 URL、证据。

## 4. 表头 → 列名

表头先归一化（小写，非字母数字变 `_`），再按优先级匹配名称，最后用前 5 行的取样值确认。名称对上但取样值不符（例如 `pos` 列不是整数）就跳过这一列。

| 角色 | 认得的写法（节选） | 取样值要求 |
|---|---|---|
| chrom | `chr` `chrom` `chromosome` `snp_chr` `seqnames` | `1–22/X/Y/MT`，可带 `chr` |
| position | `pos` `position` `bp` `snp_pos` `variant_pos` `base_pair_location` | 整数 |
| ref | `ref` `ref_allele` `other_allele` `non_effect_allele` `nea` | 碱基串 |
| alt | `alt` `alt_allele` `effect_allele` `ea` `tested_allele` | 碱基串 |
| beta | `slope` `beta` `effect_size` `effect` `b` `nes` | 数值 |
| se | `slope_se` `se` `beta_se` `stderr` `standard_error` `std` | ≥ 0 |
| pval | `pval_nominal` `pvalue` `pval` `p_value` `p` | 0–1；名称含 `fdr/qval/adj/perm/bonf` 的不算 |
| rsid | `rsid` `rs_id…` `dbsnp…` | `rs` + 数字 |
| maf | `maf` `minor_allele_frequency` | 0–0.5 |
| variant_id | `variant_id` `variant` `snp` `snp_id` `marker` | 取样值是 rsID 时改记为 rsid |
| phenotype_id | `molecular_trait_id` `phenotype_id` `gene_id` `peak_region` `feature_id` `probe` `protein` … | — |

特殊情况都**标记而不猜**：

- **列合在一起**：`chr1_13550_G_A_b38` 这类 variant id、`chr16:85721809` 这类位置列 → 标 `columns_need_deriving`，可拆分出 chrom/pos/ref/alt。id 带 `_b38`/`_b37` 后缀时顺便得到基因组版本。
- **没有 p 值但有 −log10 p 或卡方** → 同样标 `columns_need_deriving`。
- **A1/A2 编号等位基因**：暂按 A1 = alt、A2 = ref 填，并标 `allele_order_ambiguous`，因为各软件约定不同。
- **只有等位基因频率没有 MAF**：填进 `maf_col` 并标 `maf_col_is_allele_frequency`。
- **效应列取样值全在 0–1 之间**（如 RASQUAL 的 π）→ 标 `effect_scale_check`。
- **必需列（chrom、position、ref、alt、pval、phenotype）找不到也推不出** → `required_column_missing`，状态 `not_loadable`。

## 5. 文件判定

**哪些文件会被打开**：扩展名像表格的（`tsv/txt/csv/tab/parquet/xlsx/zip/tar/gz…`）。README、`.md`、`.rtf`、列说明文件当作文本读给 LLM（每个位置最多 4 个）。其余只列出不打开。

**能读的格式**：纯文本及 gzip / bgzip / bz2 / xz；zip 成员（HTTP range 请求，不下载整包）；xlsx 各 sheet；tar 流的开头；parquet 只读页脚拿列名和行数（没有取样值）。

**合并为一行**：路径里的 `chr1…chr22/X/Y` 换成 `chr*` 后相同的文件是一个 unit，对应 manifest 的一行通配路径。

**分组与标签**：同一文件夹里同扩展名的多张表是一组（`OneK1K_<细胞类型>.tsv.gz`）；一个文件夹只有一张表时，与相邻文件夹里同名的表成组（`<细胞类型>/x.cis_qtl_pairs.chr*`）。组内各路径去掉公共前后缀，剩下的就是标签。

**每个位置最多打开 14 个文件**（`MANIFEST_MAX_PROBES`）。文件更多时每组开一到两个，其余沿用同组已开文件的表头并标 `header_inferred_from_sibling`；同组打开的文件表头不一致时再标 `mixed_headers`。

**没有表头的文件**：同一位置里列数相同的有表头文件借给它列名（再用它自己的取样值确认），标 `no_header`。

**表里是什么**（按顺序判断）：

| 判定 | 条件 | 结果 |
|---|---|---|
| `not_qtl` | 文件名像其他数据（covariate、expression、genotype、gwas、coloc、smr…）且没有 QTL 字样；或没有"变异列 + p 值/效应列" | 只进清单，不生成行 |
| `subset` | 文件名含 `signif/sig/egene/fdr/qval/lead/top/perm/indep/conditional/finemap/susie/credible/coloc/clump`；或是电子表格；或 LLM 判断为子集 | 只进清单，不生成行 |
| `all_pairs` | 路径含 `all_pairs/nominal/alltests/sumstats/summary/cis_qtl_pairs/all/full`；或文件 ≥ 20 MB 且有逐变异统计量 | 生成行 |
| `unknown_scope` | 其余 | 生成行，标 `scope_unknown` |

规则判成 `all_pairs` 的，LLM 只能把它收窄成 `subset`，不能反过来放宽。

**不自动处理的下载位置**（在页面上列为 `manual` 或 `skipped`，需要人去看）：

- 受控或需申请的数据（dbGaP、EGA、"on request"）
- 结果浏览器、没有文件链接的网页
- Dropbox、Google Drive、Box、Synapse、Dryad、GWAS Catalog accession
- 补充材料表格：按规则属于子集，默认不打开；`--include-supplements` 可以打开

## 6. 证据规则

LLM 的每个值都要带一句原文引用。程序拿引用去比对**当时给 LLM 的全部文本**：论文全文、仓库描述、README、文件路径、表头。

- 比对只看字母和数字的顺序，忽略大小写、空格、标点和排版差异。用 `…` 缩写的引用，每一段都要按顺序出现在同一份文本里。少于 12 个字母数字的引用不算。
- 找到 → `✓`，并记下出自哪份文本。找不到 → 值保留，标 `✗ unverified`，并加对应 issue。
- **样本量**的引用里必须出现这个数字本身（`1,267` 算 1267，但不算 126）。
- **祖源**的引用读出来的祖源必须和填的代码一致。
- **基因组版本**的引用里必须点名这个版本。文件自己带版本（variant id 后缀、路径里的 `hg38`）时以文件为准。

样本量的取值顺序：

1. LLM 给的该 context 的数字，引用核实通过
2. 文件里取值恒定的 `N` 列（`AN` 列除以 2）
3. LLM 给的全研究数字，引用核实通过 → 多个 context 时标 `sample_size_study_level`
4. 文件名里的数字（`…_175samples_…`）
5. LLM 给了数字但引用没通过 → 标 `sample_size_unverified`

## 7. 状态

一行的状态由它最严重的 issue 决定：

| 状态 | 含义 | 触发的 issue |
|---|---|---|
| `not_loadable` | 现有文件装不进 manifest | `not_full_summary_statistics` `required_column_missing` `header_not_read` |
| `needs_processing` | 先做一步转换就能装 | `columns_need_deriving` `no_header` `format_needs_conversion` `peak_id_needs_reformat` `needs_liftover` |
| `needs_review` | 文件可用，但有事实待人工确认 | 其余任何 issue |
| `ready` | 没有任何 issue | — |

全部 issue 及说明见 `manifest_rules.ISSUES`，页面上每行展开后也会列出。常见的几个：

- `context_unverified` / `sample_size_unverified` / `population_unverified`：值有，引用没对上
- `population_missing` / `sample_size_missing` / `genome_build_unknown`：文本里没有
- `paper_full_text_not_read`：全文没读到，事实只来自摘要和仓库
- `duplicate_biocontext`：同一数据集同一类型下有两行 biocontext 相同（例如全体样本和某个祖源子集）
- `publication_already_in_manifest`：参考 manifest 里已有这篇论文的行
- `qtl_type_not_in_manifest`：类型不在 eQTL/sQTL/caQTL/pQTL 之内

## 8. 使用

```bash
# 处理接下来 20 条还没处理过的人类记录
dataqueryweb-manifest run --limit 20

# 指定记录重跑
dataqueryweb-manifest run --record 20 28

# 只跑取文件和规则，不花 token（没有 context / 样本量 / 祖源）
dataqueryweb-manifest run --limit 50 --no-llm

# 导出：全部行 + 状态、issue、源文件、证据摘要
dataqueryweb-manifest export --out candidates.tsv
# 导出：只要已 Approve 的行，27 列，可直接追加到 qtl_manifest.tsv
dataqueryweb-manifest export --out approved.tsv --scope approved --format manifest
```

**网页 `/manifest`（推荐）**：

- **Build rows** 面板：填"接下来处理几条"（默认 20）或指定记录号，点 **Start**。运行在服务器后台进行，关掉页面不影响；每篇论文跑完立刻出现在下方。**Stop** 会丢弃正在处理的那一篇并停止，已完成的保留。同一时间只能有一个运行。
- 选项：打开补充材料表格、包含非人类记录、重跑已处理的记录、只跑文件和规则（不调用 AI）。
- 每篇论文标题旁的 **Rebuild**：只重跑这一篇。
- 按论文分组列出下载位置和候选行；点 **Check** 展开证据表、列映射、文件预览，可改字段，然后 Approve / Reject。重跑不会覆盖已审核或已修改过的行。
- **输出文件**（本仓库 `manifest/` 目录，每次运行、修改、Approve / Reject 后自动重写，数据库是唯一来源，不要手改这两个文件）：
  - `manifest/qtl_manifest.tsv`：已 Approve 的行，27 列，格式与 LocusCompare 的 manifest 相同。
  - `manifest/qtl_manifest_candidates.tsv`：全部候选行，附状态、issue、源文件和证据摘要。
- `lc2_newfeature_backend/scripts/qtl_manifest.tsv` 只作**参考**（沿用已有的 dataset 名和 level_1 词表），从不写入。
- 右上两个下载按钮（内容与上面两个文件相同）：已 Approve 的行（27 列，可直接追加到 `qtl_manifest.tsv`）；全部行连同状态和证据。

上面的命令行与网页做的是同一件事，适合不开浏览器时用。

环境变量：`DATAQUERY_MANIFEST_DIR`（输出目录，默认本仓库 `manifest/`）、`LC2_QTL_MANIFEST`（参考 manifest 路径，只读，默认找同级目录的 `lc2_newfeature_backend/scripts/qtl_manifest.tsv`）、`MANIFEST_MAX_PROBES`、`MANIFEST_TEXT_CHARS`（给 LLM 的论文文本上限，默认 24000 字符）、`GITHUB_TOKEN`（GitHub 列文件限流时用）。

默认只处理物种为 human 的记录；`--all-species` 取消这个限制。

## 9. 已知局限

- 不下载文件。行里的 `file_path` 是建议路径，拆列、转格式、liftover 都要另外做。
- parquet 只读到列名，看不到取样值，所以 variant id 和 phenotype id 的格式未经检查（`phenotype_values_not_seen`）。
- 每个位置最多开 14 个文件；沿用兄弟文件表头的行需要抽查。
- 论文全文读不到时不会联网搜索，事实只能来自摘要和仓库文本。
- 兄弟文件只有两三个且名字有公共词头时，标签可能被切短（`B_naive` / `B_memory` → `naive` / `memory`）。
- PMC 的补充材料对脚本返回验证页；只有 Europe PMC 存有该文章补充材料压缩包时才能读到。
