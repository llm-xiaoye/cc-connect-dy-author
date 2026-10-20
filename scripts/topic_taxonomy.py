"""Shared topic tree (copied from douyin-to-obsidian) plus the dy-author question bank.

The folder says what problem a note answers.  Source, format, and interview
context are metadata, never physical categories — except the dedicated
``知识库/面试题库`` root, which only dy-author writes.
"""
from __future__ import annotations

import re

ROOT = "知识库"
QUESTION_ROOT = "面试题库"
AGENT = ("基础与架构", "记忆管理", "上下文管理", "工具与技能", "规划与控制", "质量与安全", "实践与案例")
MEMORY = ("体系与分层", "写入与更新", "检索与调用", "遗忘与评测")
CONTEXT = ("组织与注入", "压缩与摘要", "长任务与恢复")
RAG = ("数据准备", "检索与重排", "生成与评测", "Agentic RAG")
# Starting points only; the model may add first-level folders when none fit.
QUESTION_TOPICS = ("大模型基础", "Prompt与推理", "RAG", "Agent", "训练与微调", "推理与部署",
                   "评测与安全", "系统设计与工程", "项目与表达")
BAD_PART = re.compile(r'[\\/:*?"<>|\[\]#^\x00-\x1f]')
SOURCE_WORDS = ("抖音", "KI", "DY", "视频", "文章", "图文")


def validate_category(category: list[str]) -> list[str]:
    if not isinstance(category, list) or not 2 <= len(category) <= 4:
        raise ValueError("主题分类须为 2–4 级")
    if any(not isinstance(part, str) or not part or part != part.strip()
           or BAD_PART.search(part) or part.startswith(".")
           for part in category):
        raise ValueError("主题目录名无效")
    if category[0] == QUESTION_ROOT:
        raise ValueError("知识笔记不能放入面试题库；面试题目文档单独提交")
    if category[0] == ROOT or any(part in ("抖音", "KI", "DY", "视频", "文章", "面试") for part in category):
        raise ValueError("来源、媒体形式与面试不能作为主题目录")
    if category[:2] == ["AI", "Agent"]:
        if len(category) < 3 or category[2] not in AGENT:
            raise ValueError("Agent 须使用固定的七个主题主干")
        if len(category) == 4:
            allowed = {"记忆管理": MEMORY, "上下文管理": CONTEXT,
                       "基础与架构": ("概念与组成", "框架与编排", "多智能体协作"),
                       "工具与技能": ("工具调用", "Skill设计", "MCP与集成"),
                       "规划与控制": ("任务规划", "循环与停止", "失败恢复与回滚"),
                       "质量与安全": ("评测与观测", "安全与权限"),
                       "实践与案例": ("项目实现", "产品与行业")}
            if category[3] not in allowed[category[2]]:
                raise ValueError("Agent 子主题不在固定目录中")
    if category[:2] == ["AI", "RAG"] and len(category) >= 3:
        if category[2] not in RAG:
            raise ValueError("RAG 须使用固定的四个主题主干")
        if len(category) == 4:
            allowed = {"数据准备": ("解析与切片", "索引与更新"),
                       "检索与重排": ("向量与混合检索", "重排与性能"),
                       "生成与评测": ("答案质量", "评测方法"), "Agentic RAG": ()}
            if category[3] not in allowed[category[2]]:
                raise ValueError("RAG 子主题不在固定目录中")
    return category


def validate_question_category(category: list[str]) -> list[str]:
    """面试题库/<一级>/<子方向>: exactly two levels below the root; folders may be new."""
    if not isinstance(category, list) or len(category) != 3 or category[0] != QUESTION_ROOT:
        raise ValueError("面试题目录须为 [\"面试题库\", 一级, 子方向] 三段")
    for part in category[1:]:
        if (not isinstance(part, str) or not part or part != part.strip() or len(part) > 24
                or BAD_PART.search(part) or part.startswith(".")):
            raise ValueError("面试题目录名必须简短，不得含路径符号或隐藏目录")
        if part in SOURCE_WORDS or part in ("面试", QUESTION_ROOT, ROOT):
            raise ValueError("来源、媒体形式不能作为面试题目录")
    return category
