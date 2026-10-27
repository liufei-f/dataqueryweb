# QTL manifest 候选行：规则说明

规则版本 `2026-10-12`（`manifest_rules.RULES_VERSION`）。改规则时同步改这份文档和版本号。

## 1. 目标

把 Saved records 里 AI 自动确认的论文，变成与 LocusCompare `scripts/qtl_manifest.tsv` 同格式的**候选行**，保存在本仓库的 `manifest/qtl_manifest.tsv`（LocusCompare 那份只作参考，不写入）：
一行 = 一个数据集 × 一种 QTL 类型 × 一个 context，指向一张完整的 nominal summary statistics 表
（或一组按染色体拆分的 `chr*` 文件），并写明导入脚本要读的列名。数据文件同时下载到本仓库的 `data/` 目录（已在 `.gitignore` 里），导入脚本读不了的会处理成 `_processed` 后缀的副本。

候选行要人工 Approve 后才进入 `manifest/qtl_manifest.tsv`。每个字段都带来源和证据，人工在 `/manifest` 页面核对、修改、Approve；全部候选行连同证据另存在 `manifest/qtl_manifest_candidates.tsv`。

## 2. 流程

### 2a. 默认：LLM 逐篇调查（`agent.py`）

从 2026-10-20 起，每篇论文由 LLM 自己用工具完成，程序只提供工具并检查结果：

| 工具 | 做什么 |
|---|---|
| `read_paper` | 按关键词搜索或按顺序读论文全文；全文没取到时也搜已打开网页的文字 |
| `open_url` | 打开网页 / DOI，返回文字（`offset` 往下读，`find` 只看匹配段落）和全部链接 |
| `list_files` | 列出仓库（Zenodo、figshare、GitHub、Hugging Face、OSF）或网页上的文件、大小、下载地址 |
| `peek_file` | 不下载，读远程文件 / 压缩包成员 / xlsx / parquet 的表头和前几行 |
| `download` | 下载到 `data/<记录编号>/`，断点续传，受每篇上限和磁盘剩余空间限制；别的论文下过的同一 URL 直接复用（`data/.downloads.json`） |
| `inspect_local` | 看已下载文件或压缩包成员的前几行、文件夹或压缩包的内容 |
| `run_python` | LLM 写 Python 处理数据，生成 `<原名>_processed.tsv.gz`；在数据集文件夹里运行，无网络（`unshare -n`）、无 API key、有时间上限；出现联网、子进程、删除文件、读环境变量、绝对路径等写法的代码直接拒绝执行；脚本保存在 `data/.agent_scripts/<record_id>/` |
| `add_rsid` | 给处理后的表（`<原名>_processed.tsv.gz`）补 rsID：用本地的 NCBI dbSNP（GRCh38，build 157，`data/reference/dbsnp/GCF_000001405.40.gz` 及其 tabix 索引，所有论文共用）按 chrom、pos、ref、alt 精确匹配，匹配不上时再试 ref/alt 互换；dbSNP 里没有的变异 rsID 留空，不猜、不借用相邻位置。表里已有的 rsID 保留。只适用于 GRCh38 坐标。LLM 不能自己下载 dbSNP 或其他参考数据 |
| `validate` | 按导入脚本的方式（`pandas.read_csv`，manifest 的分隔符和列名）读文件前 2 万行，逐列检查数值；**格式必须每一行都对**：染色体和位置只能是数字（`1`，不能是 `chr1`），ref/alt 大写，variant id 写成 `chr<chrom>_<pos>_<ref>_<alt>` 用下划线连接、带 `chr` 前缀（`chr1_12345_A_G`）且和这四列一致，`variant_id_col` 必填；`rsid_col` 必填，值只能是 `rs<数字>` 或空；还检查导入脚本过滤后保留的比例、gene id / caQTL peak id 格式 |
| `note_location` | 记录某个数据位置的结论（需要人工、打不开、不是数据……） |
| `submit` / `give_up` | 提交行；程序检查：`file_path` 必须通过 `validate` 且列名一致、QTL 类型在 manifest 范围内、引用逐字出现在 LLM 实际看过的文本里、context 词表、标签格式、同一 context 不重复。有问题退回给 LLM 改，最多 3 轮 |

