# DataQuery 使用说明

DataQuery 用来查找**新发表的 QTL 数据**及其下载地址。它可以搜索 Europe PMC 文献（包括 bioRxiv / medRxiv 预印本），也可以直接输入某篇文章的 DOI 或网址。后端会逐篇阅读文章，找出 Data availability 声明、数据仓库链接和编号，再用免费 LLM 判断文章是否产生了新的 QTL 数据、哪个链接可以下载。

- 网页地址：<http://127.0.0.1:8010>
- 项目目录：`/Users/theeeight/github/dataqueryweb`

下面的命令都假设你已经进入项目目录：

```bash
cd /Users/theeeight/github/dataqueryweb
```

---

## 1. 第一次使用：安装依赖

需要 [uv](https://docs.astral.sh/uv/)（已安装在 `anaconda3/envs/bio` 里）。

```bash
uv sync
```

## 2. 启动、停止、重启服务

| 操作 | 命令 |
|---|---|
| 后台启动（关掉终端也继续运行，日志写到 `server.log`） | `nohup uv run dataqueryweb --port 8010 > server.log 2>&1 &` |
| 前台启动（占用当前终端，`Ctrl+C` 停止） | `uv run dataqueryweb --port 8010` |
| 开发模式（改完代码保存后自动重启） | `uv run dataqueryweb --port 8010 --reload` |
| 停止 | `pkill -f "dataqueryweb --port 8010"` |
| **重启** | 见下方 |

重启命令（先停止，再在后台启动）：

```bash
pkill -f "dataqueryweb --port 8010"; sleep 2
nohup uv run dataqueryweb --port 8010 > server.log 2>&1 &
```

以下几种情况需要重启：

- 修改了 Python 代码（`src/backend/…`），且没有用 `--reload` 启动；
- 修改了 `.env`，比如新增了 LLM 的 key。

只改前端文件（`src/frontend/…` 下的 HTML、CSS、JS）不需要重启，刷新浏览器即可。

其他参数：

- `--port 8020`：换一个端口；
- `--host 0.0.0.0`：让局域网里的其他电脑也能访问（请注意安全）。

## 3. 检查服务状态

```bash
curl localhost:8010/health        # {"status":"ok",...} 表示服务正常
curl localhost:8010/api/llm       # 当前使用的 LLM 服务商和模型
lsof -iTCP:8010 -sTCP:LISTEN      # 查看哪个进程占用了 8010 端口
tail -f server.log                # 实时查看后台运行的日志（Ctrl+C 退出查看）
```

如果启动时报 `address already in use`，说明端口已被占用：先执行上面的停止命令，或者用 `lsof` 找到进程号后执行 `kill <PID>`。

## 4. 网页怎么用

1. **Search literature**：输入关键词，例如 `single-cell eQTL`、`sQTL brain`。
   - **Papers**：一次读取多少篇文章，可以直接填数字，1–1000（上限可用环境变量 `DATAQUERY_MAX_LIMIT` 调整）。超过 50 篇时会提示耗时和 token 用量。Europe PMC 每次最多返回 1000 篇，程序会自动分页取。
   - **Years**：发表年份范围，例如 `2025 – 2026`，两端都可以留空（留空表示不限）。旁边的 **This year / 2 yrs / 5 yrs / Any** 可以一键填写。
   - **Sort**：`Relevance`（相关度）或 `Newest first`（最新发表的在前，适合找新数据）。
   - **Sources**：`All` / `Journals` / `bioRxiv + medRxiv`。
   - 这些选项会保存在浏览器地址栏的网址里，刷新页面或把链接发给别人时会原样保留。
   - **Only papers mentioning QTL**：只搜提到 QTL 的文章。
   - 普通关键词只匹配标题和摘要。也可以直接写 Europe PMC 语法，例如 `AUTH:"Smith J" AND eQTL`。
   - 搜索进行中，**Search** 旁边会出现红色的 **Stop** 按钮。点它会立即停止：已读完的文章保留在页面上，服务器也会同时取消还没读完的文章，不再消耗 GPT 额度，也不会再自动保存。关掉网页标签也有同样效果。
2. **DOI or URL**：粘贴 DOI、期刊或 PMC / PubMed / bioRxiv / medRxiv 的网址、PMID 或 PMCID。
3. **AI check for new QTL data**（默认打开）：免费 LLM 会判断每篇文章是否有新的 QTL 数据，并从已找到的链接中选出下载位置，这些位置以绿色的 `AI: …` 标出。
4. **Auto-save when the AI is sure**（默认打开，需要同时打开 AI check）：满足以下全部条件的文章，会被自动存进 Saved records，并标为 **🤖 Auto-saved**：
   - AI 判定为新 QTL 数据；
   - 置信度 ≥ 90%；
   - 由主模型（gpt-5.5 等）判断，而不是备用的 Pollinations；
   - AI 指出了存放 QTL 结果的位置（summary statistics、结果浏览网站或补充表格）。只有原始数据或代码链接的不算。

   只保存符合条件的那几个位置。自动保存不会覆盖你手动保存的记录和备注。没自动保存的文章，结论下方会写明原因，例如 "Needs human check: confidence 85% < 90%"。
   **已保存过的文章不再送给 AI**（节省 token）：已在 Saved records 里的文章（不论是你保存的还是 AI 自动保存的），不会再让 AI 读全文或联网搜索。页面显示 **⏭ AI check skipped**，规则提取的链接照常显示。判断"同一篇文章"的依据是以下任一项相同：DOI（包括预印本正式发表后的期刊 DOI）、PMID、标题（忽略大小写和标点）、文章网址。想让 AI 重新判断，点这篇文章上的 **Re-check with AI**（会消耗 token）；API 用法是加 `&recheck=true`。
   **confidence 是怎么算的**（写在 `src/backend/dataqueryweb/rubric.py`，How it works 页面的 "Scoring rubric" 一节会列出完整内容）：
   - AI **不直接给结论和分数**，只回答 3 个事实问题，每个答 yes/no/unclear，并附原文句子：① 作者是否自己做了 QTL mapping；② 是否只用了别人发表的 QTL 结果；③ 文章是否说明了自己的 QTL 结果在哪里可以获取。回答 yes 但没附原文句子的，按 unclear 处理。
   - 程序另外检查 2 项：④ AI 是否选出了存放 QTL 结果的下载位置（summary statistics、结果浏览网站或补充表格）；⑤ 是否读到了全文。
   - **结论**：① 为 yes 且 ② 不为 yes → 有新数据；① 为 no 或 ② 为 yes → 没有新数据；其他情况 → 不确定。
   - **分数**（有新数据时）= 满足项的权重之和：① 35 + ② 15 + ③ 20 + ④ 15 + ⑤ 15 = 100。自动保存要求 ≥ 90，也就是**五项全部满足**，缺任何一项最多 85 分。
   - 每篇文章的结论下方会列出这五项，✓/✗ 加权重。鼠标悬停可以看 AI 的回答和原文句子，也可以展开 "Why this score" 查看。
   - 规则改动时要更新 `RUBRIC_VERSION`，每次判断都会记录用的是哪个版本。
5. 结果上方的**审核状态筛选**（带数量）：
   - **All**：全部；
   - **Needs human check**：AI 认为有新数据或不确定、但没到自动保存标准，或者 AI 没能判断的文章，**需要你来看**；
   - **Auto-saved**：AI 已自动保存，建议抽查。每行有 **Confirm**（确认无误，变为"你保存的"）和 **Remove**（AI 判断错了，删除）；
   - **Saved by you**：你保存或确认过的；
   - **No new QTL data**：AI 判定只用了现有 QTL 数据（MR、SMR 等）的文章。
6. 结果区的其他筛选项：
   - **Hide papers with no data source**：隐藏没有找到任何数据来源的文章；
   - **Hide low-relevance links**：隐藏低相关度的链接。

预印本如果已经正式发表，会自动读取期刊版本，并把期刊版本里的数据来源合并进来，这些来源前面会标出期刊名。

**保存正确的记录**：结果表格每行最右边有一个 **Save** 按钮。确认这一行（文章 + 下载位置）正确后点它，这条记录就会存进 **Saved records** 表格，按钮变成 **Saved ✓**；再点一次会取消保存。详见第 5 节。

浏览器地址栏里的链接（例如 `http://127.0.0.1:8010/?q=eQTL`）可以直接分享，打开后会自动重新搜索。

## 5. Saved records（已保存的记录表）

- 页面：<http://127.0.0.1:8010/records>（导航栏中的 **Saved records**）。
- 每条记录会标明来源：**🤖 auto-saved**（AI 自动保存，显示置信度，旁边有 **Confirm** 按钮）或你自己保存的。可以用下拉框只看 "Auto-saved (to check)"；也可以直接打开 <http://127.0.0.1:8010/records?by=auto>。导出的表格里多了 `saved_by`（auto/human）和 `ai_confidence` 两列。
- 在搜索页再次点 Save 或 Confirm 时，会保留你已写的备注（修复了之前重新保存会清空备注的问题）。
- 每条记录对应一篇文章加一个下载位置。同一篇文章、同一个下载地址再次保存时，会更新原记录，不会重复。网址比对时会忽略 `http`/`https`、`www.` 和末尾的 `/`。
- **搜索页面会提示已保存的内容：**
  - 文章卡片上方显示 **✓ Already in Saved records**，写明这篇文章已存了几条、最后保存日期；点 **View** 会跳到 Saved records 并自动按这篇文章筛选。这次结果里没有出现、但以前保存过的下载地址也会列出来。
  - 已保存的那一行有 **✓ Already saved · 日期** 标签和蓝色底色，按钮显示 **Saved ✓**，点它会先确认再取消保存。
  - 已保存的行和文章不会被 "Hide low-relevance links"、"Only papers with new QTL data" 隐藏。
  - 搜索完成后的状态栏会显示 "N already in Saved records"。
  - 在另一个标签页里增删记录后，切回搜索页会自动更新保存状态。
- 页面上可以：按关键词筛选；在 **Curator note** 里写备注（点到别处后自动保存）；点 **×** 删除；**Download TSV / CSV** 导出整张表。
- 导出的列名与 locusview 的 `qtl-data-agent` 审核表一致（`record_id, publication_title, publication_url, doi, pmid, first_author, authors, year, journal, qtl_type, qtl_context, dataset_name, download_url, access_route, extraction_note, evidence_source, search_terms`…）。另外还多了几列：`repository`、`content`、`file_urls`、`ai_verdict`、`ai_reason`、`curator_note`、`saved_at`。
- 数据存在 SQLite 文件 `data/records.sqlite3` 里，重启服务不会丢失。`data/` 已加入 `.gitignore`。想换存放位置，可以在启动前设置 `DATAQUERY_DB=/path/to/file.sqlite3`。

命令行操作：

```bash
curl localhost:8010/api/records                                  # 列出全部记录（JSON）
curl -o saved.tsv "localhost:8010/api/records/export?fmt=tsv"    # 导出 TSV
curl -o saved.csv "localhost:8010/api/records/export?fmt=csv"    # 导出 CSV
curl -X DELETE localhost:8010/api/records/3                      # 删除第 3 条记录
cp data/records.sqlite3 records-backup-$(date +%F).sqlite3       # 备份数据库
sqlite3 data/records.sqlite3 "SELECT record_id, doi, download_url FROM records;"   # 直接查询
```

## 6. 配置免费 LLM（推荐）

默认使用不需要 key 的 Pollinations（gpt-oss-20b）。它一次只能处理一篇文章，每篇约 15–25 秒。配置一个免费 key 会更快，模型也更大：

```bash
cp .env.example .env
# 编辑 .env，取消注释并填入其中一个 key：
#   OPENROUTER_API_KEY=sk-or-...   在 https://openrouter.ai/keys 免费注册
#   GROQ_API_KEY=gsk_...           在 https://console.groq.com/keys 免费注册
```

**换 key 不需要重启**：服务每次调用 LLM 前都会重新读取 `.env`。页面上的显示：
- "AI check" 勾选框旁边显示当前在用的服务商、模型和**打码的 key**（例如 `key sk-Z0E…wMZq`），完整 key 不会出现在页面上；
- 页面每 30 秒、以及切回标签页时，会重新读取设置。发现 key 和上次不同，会出现黄色提示 **🔑 LLM API key changed: 旧 → 新**，并自动检测新 key 能不能用：✓ 表示可以用，会列出可用模型数；✗ 表示不能用，会给出错误原因，例如 401 Invalid API key；
- 随时可以点 key 旁边的 **check key** 手动检测。检测只查询模型列表，不消耗 token。

注意：启动服务前已在终端里 `export` 的同名变量优先于 `.env`。`.env` 里删掉的变量会随之失效。

命令行确认：

```bash
curl localhost:8010/api/llm          # 当前服务商、模型、打码的 key
curl localhost:8010/api/llm/check    # 检测 key 是否可用（不耗 token）
```

可选设置（写在 `.env` 里）：

- `LLM_MODEL=qwen/qwen3.8-27b:free,google/gemma-4-31b-it:free`：指定模型，用逗号分隔，按顺序尝试；
- `LLM_BASE_URL=…`、`LLM_API_KEY=…`：改用任何兼容 OpenAI 格式的接口（地址要写到 `/v1` 为止）；
- `LLM_FALLBACK=pollinations`：配置的模型都失败时（比如额度用完），自动改用不需要 key 的 Pollinations。该模型额度用完后，本次搜索的其余文章会直接跳过它，不会反复等待。页面上结果标注为 `(fallback)`。

- `LLM_MAX_TEXT_CHARS=60000`：每篇文章最多发给 LLM 多少字符的全文。优先放讲数据、QTL、编号的段落，其余按原文顺序补齐。全文越长，判断越准，但消耗的额度也越多。
- `LLM_WEB_SEARCH=true`：接口支持 OpenAI Responses API 的 `web_search` 工具时（目前这个代理支持），以下两种情况 LLM 会自己上网搜索、打开文章和数据仓库页面，找出真正的数据地址：
  - 读不到全文（PMC 没有开放全文、出版社或 bioRxiv 拦截）；
  - AI 判断有新数据，但全文里没找到下载链接。

  上网找到的每个 URL 都会被实际访问一次：不存在的（404 等）会被删掉；存在但拒绝程序访问的会标注 `(link blocked)`。页面上可以展开 **AI web search**，查看它搜了什么、打开了哪些网页。每篇大约多花 1–2 分钟和约 2 万 token。

目前 `.env` 的配置是：先用 `http://54.179.178.78:8317/v1` 代理上的 `gpt-5.5` → `gpt-5.6-luna` → `gpt-6-luna`，都不可用时改用 Pollinations（Pollinations 只接收 1.2 万字符的全文，也不能上网）；每篇最多发送 6 万字符全文；已开启上网搜索。查看这个代理当前有哪些模型：

```bash
source .env && curl -s -H "Authorization: Bearer $LLM_API_KEY" "$LLM_BASE_URL/models"
```

- `AUTO_SAVE_MIN_CONFIDENCE=0.9`：自动保存需要的最低置信度，设成 0.95 会更严格；
- `AUTO_SAVE_FALLBACK=false`：设为 `true` 时，备用模型的判断也可以触发自动保存（不建议）。

`.env` 已加入 `.gitignore`，不会被提交到 git。

## 7. 直接调用 API（命令行 / 脚本）

```bash
# 查询单篇文章（加 ai=true 会附带 LLM 的判断）
curl "localhost:8010/api/inspect?ref=10.1038/s41588-021-00913-z&ai=true"

# 搜索文献，返回 NDJSON：一行 meta，每篇文章一行 paper，最后一行 done
curl -N "localhost:8010/api/search?q=single-cell+eQTL&limit=10&source=preprint&ai=true"

# 2025–2026 年发表、最新的在前、读 100 篇
curl -N "localhost:8010/api/search?q=eQTL&limit=100&year_from=2025&year_to=2026&sort=newest&ai=true&autosave=true"

# 把搜索结果保存到文件
curl -sN "localhost:8010/api/search?q=pQTL&limit=20&ai=true" > pqtl.ndjson
```

`/api/search` 的参数：

| 参数 | 含义 | 默认值 |
|---|---|---|
| `q` | 关键词 | 必填 |
| `limit` | 读取的文章数（1–1000） | 10 |
| `year_from` / `year_to` | 发表年份范围，可只填一端 | 不限 |
| `sort` | `relevance` / `newest` | `relevance` |
| `qtl_only` | 只搜提到 QTL 的文章 | `true` |
| `source` | `all` / `journal` / `preprint` | `all` |
| `ai` | 是否使用 LLM 判断 | `false` |
| `autosave` | AI 很有把握时自动保存 | `false` |
| `recheck` | 已保存的文章也重新交给 AI 判断 | `false` |

## 8. 开发与测试

```bash
uv run pytest            # 离线单元测试，不访问网络
uv run ruff check src tests
uv run ruff format src tests
```

代码结构：

- `src/backend/dataqueryweb/web.py`：网页和 API 路由；
- `src/backend/dataqueryweb/finder.py`：访问 Europe PMC、PMC、bioRxiv、Zenodo、figshare；
- `src/backend/dataqueryweb/extract.py`：从全文和网页中提取链接与编号；
- `src/backend/dataqueryweb/llm.py`：免费 LLM 判断；
- `src/backend/dataqueryweb/records.py`：已保存记录（SQLite）与导出；
- `src/frontend/`：页面模板、CSS 和 JS。

## 9. 常见问题

- **页面打不开**：先运行 `curl localhost:8010/health`。没有返回就启动服务（见第 2 节），然后查看 `server.log`。
- **AI 结论显示 "AI check failed"**：免费服务限流或暂时不可用。稍后重试，或者配置 OpenRouter / Groq 的 key。
- **文章显示 "publisher page refused…"**：这篇文章在 PMC 里没有开放全文，出版社也拦截了程序访问。请打开原文自己查看 Data availability。
- **结果不对**：AI 和规则都可能判断错误。页面上有理由和原句可以核对。
