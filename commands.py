"""子命令注册表与卡片构建。

命令表是「单一事实源」：``/mh help`` 的文本、参数校验、未知子命令的提示
全部从这张表生成。以后加 ``/mh 配装``、``/mh 技能`` 只需在 COMMANDS 里追加一项，
不必改命令解析、帮助文本或渲染入口。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .data import MaterialInfo, MonsterInfo, QuestInfo, Snapshot
from .render import Card, Line, Section

# 每只怪物最多显示几条获取方式（与 render 里的上限不同：那是怪物数上限）
MAX_ENTRIES_PER_MONSTER = 6
MAX_MONSTERS = 8
MAX_QUESTS = 5
MAX_GATHERING_MAPS = 6
MAX_GATHERING_PER_MAP = 4


@dataclass
class CommandResult:
    """一次子命令执行的结果。"""

    card: Card | None = None
    text: str | None = None


@dataclass
class Command:
    """一个子命令。"""

    name: str
    usage: str
    summary: str
    handler: Callable[[Snapshot, str], CommandResult]
    examples: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# 卡片构建
# --------------------------------------------------------------------------


def build_material_card(snapshot: Snapshot, material: MaterialInfo) -> Card:
    """素材卡：每只怪物一个分区 + 可刷任务。"""
    subtitle_parts = [f"稀有度 {material.rarity}"]
    if material.item_type:
        subtitle_parts.append(_type_label(material.item_type))
    if material.monster_count:
        subtitle_parts.append(f"来源怪物 {material.monster_count} 只")
    else:
        # 没有怪物掉落来源：多半是野外采集，明确写出来，避免读成「数据缺失」
        subtitle_parts.append("非怪物掉落")
    if material.is_rare:
        subtitle_parts.append("稀有素材")
    subtitle = " · ".join(subtitle_parts)

    sections: list[Section] = []
    groups = material.grouped_by_monster(MAX_MONSTERS)

    for group in groups:
        lines: list[Line] = []
        for entry in group["entries"][:MAX_ENTRIES_PER_MONSTER]:
            rank = entry.get("rank_label") or ""
            part = entry.get("part_name")
            if part:
                # 部位破坏：写明是哪个部位，否则同一只怪的多行看起来一模一样
                kind = f"{part}破坏"
            else:
                kind = entry.get("kind_label") or ""
            quantity = entry.get("quantity") or 0
            chance = entry.get("chance") or 0
            segments = [
                (f"{rank} · ", "rank"),
                (f"{kind} ", "body"),
                (f"{quantity}个 ", "body"),
                (f"{_fmt_chance(chance)}", "chance"),
            ]
            lines.append(Line.of(*segments))
        hidden = len(group["entries"]) - MAX_ENTRIES_PER_MONSTER
        footer_parts = []
        if hidden > 0:
            footer_parts.append(f"另有 {hidden} 条获取方式")
        # 标出这只怪去哪些地图打（来自栖息地数据）
        maps = material.maps_for(group["name"])
        if maps:
            shown = "、".join(maps[:5])
            more = f" 等 {len(maps)} 张" if len(maps) > 5 else ""
            footer_parts.append(f"出现于 {shown}{more}")
        sections.append(
            Section(
                title=group["name"],
                lines=lines,
                highlight=material.is_rare,
                footer=" · ".join(footer_parts),
            )
        )

    # 可刷任务：走任务目标报酬推导
    tops, total = snapshot.top_quests_dropping(material, limit=MAX_QUESTS)
    if tops:
        lines = []
        for quest in tops:
            tag = "【活动】" if quest["is_event"] else ""
            level = quest["level"] or ""
            lines.append(
                Line.of(
                    (tag, "event"),
                    (quest["name"], "body"),
                    (f"  {level}", "rank"),
                    (f"  {quest['map'] or '未知地图'}", "muted"),
                    (f"  打{quest['monster']} ", "muted"),
                    (f"{_fmt_chance(quest['chance'])}", "chance"),
                )
            )
        footer = (
            f"共 {total} 个任务可刷，此处列出概率最高的 {len(tops)} 个"
            if total > len(tops)
            else f"共 {total} 个任务可刷"
        )
        sections.append(Section(title="可刷任务", lines=lines, footer=footer))

    # 任务通用素材报酬（只覆盖小部分消耗品，有才显示）
    reward_tops, reward_total = snapshot.top_quests_rewarding(
        material.item_id, limit=MAX_QUESTS
    )
    if reward_tops:
        lines = []
        for quest in reward_tops:
            tag = "【活动】" if quest["is_event"] else ""
            lines.append(
                Line.of(
                    (tag, "event"),
                    (quest["name"], "body"),
                    (f"  {quest['level']}", "rank"),
                    (f"  {quest['map'] or '未知地图'}", "muted"),
                    (f"  {_fmt_chance(quest['chance'])}", "chance"),
                )
            )
        sections.append(
            Section(
                title="任务报酬",
                lines=lines,
                footer=f"共 {reward_total} 个任务的结算奖励含该素材",
            )
        )

    # 采集点（来自 Kiranico，仅部分素材有）
    map_groups = material.gathering_by_map(MAX_GATHERING_MAPS)
    if map_groups:
        lines = []
        for group in map_groups:
            entries = group["entries"][:MAX_GATHERING_PER_MAP]
            detail = "  ".join(
                f"{point.get('rank') or ''} {point.get('chance') or ''}".strip()
                for point in entries
            )
            extra = (
                f" 等{len(group['entries'])}条"
                if len(group["entries"]) > len(entries)
                else ""
            )
            lines.append(
                Line.of(
                    (group["map"], "body"),
                    (f"   {detail}", "chance"),
                    (extra, "muted"),
                )
            )
        sections.append(
            Section(
                title="采集点",
                lines=lines,
                footer=f"共 {len(material.gathering)} 条采集记录",
            )
        )

    if not groups and not sections:
        sections.append(
            Section(
                title="暂无掉落记录",
                lines=[
                    Line.of(("源数据里没有该素材的怪物掉落或任务报酬记录。", "muted")),
                    Line.of(("它通常来自野外采集、交易或特定获取途径。", "muted")),
                ],
            )
        )
    elif not groups:
        # 没有怪物掉落，但有任务报酬/任务来源，提示一下免得用户以为信息缺失
        sections.append(
            Section(
                title="说明",
                lines=[
                    Line.of(
                        ("该素材没有怪物掉落记录，获取途径见上面的任务信息。", "muted")
                    )
                ],
            )
        )

    return Card(
        title=material.name,
        subtitle=subtitle,
        sections=sections,
        footnote=_footnote(snapshot),
    )


def build_monster_card(snapshot: Snapshot, monster: MonsterInfo) -> Card:
    """怪物卡：按难度分组展示掉落表。小动物额外标注出现地图。"""
    subtitle_parts = [f"{len(monster.drops)} 条掉落记录"]
    if monster.is_small:
        subtitle_parts.insert(0, "小动物")
    if monster.alias:
        subtitle_parts.append(monster.alias)
    subtitle = " · ".join(subtitle_parts)

    sections: list[Section] = []

    # 小动物的出现地图放在最上面——「去哪找它」比掉落率更常用
    if monster.is_small:
        if monster.map_names:
            lines = []
            for index in range(0, len(monster.map_names), 4):
                lines.append(
                    Line.of(("、".join(monster.map_names[index : index + 4]), "body"))
                )
            sections.append(Section(title="出现地图", lines=lines))
        else:
            sections.append(
                Section(
                    title="出现地图",
                    lines=[Line.of(("源数据里没有记录栖息地图。", "muted"))],
                )
            )

    for rank, entries in monster.grouped_by_rank_then_kind():
        rank_label = {"Low": "下位", "High": "上位", "Master": "大师"}.get(
            rank, rank or "未知"
        )
        lines = []
        for entry in entries[:14]:
            segments = [
                (f"{entry.get('kind_label') or ''} ", "body"),
                (f"{entry.get('quantity') or 0}个 ", "body"),
                (f"{_fmt_chance(entry.get('chance'))}", "chance"),
                (f"  {_item_name(snapshot, entry)}", "muted"),
            ]
            lines.append(Line.of(*segments))
        hidden = len(entries) - 14
        sections.append(
            Section(
                title=rank_label,
                lines=lines,
                footer=f"另有 {hidden} 条" if hidden > 0 else "",
            )
        )

    if not sections:
        sections.append(
            Section(
                title="暂无数据", lines=[Line.of(("没有查到该怪物的掉落。", "muted"))]
            )
        )

    return Card(
        title=monster.name,
        subtitle=subtitle,
        sections=sections,
        footnote=_footnote(snapshot),
    )


def build_quest_card(snapshot: Snapshot, quest: QuestInfo) -> Card:
    """任务卡：基本属性 + 奖励明细。"""
    parts = []
    if quest.enemy_level_label:
        parts.append(quest.enemy_level_label)
    if quest.level:
        parts.append(f"{quest.level}（★{quest.level_num}）")
    if quest.quest_type:
        parts.append(quest.quest_type)
    if quest.map_name:
        parts.append(quest.map_name)
    if quest.is_event:
        parts.append("活动任务")
    subtitle = " · ".join(parts)

    sections = []
    if quest.bosses:
        lines = []
        for index in range(0, len(quest.bosses), 4):
            chunk = "、".join(quest.bosses[index : index + 4])
            lines.append(Line.of((chunk, "body")))
        sections.append(Section(title="出场怪物", lines=lines))

    if quest.rewards:
        lines = []
        # 按概率降序，稀有素材高亮
        for reward in sorted(quest.rewards, key=lambda r: -float(r.get("chance") or 0))[
            :20
        ]:
            style = "accent" if int(reward.get("rarity") or 0) >= 7 else "body"
            lines.append(
                Line.of(
                    (reward.get("name") or "", style),
                    (f"  {reward.get('quantity') or 0}个 ", "body"),
                    (f"{_fmt_chance(reward.get('chance'))}", "chance"),
                )
            )
        hidden = len(quest.rewards) - 20
        sections.append(
            Section(
                title="结算奖励",
                lines=lines,
                footer=f"另有 {hidden} 项" if hidden > 0 else "",
            )
        )
    else:
        sections.append(
            Section(
                title="结算奖励",
                lines=[Line.of(("该任务没有记录到结算奖励。", "muted"))],
            )
        )

    return Card(
        title=quest.name,
        subtitle=subtitle,
        sections=sections,
        footnote=_footnote(snapshot),
    )


# --------------------------------------------------------------------------
# 各子命令的处理函数
# --------------------------------------------------------------------------


def handle_material(snapshot: Snapshot, argument: str) -> CommandResult:
    if not argument:
        return CommandResult(text="用法：/mh 素材 <素材名>\n例如：/mh 素材 龙玉")

    matches = snapshot.search_materials(argument, limit=12)
    if not matches:
        return CommandResult(
            text=f"没有找到名为「{argument}」的素材。可以只输入名字的一部分再试试。"
        )

    # 精确命中一条就直接出卡
    exact = [m for m in matches if m.score == 0]
    if len(exact) == 1:
        material = snapshot.material(exact[0].key)
        if material:
            return CommandResult(card=build_material_card(snapshot, material))

    if len(matches) == 1:
        material = snapshot.material(matches[0].key)
        if material:
            return CommandResult(card=build_material_card(snapshot, material))

    # 多条候选：列出来让用户挑
    lines = [f"「{argument}」匹配到 {len(matches)} 个素材，请回复更具体的名字："]
    for match in matches:
        material = snapshot.material(match.key)
        suffix = f"（稀有度 {material.rarity}）" if material else ""
        note = f"  {match.note}" if match.note else ""
        lines.append(f"· {match.name}{suffix}{note}")
    lines.append("")
    lines.append("例如：/mh 素材 " + matches[0].name)
    return CommandResult(text="\n".join(lines))


def handle_monster(snapshot: Snapshot, argument: str) -> CommandResult:
    if not argument:
        return CommandResult(text="用法：/mh 怪物 <怪物名>\n例如：/mh 怪物 爵银龙")

    matches = snapshot.search_monsters(argument, limit=12)
    if not matches:
        return CommandResult(
            text=f"没有找到名为「{argument}」的怪物。可以只输入名字的一部分再试试。"
        )

    exact = [m for m in matches if m.score == 0]
    if len(exact) == 1 or len(matches) == 1:
        target = exact[0] if exact else matches[0]
        monster = snapshot.monster(target.key)
        if monster:
            return CommandResult(card=build_monster_card(snapshot, monster))

    lines = [f"「{argument}」匹配到 {len(matches)} 只怪物，请回复更具体的名字："]
    for match in matches:
        note = f"  {match.note}" if match.note else ""
        lines.append(f"· {match.name}{note}")
    lines.append("")
    lines.append("例如：/mh 怪物 " + matches[0].name)
    return CommandResult(text="\n".join(lines))


def handle_quest(snapshot: Snapshot, argument: str) -> CommandResult:
    if not argument:
        return CommandResult(text="用法：/mh 任务 <任务名>\n例如：/mh 任务 撕裂寂静者")

    matches = snapshot.search_quests(argument, limit=12)
    if not matches:
        return CommandResult(
            text=f"没有找到名为「{argument}」的任务。可以只输入名字的一部分再试试。"
        )

    exact = [m for m in matches if m.score == 0]
    if len(exact) == 1 or len(matches) == 1:
        target = exact[0] if exact else matches[0]
        quest = snapshot.quest(int(target.key))
        if quest:
            return CommandResult(card=build_quest_card(snapshot, quest))

    lines = [f"「{argument}」匹配到 {len(matches)} 个任务，请回复更具体的名字："]
    for match in matches:
        lines.append(f"· {match.name}")
    lines.append("")
    lines.append("例如：/mh 任务 " + matches[0].name)
    return CommandResult(text="\n".join(lines))


def handle_help(snapshot: Snapshot, argument: str) -> CommandResult:
    return CommandResult(text=render_help(snapshot))


# --------------------------------------------------------------------------
# 命令表
# --------------------------------------------------------------------------

COMMANDS: list[Command] = [
    Command(
        name="素材",
        usage="/mh 素材 <素材名>",
        summary="查询素材的获取方式（怪物、部位/方式、概率、数量、地图、任务）",
        handler=handle_material,
        examples=("/mh 素材 龙玉", "/mh 素材 火龙的天鳞"),
    ),
    Command(
        name="怪物",
        usage="/mh 怪物 <怪物名>",
        summary="查询某只怪物的完整掉落表",
        handler=handle_monster,
        examples=("/mh 怪物 爵银龙",),
    ),
    Command(
        name="任务",
        usage="/mh 任务 <任务名>",
        summary="查询某任务的星级、地图、出场怪物与结算奖励",
        handler=handle_quest,
        examples=("/mh 任务 撕裂寂静者",),
    ),
    Command(
        name="help",
        usage="/mh help",
        summary="显示这份帮助",
        handler=handle_help,
        examples=("/mh help",),
    ),
]

COMMAND_INDEX: dict[str, Command] = {
    command.name.lower(): command for command in COMMANDS
}


def render_help(snapshot: Snapshot) -> str:
    """由命令表生成帮助文本，避免手写帮助与实现脱节。"""
    lines = ["怪物猎人素材查询 —— 可用指令", ""]
    for command in COMMANDS:
        lines.append(command.usage)
        lines.append(f"    {command.summary}")
        if command.examples:
            lines.append(f"    示例：{command.examples[0]}")
        lines.append("")

    stats = snapshot.stats()
    lines.append(
        f"数据：{stats['materials']} 种素材 / {stats['monsters']} 只怪物 / {stats['quests']} 个任务"
    )
    sample = snapshot.rare_material_names(3)
    if sample:
        lines.append("试试：" + "、".join(f"/mh 素材 {name}" for name in sample))
    lines.append("")
    lines.append("查询用「精确 → 别名 → 包含」逐级匹配；命中多条时会列出候选让你挑。")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


def _fmt_chance(chance) -> str:
    """概率显示：整数不带小数点，小数保留一位。"""
    try:
        value = float(chance)
    except (TypeError, ValueError):
        return ""
    if value == int(value):
        return f"{int(value)}%"
    return f"{value:.1f}%"


def _item_name(snapshot: Snapshot, entry: dict) -> str:
    """从一条掉落记录里取素材名。

    快照里怪物的 drops 记录只带 item_id（不带名字），所以这里按 id 反查；
    查不到时退回记录里可能存在的 name 字段。
    """
    item_id = entry.get("item_id")
    if item_id is not None:
        item = snapshot.items.get(str(item_id))
        if item and item.get("name"):
            return item["name"]
    return entry.get("name") or ""


# 素材类型的中文标签（覆盖 items.json 里实际出现的类型）
TYPE_LABELS = {
    "Material": "素材",
    "OffcutsMaterial": "边角料",
    "Antique": "古董",
    "PayOff": "换金道具",
    "CarryPayOff": "搬运换金道具",
    "Consume": "消耗品",
    "Bullet": "弹药",
    "Bottle": "瓶",
    "Tool": "道具",
    "Weapon": "武器",
    "Armor": "防具",
}


def _type_label(item_type: str) -> str:
    if not item_type:
        return ""
    return TYPE_LABELS.get(item_type, item_type)


def _footnote(snapshot: Snapshot) -> str:
    """卡片底部的数据来源标注。

    地图 12/13 是推断值，这里如实说明，避免用户以为是官方数据。
    """
    parts = []
    inferred_maps = [
        entry["name"]
        for entry in snapshot.maps.values()
        if entry.get("origin") == "inferred"
    ]
    if inferred_maps:
        parts.append("地图 " + "、".join(inferred_maps) + " 为推断值")
    if snapshot.generated_at:
        parts.append("数据更新于 " + snapshot.generated_at[:10])
    return " · ".join(parts)


def dispatch(snapshot: Snapshot, argument_text: str) -> CommandResult:
    """解析 ``/mh`` 后面的参数并执行。"""
    text = (argument_text or "").strip()
    if not text:
        return handle_help(snapshot, "")

    parts = text.split(maxsplit=1)
    name = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    command = COMMAND_INDEX.get(name)
    if command is None:
        # 未知子命令：提示并附上帮助，比单纯报错有用
        return CommandResult(
            text=f"未知的子命令「{parts[0]}」。\n\n" + render_help(snapshot)
        )
    return command.handler(snapshot, rest)