LLM 的工作顺序写在 skill 文件 [`skills/build-manifest/SKILL.md`](skills/build-manifest/SKILL.md) 里：读论文 → 清点数据位置 → 选表（下载前先 peek）→（已有链接里没有完整 summary statistics 时）回原文找完整结果：数据/代码可用性声明、accession、出版商页面、预印本与正式版、仓库的新版本和 README → 下载 → 处理 → 验证 → 提交，每一阶段有完成条件，提交前有逐项检查清单；开头还写明了三个 manifest 文件的作用（参考 manifest 只读，`manifest/qtl_manifest_candidates.tsv` 和 `manifest/qtl_manifest.tsv` 由程序写）和 27 列各自怎么填。LLM 的第一条消息会告诉它这篇论文在参考 manifest 里是否已有、已有哪些 qtl_type + biocontext。每次 build 都会把它（去掉开头的 front matter）附在系统提示末尾；改流程只需改这个文件，不用改代码。`MANIFEST_SKILL` 可指向另一份文件。

上限：每篇 80 次工具调用（`MANIFEST_AGENT_STEPS`）、400 万 token（`MANIFEST_AGENT_TOKENS`），单个脚本最长 6 小时（`MANIFEST_RUN_MAX_SECONDS`）。用完后要求 LLM 立即提交已通过验证的行。行的证据里保存 LLM 的总结、运行过的脚本和每次工具调用，在 Check 面板的 “AI work” 里看。

`MANIFEST_AGENT=0` 或勾选“skips the AI”时，用下面 2b 的旧流程。

### 2b. 规则优先的旧流程

| 步骤 | 模块 | 做什么 | 是否用 LLM |
|---|---|---|---|
| 1 取文件 | `probe.py` | 列出每个下载位置里的文件；只读每张表的前 256 KB 拿到表头和前 5 行 | 否 |
| 2 文件规则 | `manifest_rules.py` | 表头 → manifest 列；判断表里是什么；合并按染色体拆分的文件；找出兄弟文件之间变化的那段名字 | 否 |
| 3 读论文 | `finder.py` | 重新读论文全文（Europe PMC / 出版商页面） | 否 |
| 4 问 LLM | `manifest.py` | 每篇论文一次调用（表多时每 30 张一次，失败重试一次）：context、样本量、祖源、基因组版本、数据集名，每项附原文引用 | 是 |
| 5 核对与组装 | `manifest.py` | 引用必须能在给 LLM 的文本里找到才算证据；按规则给出状态 | 否 |
| 6 下载 | `fetch.py` | 把每行的原始文件下载到 `data/<记录编号>/<仓库内路径>`，断点续传，受每篇大小上限和磁盘剩余空间限制 | 否 |
| 7 识别与处理 | `fetch.py` | 用下载后的真实前几行重新识别列；导入脚本能直接读就用原文件，否则由 LLM 从固定操作中选方案，代码执行并逐列检查，写出 `<原名>_processed.tsv.gz` | 是（只选方案） |

LLM 只回答文件本身说不出来的事实，以及“这张表要怎么处理”（只能从固定操作里选）。列名、文件范围、路径、状态由固定规则决定；处理结果一律经过代码的数值检查。

## 3. 27 列各自怎么填

