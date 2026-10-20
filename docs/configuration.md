# 个人配置

推荐通过 `scripts/configure.py` 生成本地 `config.json`，不要直接修改示例来提交 Git。配置快照会复制到每次 run_dir，勿公开运行目录。

| 字段 | 含义与配置方式 |
| --- | --- |
| `vault` | 已有 Obsidian Vault 的绝对路径；写入目标。必须可读写。 |
| `python` | 本技能虚拟环境的 Python 绝对入口，配置器取当前 `sys.executable`。不要写另一个技能或他人的 Python。 |
| `cache_dir` | 个人运行材料/状态目录，默认仓库下 `.cache`；不要放在公开同步目录。 |
| `shared_cache_dirs` | 可选的既有兼容材料缓存，默认空；不依赖 `/dy` 的缓存。只加入自己明确授权的路径。 |
| `browser` | `chrome`；无浏览器模式为 JSON `null`。 |
| `browser_profile` | Chrome 的完整 profile 目录，须与实际 Cookie 读取/页面打开使用的资料一致。 |
| `browser_cache_dir` | 同一资料的 `Cache_Data`；标准 macOS 路径一般为 `~/Library/Caches/Google/Chrome/<资料名>/Cache/Cache_Data`，实际配置使用展开后的绝对路径。 |
| `auto_browser_recovery` | 默认 `true`；材料获取失败时有限地打开作品页面恢复。无浏览器模式为 `false`。 |
| `prefer_browser_media` | 默认 `false`；为 `true` 时优先浏览器恢复。 |
| `background_detail_first` | 默认 `true`；先后台取严格匹配 ID 的详情，再回退浏览器。 |
| `close_opened_tabs` | 默认 `true`；仅尝试关闭本次打开、严格匹配作品的新增标签。需系统授权。 |
| `browser_recovery_wait_seconds` | 默认 8，0–20 秒。 |
| `browser_media_timeout_seconds` | 默认 40，0–40 秒，共享缓存等待预算。 |
| `prepare_workers` | 默认 2；本地材料并行准备进程数。不要并行运行多个 `prepare-all`。 |
| `prepare_batch` | 默认 10；Agent 流程也显式传 `--limit 10`，不要超过十条。 |
| `asr_model` | 默认 `mlx-community/whisper-small-mlx`；首次从模型托管方下载，需网络。 |
| `notes_dir` | 默认 `知识库`，Vault 内的知识笔记相对目录。 |
| `attachments_dir` | 默认 `附件`，Vault 内图片附件相对目录。 |
| `authors_dir` | 默认 `抖音作者`，作者文档相对目录。 |
| `question_root` | 默认 `面试题库`，置于 `notes_dir` 内；写作规则使用此默认分类名称，首次安装建议保留。 |
| `review_state_file` | 默认 `.dy-review-state.json`，Vault 内的学习/审核状态文件。 |
| `unified_only` | 保持 `true`，使用现有统一索引和日志格式。 |
| `cc_binary` | 可选 cc-connect 可执行文件的绝对路径；未填时尝试 `~/.local/bin/cc-connect`。建议明确填写。 |
| `cc_data_dir` | 可选 cc-connect 当前服务的实际 data-dir；未填时使用 `~/.cc-connect`。须与该服务一致。 |

目录配置不是任意 shell 代码。路径包含空格时在终端中完整引用。相对分类目录不要带 `..` 或绝对路径；不把聊天内容拼入 shell。修改输出目录时也要同步审阅写作规则，避免 Agent 的分类输出与脚本配置不一致。

不在 `config.json` 保存 API key、Cookie、UIFID、Chrome 密钥、平台 token 或会话 ID。模型和消息平台认证在你自己的 Agent/cc-connect 中配置。`CC_PROJECT`、`CC_SESSION_KEY` 只由当前运行会话提供，不能从别人的安装示例复制。

## 手动检查队列

使用配置中相同的 Python 和本技能真实脚本路径。例如在仓库根：

```sh
./.venv/bin/python -B scripts/dy_author.py --help
./.venv/bin/python -B scripts/dy_author.py init
```

`init` 会创建运行目录。下一步由 Agent 将**当前带显式命令前缀的请求**写入返回的 `input_file`，再执行 `plan`。不要直接拿 README 的占位作品 ID 运行，也不要为裸分享补前缀冒充用户授权。

`plan/prepare-all/catalog-search/commit/skip/fail/progress/status` 的 `--run` 一律取实际返回值，作品 `--id` 取该批次实际 ID。`--config` 是全局参数，放在子命令前；首次配置通常无需传它。

## 更新与卸载

- 更新前保留本地配置和未完成 `.cache`，不要覆盖运行快照。依赖变更后重新安装版本清单并运行本地测试/doctor。
- 配置器拒绝覆盖旧安装。重新配置前自行备份并检查旧 `config.json` 和命令，明确移除旧安装入口后再生成；不要删除已保存笔记。
- 卸载只移除你安装的 `.claude/skills/dy-author` 链接与 `.claude/commands/dy-author.md`，再按需归档仓库和缓存。不要对 Vault 运行清理命令。
