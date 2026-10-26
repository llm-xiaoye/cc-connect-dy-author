# dy-author：把抖音作者作品整理进 Obsidian

面向 **cc-connect + Claude Code 等可读写本地文件、执行 Python 的 Agent** 的独立 Skill。发送一次 `/dy-author 作品链接`，从该作品识别作者，分页获取公开作品，准备完整视频转写或图文材料，由当前 Agent 提炼知识并逐条存入自己的 Obsidian Vault。

**语义提炼和笔记写作由当前 Agent 完成**；本仓库脚本负责材料获取、队列、查重、校验和事务保存。安装与使用流程均在本仓库中提供。

## 能做什么

- 以一条作品识别作者，收集其公开视频与图文，按作品 ID 查重；每批最多十条，每条完成立即保存。
- 从真实音轨做本地 Whisper 转写；图文下载完整图片并用 macOS Vision OCR，Agent 再逐页看图。
- 生成知识笔记、维护作者文档、知识总索引与导入总日志；保留已有笔记和“我的补充”。
- 对原始材料含“面试”的作品生成/合并面试题；`/dy-author 补录` 为已有面试类知识笔记补建题库。
- 保存运行检查点；后续条目失败或模型额度耗尽不回滚已经成功的作品。
- 可向**明确绑定的当前 cc-connect 会话**发批次进度；发送失败不影响保存，不自动重发未知送达结果。

普通作品链接不会授权全作者导入。只有当前消息显式使用 `/dy-author` 或 `$dy-author` 才启动本技能。

## 支持环境与前提

完整媒体路线面向 **macOS Apple Silicon（M 系列）+ Python 3.12**。本地 ASR 使用 MLX，图文 OCR 使用 Swift/Vision；Windows、Linux、Intel Mac 的完整流程目前不承诺支持。需要 Apple Command Line Tools（含 Swift），以及可用的 Google Chrome profile。首次转写会从 Hugging Face 下载模型，需要网络和本地磁盘空间。FFmpeg 可来自系统，或依赖中的 `imageio-ffmpeg`。