| 列 | 含义 | 来源 | 规则 |
|---|---|---|---|
| `dataset` | 这一行属于哪个数据集合；LocusCompare 按 dataset + qtl_type 分组，一组共用一个 population | 参考 manifest → LLM → 规则 | 该论文 DOI 已在参考 manifest 里：沿用那里的名字。否则用论文或仓库点名的资源名（需引用核实）。都没有：`<第一作者姓>_<年份>`，与 eQTL Catalogue 的 `Alasoo_2018` 写法一致 |
| `source_project_id` | 数据集里的具体研究（如 eQTL-Catalogue 里的 INTERVAL）；与 dataset、qtl_type、biocontext 一起唯一确定一张表，重新导入会更新那张表 | 规则 | 等于 `dataset`（单篇论文即一个项目） |
| `use_in_lc2` | LocusCompare 2 是否对用户显示这张表 | 规则 | 一律 `FALSE`，由人决定是否启用 |
| `qtl_type` | 检验的分子表型：eQTL 表达、sQTL 剪接、caQTL 染色质可及性、pQTL 蛋白 | 路径 → LLM → 记录 | 路径里写了 `eQTL/sQTL/caQTL/pQTL/mQTL` 就用它；否则用 LLM 的判断；否则用记录里唯一的类型 |
| `biocontext` | 这张表的 context 名称，在同一 dataset + source_project_id + qtl_type 下唯一 | LLM + 规则 | 数据集内标识这个 context 的标签。优先用文件自带的标签（兄弟文件间变化的那段），否则 `level_2_context`，否则组织名。只含 QTL 类型或 `summary/statistics/results` 之类字样的标签不算 context，回退到 level 2 / level 1 |
| `level_1_context` | 大类组织，用于浏览和筛选；导入脚本要求必填 | LLM，词表受限 | 大类组织。词表取自参考 manifest 现有的值（Blood、Brain、Neural、Adipose、Muscle、Skin、Pancreas、Placenta、Cartilage、Synovium 及 GTEx 组织名）。全血、PBMC、各类免疫细胞、血浆、LCL 都归 Blood。用了词表外的值会标 `new_level_1_context` |
| `level_2_context` | level 1 之下更细的 context（细胞类型、亚区、细胞 + 条件） | LLM | 细胞类型、亚区或"细胞+条件"，下划线连接（`CD14_Mono`、`macrophage_IFNg`）；bulk 组织没有更细一层时留空 |
| `population` | 样本的遗传祖源 | LLM | `EUR/EAS/AFR/AMR/SAS`，多祖源队列用 `ALL`；文本没写就留空并标 `population_missing` |
| `sample_size` | 进入这个 context QTL mapping 的基因分型个体数 | 见 §6 | 进入该 context QTL mapping 的基因分型个体数（不是细胞数、不是 QC 前样本数、不是 GWAS 队列） |
| `file_path` | 导入脚本读取的文件；`chr*` 通配一次读入同一 context 的全部染色体文件 | 下载结果 | 相对于数据目录的路径（导入时用 `--data-root <数据目录>`）：`<记录编号>/<仓库内路径>`，处理过的指向 `<原名>_processed.tsv.gz`，按染色体拆分的写成 `chr*`。没下载成功时是将来的位置，并带对应 issue |
| `delimiter` | 列分隔符，传给 `pandas.read_csv` | 文件 | 从前几行判断：`\t`、`,`、`;` 或空格；处理过的文件一律 `\t` |
| `chrom_col` … `phenotype_id_col` | 文件里各个角色所在列的**列名**（不是值）：染色体、位置、ref、alt、效应量、标准误、名义 p 值、rsID、MAF、variant id、表型 id。必需：chrom、position、ref、alt、pval、phenotype_id；可选列留空时 beta/se/maf 记为 0 | 文件表头 | 见 §4 |
| `gene_id` | 导入脚本不读（基因由 phenotype_id 推出） | 规则 | 一律留空（与现有 manifest 一致） |
| `gene_id_mode` | 导入脚本如何从 phenotype_id 得到 Ensembl 基因：`self` 本身就是基因；`phenotype_last_segment` 取最后一个 `:` 段 | 文件取样值 | eQTL/sQTL/pQTL：取样值全是 `ENSG…` → `self`；最后一个 `:` 段是 `ENSG…` → `phenotype_last_segment`；都不是 → 标 `phenotype_id_not_ensembl`。caQTL 留空，但要求 peak id 是 `chr_start_end` |
| `median_tpm` | 文件里每个基因中位表达量（TPM）那一列的列名 | 文件表头 | 有 `median_tpm` 列就填列名，否则留空 |
| `url` | 人去哪里找这份数据（数据落地页） | 仓库 | 下载位置的落地页 |
| `publication` | 数据出自哪篇论文 | 记录 | `https://doi.org/<DOI>` |

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

## 6b. 下载与处理（`fetch.py`）

**下载哪些行**：状态不是 `not_loadable`、不是子集、QTL 类型在 eQTL/sQTL/caQTL/pQTL 之内的行。已审核或改过的行重跑时不动。

