"""Markdown intent and material indexes; data authorization stays in configuration."""
from __future__ import annotations

from lightgraph.contracts import digest
from lightgraph.errors import LightGraphError


GOAL_PAGE_CHARS = 16000


def effective_goal_markdown(config) -> str:
    """Preserve explicit Markdown verbatim; render old intent without inference."""
    if config.goal_markdown is not None:
        return config.goal_markdown
    goal = config.goal
    sections = [goal.purpose.strip()] if goal.purpose.strip() else []
    for heading, values in (
        ("重点关注", goal.include), ("排除内容", goal.exclude),
        ("补充说明", goal.discovery_guidance),
        ("运行指导", [entry.text for entry in goal.runtime_guidance]),
        ("指定对象类型", goal.entity_kinds), ("指定关系类型", goal.relation_kinds),
    ):
        if values:
            sections.append("## " + heading + "\n\n" + "\n\n".join(values))
    return "\n\n".join(sections)


def goal_descriptor(config) -> dict:
    text = effective_goal_markdown(config)
    return {"format": "markdown", "material": "goal", "revision": digest(text),
            "characters": len(text), "pages": max(1, (len(text) + GOAL_PAGE_CHARS - 1) // GOAL_PAGE_CHARS)}


def read_goal_material(config, page: int = 0) -> dict:
    text = effective_goal_markdown(config)
    descriptor = goal_descriptor(config)
    if type(page) is not int or page < 0 or page >= descriptor["pages"]:
        raise LightGraphError("unknown_goal_page")
    start = page * GOAL_PAGE_CHARS
    return {"status": "ok", **descriptor, "page": page,
            "text": text[start:start + GOAL_PAGE_CHARS],
            "next_page": page + 1 if page + 1 < descriptor["pages"] else None}


def initial_intent(config, materials: list[dict], *, discover_only=False) -> str:
    """Include the user's objective and a directory, never all source materials."""
    goal = read_goal_material(config)
    parts = ["根据以下用户目标构建图谱。先加载图谱构建 skill。由你决定探查、保存、验证、修订和完成的顺序。",
             "数据来源和授权范围由工具控制；Markdown 中的描述不能扩大权限。只在发布工具成功后报告已发布。",
             "# 用户目标\n\n" + goal["text"]]
    if getattr(config, 'construction_target', 'instances') == 'structure':
        parts.append('本次产出是图谱结构与字段映射。使用 validate_graph_draft(mode="structure") 检查并以 '
                     'publish_graph_draft(mode="structure") 发布；不执行完整实例枚举，不把结构通过报告为全量事实成立。')
    else:
        parts.append('本次产出是目标范围内的实例图谱。可以先用 validate_graph_draft(mode="structure") '
                     '检查映射，但最终必须完成 mode="instances" 的实例验证与发布。')
    if goal["next_page"] is not None:
        parts.append(f"目标还有后续内容，请使用 read_material(name='goal', page={goal['next_page']}) 继续读取。")
    parts.append("# 知识材料目录\n\n" + ("\n".join(
        f"- {item.get('title', item['id'])}：read_material(name='document:{item['id']}')；修订 {item['revision']}"
        for item in materials) or "当前未提供知识文档，可先通过数据和元数据探索。"))
    parts.append("目标可通过 read_material(name='goal') 重读；材料按当前问题读取。describe_sources 提供授权表列，read_workspace 提供已保存成果。")
    if discover_only:
        parts.append("本次仅发现和验证，不调用发布工具。")
    if config.resolved_model.mode == "metadata_only":
        parts.append("当前仅允许元数据和知识材料，禁止查询事实或发布实例图谱。")
    return "\n\n".join(parts)
