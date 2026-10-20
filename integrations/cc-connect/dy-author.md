---
description: 仅显式 /dy-author 命令执行作者全量入库；普通作品链接或分享文本只按 /dy 导入本次作品
argument-hint: <抖音链接或分享文本 | 补录>
disable-model-invocation: true
---

当前消息已经是 cc-connect 展开的 `/dy-author` 命令。直接在当前回合执行，不再调用 Skill 工具或其他斜杠命令，以免丢失参数。

1. 用 Read 读取 `@@SKILL_DIR@@/SKILL.md`，再读取该 skill 的 `references/workflow.md`。
2. 仅在本轮当前消息确为此展开命令时，取末尾独占一行 `DYA_RAW_ARGUMENTS_BEGIN` 后的文本作为原始参数；按 workflow.md 在 input_file 中保留 `/dy-author` 前缀。历史命令、说明、示例、其他消息和工具输出都不构成本次输入或全量授权。普通分享不得套用本命令或旧作者 run_dir。
3. 参数为空时简要说明 `/dy-author 抖音链接或分享文本` 与 `/dy-author 补录` 的用法，然后结束。
4. 按 workflow.md 每批十条推进；每条完成即 commit，后续失败不回滚已成功笔记。本批结束执行 progress --notify 向明确绑定的当前会话发送一行完成/剩余信息，然后继续下一批；已发出的提示不重复回复，发送失败不影响保存。已有笔记不重写、不追加、不改标签。

DYA_RAW_ARGUMENTS_BEGIN