**存放**：`data/<记录编号>/<文件在仓库里的路径>`，每条 Saved record 一个文件夹。zip/tar 内的成员处理后放在 `data/<记录编号>/<压缩包名>/`。

**记录编号**（`record_code`）：每条 Saved record 保存时自动生成、之后不再改变，格式 `<第一作者姓>_<年份>_<一个词>`，例如 `Sun_2026_skin`。姓氏只保留 ASCII 字母（`Höglund` → `Hoglund`，`Nunes-Nesi` → `NunesNesi`）；那个词是 context 里最先出现的组织或细胞类型（`blood`、`pbmc`、`brain`、`liver`…，词表见 `records.TOPICS`），涉及三类以上组织或写明多组织时为 `multitissue`，都没有时用 QTL 类型（`pqtl`）。重名的依次加 `_2`、`_3`。Saved records 和 Manifest 页面都显示编号，导出的 TSV/CSV 里是 `record_code` 列。

**大小**：每篇论文默认最多 50 GB（页面上可改，或 `--max-gb`、`MANIFEST_MAX_GB`），并始终给磁盘留 20 GB；超出的行标 `data_too_large`，不下载。已存在且大小一致的文件不重复下载，中断的从 `.part` 续传。

**能否直接用**：导入脚本用 `pandas.read_csv` 读单个分隔文本表，需要 chrom、position、ref、alt、p 值、phenotype 各自成列。下列任一情况就要处理：在压缩包里、parquet 或表格文件、没有表头、需要从别的列推出必需列、空白分隔、压缩格式与扩展名不符（如 `.bgz`）。可以直接用的，也会按导入脚本的读法读每个文件的前 2000 行并检查数值。

**处理方案**：

1. 下载后先用真实的前几十行重新识别列（parquet 在下载前只有列名）。
2. 表头相同的表归为一类，每篇论文问 LLM 一次：给它列名、前 5 行和规则的识别结果，要它从下面的操作里选，或者回答“做不到”。
   - `split`：按给定字符拆一列（`1:12345:A:G`、`chr1_12345_A_G_b38` → chrom、pos、ref、alt）
   - `neglog10_to_p`：p = 10^-x
   - `chisq_to_p`：1 自由度卡方 → p
   - `z_to_p`：z → 双侧 p
3. 代码执行方案，先在前 2000 行上检查：chrom 是染色体名、position 是整数、ref/alt 是碱基、p 值在 0–1 之间、phenotype 非空，每项至少 95% 合格。不合格就丢弃该方案，改用规则从表头推出的方案；两个都不合格，标 `processing_failed`，页面上列出试过的方案和失败原因。
4. 通过后处理全部文件：保留原有全部列，追加新列，写成 tab 分隔的 `<原名>_processed.tsv.gz`，行数与原文件一致。原始文件保留。
5. 用处理后文件的 phenotype 取样值重新判断 `gene_id_mode`，并更新 manifest 行的列名、分隔符和 `file_path`。

LLM 只给方案，不写代码，也不直接改文件。

**证据**：每行的 Data 一节记录下载的文件和大小、为什么要处理、执行的步骤及方案来源（AI 或规则）、被拒的方案、逐列检查结果、处理后文件的前 5 行。

## 7. 状态

一行的状态由它最严重的 issue 决定：

| 状态 | 含义 | 触发的 issue |
|---|---|---|
| `not_loadable` | 现有文件装不进 manifest | `not_full_summary_statistics` `required_column_missing` `header_not_read` |
| `needs_processing` | 数据还不在可导入的状态 | `columns_need_deriving` `no_header` `format_needs_conversion` `peak_id_needs_reformat` `peak_id_not_chr_start_end` `needs_liftover` `data_not_downloaded` `data_too_large` `download_failed` `processing_failed` |
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

# 只填表、不下载；或改每篇的下载上限
dataqueryweb-manifest run --limit 20 --no-download
dataqueryweb-manifest run --record 28 --max-gb 200