先安装并运行自己的 [cc-connect](https://github.com/chenhg5/cc-connect)，配置好 Agent、模型账号、消息平台和项目的实际 `work_dir`。不同 Agent 适配器对技能/斜杠命令的支持有差异；本包提供 `.claude` 布局的命令集成，其他适配器需按其实际技能发现机制安装。

没有 Chrome 访问也可用 `--no-browser` 配置，但匿名请求可能拿不到完整作品；不会承诺绕过登录、验证码、私密或删除限制。首次使用建议选一个小作者，并先备份 Vault。

## 1. 下载并安装依赖

```sh
git clone https://github.com/llm-xiaoye/cc-connect-dy-author.git
cd cc-connect-dy-author

# 将此路径替换为你电脑上 Python 3.12 的真实绝对路径。
/absolute/path/to/python3.12 -m venv .venv
./.venv/bin/python -m pip install -r requirements.lock.txt
```

也可以在 GitHub 点 **Code → Download ZIP** 后解压，再在解压目录执行后两步。不要复制开发者的虚拟环境，不要把仓库放进会同步公开的 Vault 内。

`requirements.lock.txt` 保留原功能所用版本，包含固定 commit 与摘要的 F2 源码依赖；需要访问 PyPI/GitHub。它是版本固定清单，**不是全包 wheel hash 锁**，不要加 `--require-hashes`。依赖安装失败时保留错误，不把它当成作品或技能处理失败；不建议任意升级 F2、yt-dlp 或 MLX 后仍假定行为不变。

## 2. 生成你自己的配置与 cc-connect 命令

在仓库根执行，替换下面两个路径。`--cc-workspace` 是 cc-connect 项目的 **work_dir**，不是程序安装目录或 `.claude` 目录。

```sh
./.venv/bin/python scripts/configure.py \
  --vault "/absolute/path/to/your/ObsidianVault" \
  --cc-workspace "/absolute/path/to/your/cc-connect-workspace"
```

配置器会只读寻找真实系统用户的 Chrome 资料，优先复用已有的抖音会话；zcode 改写 HOME 不影响这个查找。缺少会话时提示你手动打开抖音登录，再重试。关闭登录页面通常不会清除会话，无需一直保持页面打开。读取失败会说明权限或路径问题，不等于账号已经退出。

1. 验证 Vault 与显式提供的路径，不新建 Chrome 资料。
2. 生成权限为 `0600`、被 Git 忽略的 `config.json`，记录当前虚拟环境入口；默认 `browser_profile=null` 自动识别、`auto_browser_recovery=false`。获取作者清单及下载作品默认不弹出页面。
3. 在 work_dir 创建 `.claude/skills/dy-author` 链接与 `.claude/commands/dy-author.md`。

已有配置、同名技能或命令时拒绝覆盖。更新时保留个人配置、虚拟环境与缓存。旧安装升级后，请将当前 `config.json` 的 `auto_browser_recovery` 设为 `false`；这一关闭设置也约束旧批次。其他运行参数仍使用创建时的快照，不删除旧批次或已保存笔记。

需要固定资料时，用 `chrome://version` 的 **Profile Path** 填入 `--chrome-profile`。标准 Chrome 缓存自动匹配；自定义 user-data 目录还需 `--chrome-cache`。只有明确希望可见浏览器恢复时才手动启用 `auto_browser_recovery=true`。请只授权自己的资料，不粘贴或提交 Cookie。

只安装 Skill、不创建 cc-connect 命令时省略 `--cc-workspace`。无浏览器模式用 `--no-browser` 代替 Chrome 参数。手动配置可参考 [config.example.json](config.example.json)，实际配置必须保存为本仓库的 `config.json`，所有路径换成你自己的绝对路径。字段说明见 [docs/configuration.md](docs/configuration.md)。

## 3. 可选：每批进度主动提示

若需要在长任务中主动收到每批十条的提示，在**首次配置**时再加：

```sh
  --cc-binary "/absolute/path/to/cc-connect" \
  --cc-data-dir "/absolute/path/to/the/running-service-data"
```

这两个参数须同时指定；data-dir 必须与正在运行的 cc-connect 服务一致。已配置者可手动更新本地 `config.json` 的 `cc_binary` 与 `cc_data_dir`。不要把这些参数单独当成一条 shell 命令执行。

运行时由 cc-connect 提供当前 `CC_PROJECT` 和 `CC_SESSION_KEY`，技能不会猜测其他活跃会话。不要把示例或别人的会话 ID 写死到环境。没有会话绑定时由正常 Agent 回复展示进度；没有主动提示不影响笔记保存。

## 4. 检查安装并开始用

```sh
./.venv/bin/python -B scripts/doctor.py
```

此检查只检查本地路径、平台和依赖模块发现，不联网、不下载模型、不写 Vault，也不证明抖音接口可用。完成后让 cc-connect/Agent 刷新技能发现或开启新会话。在连接的聊天中发送：

```text
/dy-author https://www.douyin.com/video/作品ID
```

也可以 `/dy-author` 后直接粘贴**完整分享文本**，其中必须恰好一条官方作品链接。不要先删掉 `/dy-author`。补录命令：

```text
/dy-author 补录
```

期望流程是：识别作者 → 翻页列作品 → 查重 → 每批准备材料 → 当前 Agent 写笔记/题目 → 逐条提交 → 继续下一批 → 最终汇报。**不要把 `scripts/dy_author.py` 的一次运行误当作整套 AI 写作**；它是供 Agent 驱动的队列接口。细节见 [references/workflow.md](references/workflow.md)。

输出通常位于你的 Vault：

```text
知识库/<主题分类>/<标题>.md
知识库/面试题库/<一级>/<子方向>/<题目>.md
抖音作者/<昵称>.md
知识总索引.md
导入总日志.md
附件/...
```

运行材料、转写、草稿和状态位于本地 `.cache/runs/<run_id>/` 等目录。它们可能包含个人学习记录及源作品内容，不能上传。中断后优先保留同一 run_dir 与检查点；新发同一显式命令会建立新批次，并排除已有/已跳过作品。

## 限制与常见问题

- **接口 403/429、翻页中断**：会记录清单不完整；已获取作品可继续入库，不能称“全部”。不反复刷新，不编造签名。
- **抖音长文字文章（type 163）**：目前只列清单、标记暂不支持，不拿背景音乐冒充正文。视频和图文保留原支持范围。
- **需要登录/Keychain 权限**：只在自己的 Chrome 完成必要交互。macOS 可能询问读取 Chrome 密钥/控制 Chrome 的权限；不愿授权时用无浏览器路线并接受其限制。
- **下载成功但没笔记**：Agent 仍需读取完整材料并写作/commit；标题、封面和摘要不足以入库。
- **命令没出现**：检查安装的 work_dir 与当前会话一致、`.claude` 适配器支持情况、安装路径是否移动，然后刷新/重启会话。
- **运行出错**：保留原批次；先检查具体阶段。浏览器缓存扫描不完整不能判为作品不可提取。恢复说明见 [references/recovery.md](references/recovery.md)。

只有清单完整、全部处理、无失败且无暂不支持时，才能称作者“全部作品已入库”。这里只处理账号可访问的公开内容；请遵守平台条款和内容权利。

## 隐私、开发与许可证

发布包只含技能、源代码、规则、测试和通用示例；个人配置与缓存被排除。数据流、反馈脱敏和权限边界见 [PRIVACY.md](PRIVACY.md)。模型服务仍可能接收到当前 Agent 为写作读取的转写、图片和所需目录/笔记摘录；本地 ASR/OCR 不等于整套流程完全离线。

```sh
./.venv/bin/python -B -m unittest discover -s tests
```

这些是本地回归，使用临时 Vault 与受控材料，不发真实抖音请求或主动聊天通知；它们不能保证未来平台接口一直可用。设计和保留的行为边界见 [docs/design.md](docs/design.md)。代码采用 [MIT License](LICENSE)；第三方依赖按各自许可证使用，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
