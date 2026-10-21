# dy-author 队列与汇报

以下命令里的 `PY`、`DYA` 分别是：

```sh
PY="<当前 skill 的 config.json 中 python 绝对路径>"
DYA="<当前 skill 真实绝对目录>/scripts/dy_author.py"
```

实际执行时把完整路径写进命令（不依赖 shell 变量）。`RUN_DIR`、`ID` 替换为脚本返回值。用户参数、标题、转写内容一律作为数据写入文件，不拼进命令。

## 1. 固定输入

1. `"$PY" -B "$DYA" init` → 记下 `run_dir`、`input_file`。
2. 用写文件工具把当前完整用户请求（含 `/dy-author` 或 `$dy-author` 前缀）原样写入 `input_file`，读回确认。仅当当前消息确是 cc-connect 展开的 `/dy-author` 命令时，从本轮 `DYA_RAW_ARGUMENTS_BEGIN` 后取原始参数，以 `/dy-author` + 换行 + 原始参数写入文件。禁止从历史消息借用命令，禁止为普通分享补前缀。
3. `"$PY" -B "$DYA" plan --run RUN_DIR`。脚本先核验显式命令；`AUTHOR_SCOPE_REQUIRES_EXPLICIT_COMMAND` 时结束作者流程并说明所需命令，不翻主页。已规划批次也核验输入前缀与原始 SHA-256，不能通过重用旧 run_dir 绕过。旧无前缀批次保留保存结果与草稿；继续全量需要当前显式命令建立新批次，已入库作品会被去重。
   - 作者模式：解析链接 → 取作者 → 用固定版本 F2 翻主页（每页 20，页间 2 秒）→ 按 `aweme_id` 扫全库去重 → 排除作者文档记录的“跳过”→ 写 `抖音作者/<昵称>.md`。
   - `source_complete=false`：清单不完整，照常导入已取到的作品，最后说明原因。
   - `author=null`：作者无法核实，只处理输入作品，不写作者文档。
   - `finished=true`：没有待导入作品，直接汇报。
   - 补录模式：列出库中尚未进题库的面试类笔记。

## 2. 分批准备与提炼（循环直到 done）

```text
while True:
  prepare-all --run RUN_DIR --limit 10 # 每批十条，最后一批可不足十条
  if status == "done": progress --notify → 若 batch_finished=true 则汇报一行 → break
  if status == "continue": progress --notify → 汇报本批一行进度 → 再次 prepare-all
  对 ready 中每一条：
    若 draft_files_present=true：先 commit；成功则进入下一条
    Read 完整材料 → 写 item_dir/catalog-terms.txt → catalog-search --run RUN_DIR --id ID
    写 knowledge.md / analysis.json（补录模式不写）
    interview_required=true 时写 interview.json 与题目正文
    commit --run RUN_DIR --id ID       # 本条完成就立即提交，再处理下一条
  progress --run RUN_DIR --notify → batch_finished=true 时汇报 message 一行 → 继续
```

- cc-connect：`--notify` 使用明确的 `CC_PROJECT` 与 `CC_SESSION_KEY`，使用本目录 config.json 中配置的 cc_binary 与 cc_data_dir，不默认选择其他活跃会话。`notification.status=sent` 时提示已发出；`already_attempted` 时不重复发送或回复。其他状态用正常回复补充一行，并继续下一批。发送超时状态未知，不自动重发；提示失败不回滚笔记。Codex 桌面没有这些环境变量时只返回进度，由正常回复展示。
- `prepare-all` 负责下载、Chrome 缓存读取、后台详情请求、每条最多一次浏览器恢复（打开的标签页读完即关）、音轨校验、本地 ASR/OCR，全部由脚本完成。命令耗时较长属正常；不要并行启动第二个 prepare-all。
- 调用 `prepare-all` 时把命令超时设为宿主允许的最大值（Kiro 为 1800000 毫秒）。若仍被宿主超时中断，已完成的条目有检查点，直接对同一 `run_dir` 再执行一次；反复超时保留同一 run_dir 与十条批次的检查点，报告真实中断；禁止删除重建或重复处理已保存条目。`plan` 翻主页约需 1 分钟，也给足超时。
- 每条 ready：Read 完整 `material_file`；图文用图片工具逐页看 `image_files`；从完整材料提取 3–10 个核心主题/技术概念（含同义词）写入 `item_dir/catalog-terms.txt`，执行 `catalog-search --run RUN_DIR --id ID`；只 Read 刷新后的 `catalog_file` 目录树，面试类另读 `question_catalog_file`。检索结果已返回就不重读 matches 文件；候选不足、`has_more=true` 或主题未覆盖时，换同义词/细化术语再次检索，或提高候选 limit。词命中不代表语义相同，不以无命中断言全库没有相关题目；合并前仍读目标全文并使用候选 sha256。按 [knowledge-notes.md](knowledge-notes.md) 写正文，面试类再按 [interview-notes.md](interview-notes.md) 写题。
- 只有配乐、没有可提炼的讲解：把原因写入 `reason_file`，`skip --run RUN_DIR --id ID`（作者文档记为跳过，以后不再处理）。
- 材料不足或提交反复失败：写 `reason_file` 后 `fail --run RUN_DIR --id ID`（下次同一命令会重试）。
- 提交错误先判断能否修正文件（如骨架、小节名、section 引用）；不要绕过 commit 手工写 Vault。
- `model_analysis_needed=false` 且 `status=done`：停止分析，直接汇报失败原因。禁止按标题补写笔记、重建批次循环或临时写下载脚本。
- 上下文压缩后继续使用同一个 `run_dir`；会话中断后用户重发同一命令会建立新批次，已入库/已跳过的作品自动排除。

## 3. 汇报

`progress --run RUN_DIR --notify` 是每批结束的短汇报入口；未提交条目不计新增，失败另列。提示发出后继续，不等待用户确认。`status --run RUN_DIR` 返回最终汇总：

- 作者：昵称；作者文档路径（可点击）。
- 清单：完整/不完整（原因），共 N 条（视频 x、图文 y，有文章时加“文章 z”）。
- 结果：新增 N、已有 N、跳过 N、失败 N，有文章时加“暂不支持 N”；新增笔记用文章标题列出；新增/合并的面试题列出标题。
- 暂不支持（`unsupported`）：抖音文章（aweme_type 163）只列入清单和作者文档，不下载、不写笔记；它不是失败，不给重试命令。
- 失败项：标题或作品 ID 使用 Markdown 链接指向原始完整作品 URL，再附环节与简短原因，一条不漏。
- `retry_command` 非空时，末尾用代码块原样给出，不自动执行。
- 用量：附 `usage` 汇总里的估算输入/输出 token 与耗时，并注明“脚本估算，不是计费数字；请以当前 Agent/模型服务显示的用量为准”。

每条 `commit` 的事务独立；后续失败或模型 API 中断保留全部既有成功，未提交草稿与检查点保存在原 run_dir。恢复先核实并查重，禁止以“全量未完成”为由回滚或删除成功笔记。总日志的新知识笔记/面试题事件写已核实作者名称，历史事件与未知作者留空。

只有 `complete=true`（清单完整、全部处理、无失败、无暂不支持）时才能说“该作者全部作品已入库”；只剩文章未导入时说“视频和图文已全部入库，文章暂不支持”。