# 导出：全部行 + 状态、issue、源文件、证据摘要
dataqueryweb-manifest export --out candidates.tsv
# 导出：只要已 Approve 的行，27 列，可直接追加到 qtl_manifest.tsv
dataqueryweb-manifest export --out approved.tsv --scope approved --format manifest
```

**网页 `/manifest`（推荐）**：

- 页面列出全部 Saved records，一篇一行（默认显示所有物种，勾选 “Species: human only” 只看人类记录；Build 只处理人类记录；每次显示 100 篇，可搜索，可筛选“未建 / 已建 / 有待查行 / 有已批准行”）。
- 每篇右边的 **Build manifest**：为这篇填 manifest 行，并下载、处理数据。在服务器后台一篇接一篇进行；同时点了多篇会排队，关掉页面也会继续。建过的显示 **Built**、各状态行数、数据情况和检查进度，按钮变成 **Rebuild**。
- 页面上方的设置对之后点的 Build 生效：是否下载并处理数据及每篇上限（GB）、是否打开补充材料表格、是否跳过 AI。
- 运行时顶部有一条状态栏，显示正在建哪篇、哪一步、还有几篇排队；**Stop** 丢弃正在建的那篇并清空队列。
- 已建的论文点 **Check**（括号里是待查行数）展开下载位置和候选行；每行再点 **Check** 展开证据表、列映射、数据处理记录和文件预览，可改字段，然后 Approve / Reject。Rebuild 不会覆盖已审核或已修改过的行。
- **输出文件**（本仓库 `manifest/` 目录，每次运行、修改、Approve / Reject 后自动重写，数据库是唯一来源，不要手改这两个文件）：
  - `manifest/qtl_manifest.tsv`：已 Approve 的行，27 列，格式与 LocusCompare 的 manifest 相同。
  - `manifest/qtl_manifest_candidates.tsv`：全部候选行，附状态、issue、源文件和证据摘要。
- 参考 manifest 是本仓库的 `reference/qtl_manifest.tsv`，从 LocusCompare 的 `lc2_newfeature_backend/scripts/qtl_manifest.tsv` 复制过来（2026-10-27，对应那边的提交 `2b7cf70`），只作**参考**（沿用已有的 dataset 名和 level_1 词表），从不写入。程序不读写本仓库以外的文件；LocusCompare 的 manifest 更新后，手动再复制一次。
- 右上两个下载按钮（内容与上面两个文件相同）：已 Approve 的行（27 列，可直接追加到 `qtl_manifest.tsv`）；全部行连同状态和证据。

上面的命令行与网页做的是同一件事，适合不开浏览器时用。

环境变量：`DATAQUERY_DATA_DIR`（数据目录，默认本仓库 `data/`）、`MANIFEST_MAX_GB`（每篇默认下载上限，50）、`DATAQUERY_MANIFEST_DIR`（输出目录，默认本仓库 `manifest/`）、`LC2_QTL_MANIFEST`（参考 manifest 路径，只读，默认本仓库 `reference/qtl_manifest.tsv`）、`MANIFEST_MAX_PROBES`、`MANIFEST_TEXT_CHARS`（给 LLM 的论文文本上限，默认 24000 字符）、`GITHUB_TOKEN`（GitHub 列文件限流时用）。

默认只处理物种为 human 的记录；`--all-species` 取消这个限制。

## 9. 已知局限

- 不做 liftover（GRCh37 → GRCh38）、不补 peak 坐标、不把 UniProt 等编号换成 Ensembl：这些行下载处理后仍是 `needs_processing` 或 `needs_review`。
- 不处理 xlsx 表格文件（补充材料默认也不下载）。
- 大数据集下载很慢、很占空间：TenK10K 一篇约 1.2 TB，Zenodo 实测约 3 MB/s。超过上限的行要在磁盘够大的机器上提高上限再跑。
- 下载和处理在网页服务的进程里进行；服务重启会中断，重跑时已下载完的文件不再下载，`.part` 会续传。
- 每个位置最多开 14 个文件；沿用兄弟文件表头的行需要抽查。
- 论文全文读不到时不会联网搜索，事实只能来自摘要和仓库文本。
- 兄弟文件只有两三个且名字有公共词头时，标签可能被切短（`B_naive` / `B_memory` → `naive` / `memory`）。
- PMC 的补充材料对脚本返回验证页；只有 Europe PMC 存有该文章补充材料压缩包时才能读到。
