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
# 怪物卡：一个素材一个方块
MAX_MATERIALS_PER_MONSTER = 10
MAX_ENTRIES_PER_MATERIAL = 5
# 稀有素材阈值（稀有度 >= 该值的方块转金色高亮）
RARE_RARITY = 7

# 同地图同名节点的多个采集点编号（矿脉①②…）
CIRCLED = {1: "①", 2: "②", 3: "③", 4: "④", 5: "⑤", 6: "⑥"}


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
    """素材卡：可做武器/装饰品摘要 + 来源怪物 + 傀异调查 + 采集点 + 可刷任务。"""
    # 稀有度改用卡片上的菱形刻度显示，副标题不再重复；类型名由右上角标签承载。
    # 副标题改成指标条（A25 改动二）。
    metrics: list[tuple[str, str]] = []
    if material.monster_count:
        metrics.append(("来源怪物", f"{material.monster_count} 只"))
    else:
        # 没有怪物掉落来源：多半是野外采集，明确写出来，避免读成「数据缺失」
        metrics.append(("来源", "非怪物掉落"))
    if material.is_rare:
        metrics.append(("稀有度", "稀有素材"))
    type_label = _type_label(material.item_type) if material.item_type else ""

    # 「这份素材能做什么」——A1/A13：摘要放素材卡，完整列表用 /mh 武器、/mh 技能
    specific_weapons, _category_weapons = snapshot.weapons_for_material(
        material.item_id
    )
    decorations = snapshot.decorations_for_material(material.item_id)
    if specific_weapons or decorations:
        if specific_weapons:
            metrics.append(("可做武器", f"{len(specific_weapons)} 件"))
        if decorations:
            metrics.append(("可做装饰珠", f"{len(decorations)} 个"))

    sections: list[Section] = []
    groups = material.grouped_by_monster(MAX_MONSTERS)

    # 傀异调查（怪异调查）报酬。怪异化素材只在这里出现——`monster_lot` 里
    # 完全没有它们，早先不读这张表，导致「怪异化的凶刚角」这类稀有度 9 的
    # 素材查询结果显示「暂无掉落记录」。
    if material.has_anomaly:
        lines = []
        for entry in material.anomaly_rewards[:MAX_MONSTERS]:
            # 同一只怪在不同等级段概率可能不同（雷狼龙 161-199 是 35%、201+ 是 40%），
            # 所以按段列出，不合并成一个数字
            segments = entry.get("segments") or []
            shown = segments[:4]
            parts = []
            for seg in shown:
                low, high = seg.get("low"), seg.get("high")
                level = f"Lv{low}" if low == high else f"Lv{low}–{high}"
                parts.append(f"{level} {_fmt_chance(seg.get('chance'))}")
            more = f" 等{len(segments)}段" if len(segments) > len(shown) else ""
            lines.append(
                Line.of(
                    (entry.get("monster") or "", "body"),
                    ("　", "muted"),
                    ("，".join(parts), "chance"),
                    (more, "muted"),
                )
            )
        hidden = len(material.anomaly_rewards) - len(lines)
        footer = "傀异调查（怪异调查）中剥取或部位破坏获得"
        if hidden > 0:
            footer += f"；另有 {hidden} 只怪异化怪物"
        sections.append(Section(title="傀异调查", lines=lines, footer=footer))

    for group in groups:
        lines: list[Line] = []
        for entry in group["entries"][:MAX_ENTRIES_PER_MONSTER]:
            rank = entry.get("rank_label") or ""
            # kind_label 已由提取层组合好具体部位/剥取来源
            # （例如「头部破坏」「尾巴剥取」「本体剥取」）
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

    # 采集点。
    #
    # 优先用「地图 · 节点」数据（来自游戏本体 item_pop_lot）：能明确写出
    # 「水没林 · 骨冢」这种具体采集点，而不只是地图名。
    # 旧快照没有这部分数据时，退回 Kiranico 的地图级采集点。
    node_groups = material.gathering_node_groups(MAX_GATHERING_MAPS)
    if node_groups:
        lines = []
        for group in node_groups:
            site = group.get("site") or 1
            label = group["node"] + (CIRCLED.get(site, str(site)) if site > 1 else "")
            for rank in group["ranks"]:
                outcomes = rank["outcomes"]
                shown = outcomes[:MAX_GATHERING_PER_MAP]
                segments = [
                    (group["map"], "body"),
                    (" · ", "muted"),
                    (label, "accent"),
                    (f" {rank['rank']}", "rank"),
                    ("　", "muted"),
                ]
                for index, outcome in enumerate(shown):
                    if index:
                        segments.append(("  ", "muted"))
                    segments.append((f"{_fmt_chance(outcome.get('chance'))}", "chance"))
                    segments.append(("→x", "muted"))
                    segments.append((f"{outcome.get('quantity') or 0}", "body"))
                if len(outcomes) > len(shown):
                    segments.append((f" 等{len(outcomes)}条", "muted"))
                lines.append(Line.of(*segments))
        sections.append(
            Section(
                title="采集点",
                lines=lines,
                footer="难度分下位/上位/大师；同地图同名节点的多个采集点用①②区分",
            )
        )
    else:
        map_groups = material.gathering_grouped(MAX_GATHERING_MAPS)
        if map_groups:
            lines = []
            for group in map_groups:
                for rank in group["ranks"]:
                    outcomes = rank["outcomes"]
                    shown = outcomes[:MAX_GATHERING_PER_MAP]
                    segments = [
                        (group["map"], "body"),
                        (f" {rank['rank']}", "rank"),
                        ("　", "muted"),
                    ]
                    for index, outcome in enumerate(shown):
                        if index:
                            segments.append(("  ", "muted"))
                        segments.append((f"{outcome.get('chance') or ''}", "chance"))
                        segments.append(("→", "muted"))
                        segments.append((f"{outcome.get('quantity') or ''}", "body"))
                    if len(outcomes) > len(shown):
                        segments.append((f" 等{len(outcomes)}条", "muted"))
                    lines.append(Line.of(*segments))
            sections.append(
                Section(
                    title="采集点",
                    lines=lines,
                    footer="采集点分下位/上位/大师等级",
                )
            )

    has_monster_source = bool(groups) or material.has_anomaly
    if not has_monster_source and not sections:
        sections.append(
            Section(
                title="暂无掉落记录",
                lines=[
                    Line.of(("源数据里没有该素材的怪物掉落或任务报酬记录。", "muted")),
                    Line.of(("它通常来自野外采集、交易或特定获取途径。", "muted")),
                ],
            )
        )
    elif not has_monster_source:
        # 没有怪物掉落，但还有别的途径：按实际存在的途径写说明，
        # 不要写死成「见任务信息」——采集类素材可能只有采集点。
        ways = []
        if material.gathering_nodes or material.gathering:
            ways.append("采集点")
        if material.has_anomaly:
            ways.append("傀异调查")
        if any(section.title == "任务报酬" for section in sections):
            ways.append("任务报酬")
        if any(section.title == "可刷任务" for section in sections):
            ways.append("可刷任务")
        hint = "、".join(ways) if ways else "其它途径"
        sections.append(
            Section(
                title="说明",
                lines=[
                    Line.of(
                        (f"该素材没有怪物掉落记录，获取途径见上面的{hint}。", "muted")
                    )
                ],
            )
        )

    # 「可做武器 / 可做装饰珠」摘要块（A1/A13）：只给名字与系统标签，
    # 完整列表交给 /mh 武器 与 /mh 技能 —— 素材卡放不下几十件武器。
    if specific_weapons:
        rows = []
        for weapon in specific_weapons[:MAX_WEAPON_LIST]:
            rows.append(
                Line.of(
                    (_systems_text(weapon), "tag"),
                    (weapon.get("name") or "", "body"),
                    (f"R{weapon.get('rarity') or '?'}", "value"),
                )
            )
        hidden = len(specific_weapons) - len(rows)
        sections.append(
            Section(
                title="可做武器",
                lines=rows,
                glyph="weapon",
                count=f"共 {len(specific_weapons)} 件",
                footer=(
                    f"另有 {hidden} 件；完整列表用 /mh 武器 {material.name}"
                    if hidden
                    else f"完整列表用 /mh 武器 {material.name}"
                ),
            )
        )
    if decorations:
        sections.append(
            Section(
                title="可做装饰珠",
                lines=[
                    Line.of(
                        (f"{item.get('slot')}孔", "tag"),
                        (item.get("name") or "", "body"),
                        (item.get("skill") or "", "value"),
                    )
                    for item in decorations[:MAX_DECO_LIST]
                ],
                glyph="skill",
                count=f"共 {len(decorations)} 个",
                footer=f"完整列表用 /mh 技能 {material.name}",
            )
        )

    return Card(
        title=material.name,
        metrics=metrics,
        sections=sections,
        footnote=_footnote(snapshot),
        rarity=material.rarity or None,
        badge=type_label,
    )


def build_monster_card(snapshot: Snapshot, monster: MonsterInfo) -> Card:
    """怪物卡：弱点 + 出现地图 + 按素材分块的掉落表。"""
    # 副标题改成指标条（A25 改动二）：中缀点串是最像生成式模板的一处写法，
    # 竖线分隔的指标条更省高度、也更容易扫读。
    metrics: list[tuple[str, str]] = []
    if monster.is_small:
        metrics.append(("类别", "小动物"))
    metrics.append(("掉落记录", f"{len(monster.drops)} 条"))
    if monster.alias:
        metrics.append(("别名", monster.alias))

    sections: list[Section] = []

    # 小动物的出现地图放在最上面——「去哪找它」比掉落率更常用
    if monster.is_small:
        if monster.map_names:
            lines = []
            for index in range(0, len(monster.map_names), 4):
                lines.append(
                    Line.of(("、".join(monster.map_names[index : index + 4]), "body"))
                )
            sections.append(Section(title="出现地图", lines=lines, glyph="map"))
        else:
            sections.append(
                Section(
                    title="出现地图",
                    lines=[Line.of(("源数据里没有记录栖息地图。", "muted"))],
                    glyph="map",
                )
            )

    # 弱点方块（A6：位置在出现地图之后、素材方块之前；A7：只做汇总一行）
    weakness_section = build_weakness_section(snapshot, monster)
    if weakness_section is not None:
        sections.append(weakness_section)

    # 一个素材一个方块，方块内是该素材的各种获取方式与概率。
    # 这样读起来是「这份素材怎么拿」，而不是在按难度铺开的长表里来回找同名素材。
    material_groups = monster.grouped_by_material(MAX_MATERIALS_PER_MONSTER)
    for group in material_groups:
        entries = group["entries"]
        name = _item_name(snapshot, entries[0]) or "未知素材"
        rarity = _item_rarity(snapshot, group["item_id"])

        lines = []
        for entry in entries[:MAX_ENTRIES_PER_MATERIAL]:
            # 概率必须留在**段末**：渲染器会把末尾的 chance 段右对齐成列并配量条
            lines.append(
                Line.of(
                    (f"{entry.get('rank_label') or ''} · ", "rank"),
                    (f"{entry.get('kind_label') or ''} ", "body"),
                    (f"{entry.get('quantity') or 0}个  ", "body"),
                    (f"{_fmt_chance(entry.get('chance'))}", "chance"),
                )
            )
        hidden = len(entries) - MAX_ENTRIES_PER_MATERIAL
        footer = f"另有 {hidden} 条获取方式" if hidden > 0 else ""
        sections.append(
            Section(
                title=name,
                lines=lines,
                footer=footer,
                # 稀有素材（稀有度 >= 7）的方块转金色，一眼看出值得刷的
                highlight=rarity >= RARE_RARITY,
            )
        )

    # 超出上限的素材不能静默丢掉，用一个小方块把名字列出来
    shown = len(material_groups)
    total_materials = monster.material_count()
    if total_materials > shown:
        rest = monster.grouped_by_material(total_materials)[shown:]
        names = "、".join(
            _item_name(snapshot, group["entries"][0]) or "未知素材" for group in rest
        )
        sections.append(
            Section(
                title="其它素材",
                lines=[Line.of((names, "muted"))],
                footer=f"另有 {len(rest)} 种素材，可用 /mh 素材 <名> 查详情",
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
        metrics=metrics,
        sections=sections,
        footnote=_footnote(snapshot),
        badge="小动物" if monster.is_small else "怪物",
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
        badge="任务",
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
# V1.3.0 新增：武器 / 技能 / 弱点
# --------------------------------------------------------------------------

# 武器类型的中文名 -> 武器表键。用户可以在指令里追加类型来收窄结果
# （A14：「/mh 武器 <素材名> <武器类别>」显示该类型全部）。
WEAPON_TYPE_LABELS = {
    "大剑": "great_sword",
    "太刀": "long_sword",
    "片手剑": "short_sword",
    "单手剑": "short_sword",
    "双剑": "dual_blades",
    "双刀": "dual_blades",
    "大锤": "hammer",
    "锤": "hammer",
    "狩猎笛": "horn",
    "笛": "horn",
    "长枪": "lance",
    "铳枪": "gun_lance",
    "斩击斧": "slash_axe",
    "斩斧": "slash_axe",
    "盾斧": "charge_axe",
    "充能斧": "charge_axe",
    "操虫棍": "insect_glaive",
    "虫棍": "insect_glaive",
    "弓": "bow",
    "轻弩": "light_bowgun",
    "重弩": "heavy_bowgun",
}

# 配方系统 -> 行内标签
SYSTEM_LABELS = {
    "product": "生产",
    "process": "强化",
    "change": "百龙强化",
    "qurious": "怪异炼化",
}

# 「怪物系列 + 武器类型」的复合查询（例：伞鸟枪 / 雷神龙盾斧）。
# 按后缀长度降序匹配，保证「铳枪」不会被「枪」抢先命中。
COMPOUND_SUFFIXES = sorted(
    (
        ("大剑", "great_sword"),
        ("太刀", "long_sword"),
        ("片手剑", "short_sword"),
        ("片手", "short_sword"),
        ("双剑", "dual_blades"),
        ("双刀", "dual_blades"),
        ("大锤", "hammer"),
        ("锤", "hammer"),
        ("狩猎笛", "horn"),
        ("笛", "horn"),
        ("长枪", "lance"),
        ("铳枪", "gun_lance"),
        ("枪", "lance"),
        ("铳", "gun_lance"),
        ("斩击斧", "slash_axe"),
        ("斩斧", "slash_axe"),
        ("充能斧", "charge_axe"),
        ("盾斧", "charge_axe"),
        ("操虫棍", "insect_glaive"),
        ("虫棍", "insect_glaive"),
        ("棍", "insect_glaive"),
        ("弓", "bow"),
        ("轻弩", "light_bowgun"),
        ("重弩", "heavy_bowgun"),
        ("弩", "light_bowgun"),
    ),
    key=lambda pair: -len(pair[0]),
)


def _type_zh_of(weapon_type: str | None) -> str:
    """武器表键 -> 中文名（取最短别名，如 lance -> 长枪）。"""
    if not weapon_type:
        return ""
    candidates = [
        label for label, key in WEAPON_TYPE_LABELS.items() if key == weapon_type
    ]
    return min(candidates, key=len) if candidates else ""


def _split_series_query(query: str) -> tuple[str, str | None]:
    """把「伞鸟枪」拆成 (`伞鸟`, 长枪)；拆不出类型时返回 (`伞鸟枪`, None)。

    只有在**去掉后缀后还剩内容**时才认（否则「弓」这类单字类型会被整段吃掉）。
    """
    for suffix, weapon_type in COMPOUND_SUFFIXES:
        if query.endswith(suffix) and len(query) > len(suffix):
            return query[: -len(suffix)], weapon_type
    return query, None


# 武器类型专属信息（A22 追加：盾斧的瓶、斩斧的瓶、铳枪的炮击都要显示）
SPECIAL_LABELS = {
    "bottle": "瓶",
    "fire": "炮击",
    "fire_lv": "炮击等级",
    "insect_lv": "猎虫等级",
    "charge_lv": "蓄力等级",
}

ELEMENT_LABELS = {
    "Fire": "火",
    "Water": "水",
    "Thunder": "雷",
    "Ice": "冰",
    "Dragon": "龙",
    "Poison": "毒",
    "Sleep": "睡眠",
    "Paralyze": "麻痹",
    "Bomb": "爆破",
    "None": "—",
    # 肉质表用的是小写键，且雷写作 elect
    "fire": "火",
    "water": "水",
    "ice": "冰",
    "elect": "雷",
    "dragon": "龙",
}

MOTION_LABELS = {"slash": "斩", "strike": "打", "shell": "射"}

# 没有武器类型的「武器名 -> 详情」上限
MAX_WEAPON_LIST = 4  # 不带类型时按稀有度降序只列 4 件（A14）
MAX_WEAPONS_PER_TYPE = 0  # 0 = 不截断（A14/A24：带类型时显示该类型全部）
MAX_DECO_LIST = 8  # 装饰品列表上限（A24）


def _weapon_type_zh(snapshot: Snapshot, weapon: dict) -> str:
    return (weapon.get("type_zh") or "").strip() or weapon.get("type") or "武器"


def _slot_text(slots: list) -> str:
    """`slot_num_list` 的读法：下标 = 等级-1，值 = 该等级孔的个数。"""
    parts = []
    for level, count in enumerate(slots or [], start=1):
        if count:
            parts.append(f"Lv{level}×{count}")
    return " + ".join(parts)


def _systems_text(weapon: dict) -> str:
    systems = weapon.get("systems") or []
    if not systems:
        return ""
    labels = [SYSTEM_LABELS.get(system, system) for system in systems]
    return "[" + "/".join(labels) + "]"


def build_weakness_section(snapshot: Snapshot, monster: MonsterInfo) -> Section | None:
    """弱点方块：汇总一行（A6 放在出现地图之后、素材方块之前；A7 只做汇总）。"""
    weakness = monster.weakness or {}
    elements = weakness.get("elements") or {}
    motion = weakness.get("motion") or {}
    if not elements and not motion:
        return None

    element_line = " · ".join(
        f"{ELEMENT_LABELS.get(key, key)} {elements.get(key, 0)}"
        for key in ("fire", "water", "ice", "elect", "dragon")
        if key in elements
    )
    motion_line = " · ".join(
        f"{MOTION_LABELS.get(key, key)} {motion.get(key, 0)}"
        for key in ("slash", "strike", "shell")
        if key in motion
    )
    immune = "、".join(
        ELEMENT_LABELS.get(key, key) for key in (weakness.get("immune") or [])
    )

    lines = [
        Line.of(("属性", "tag"), (element_line, "body")),
        Line.of(("肉质", "tag"), (motion_line, "body")),
    ]
    if immune:
        # 免疫单独一行：它是「完全打不动」的信息，比数值更该被看见
        lines.append(Line.of(("免疫", "tag"), (immune, "body")))

    return Section(
        title="弱点",
        lines=lines,
        glyph="note",
        count="通常状态",
        footer="各项取全部位最高肉质；源数据的部位状态无对应字段，故只取通常状态",
    )


def build_series_card(
    snapshot: Snapshot,
    label: str,
    series_names: list[str],
    weapons: list[dict],
    weapon_type: str | None = None,
) -> Card:
    """武器系列卡：某只怪衍生系列里的武器（例：伞鸟枪 → 伞鸟衍生的长枪）。"""
    type_zh = _type_zh_of(weapon_type)
    metrics = [
        ("系列", " / ".join(series_names)),
        ("武器", f"{len(weapons)} 把"),
    ]
    if type_zh:
        metrics.append(("类型", type_zh))

    rows = [
        Line.of(
            (f"R{weapon.get('rarity') or '?'}", "rank"),
            (weapon.get("name") or "", "body"),
            (_systems_text(weapon), "tag"),
        )
        for weapon in weapons
    ]
    # 标题用系列名而不是用户的原文——「伞鸟枪 的长枪」会重复，
    # 「伞鸟衍生的长枪」才读得通。
    base = series_names[0] if series_names else label
    title = f"{base}的{type_zh}" if type_zh else f"{base}的武器"
    return Card(
        title=label,
        sections=[
            Section(
                title=title,
                lines=rows,
                glyph="weapon",
                count=f"共 {len(weapons)} 把",
                # 系列里的武器往往十几到几十把，两列排版控高度
                kind="grid",
                footer="同一系列即同一只怪衍生的整条武器树",
            )
        ],
        badge="武器系列",
        metrics=metrics,
        footnote=_footnote(snapshot),
    )


def build_weapon_list_card(
    snapshot: Snapshot,
    material_name: str,
    material: MaterialInfo | None,
    specific: list[dict],
    category: list[dict],
    weapon_type: str | None = None,
) -> Card:
    """「素材 -> 可做武器」（A12/A14）。"""
    type_zh = ""
    if weapon_type:
        for label, key in WEAPON_TYPE_LABELS.items():
            if key == weapon_type:
                type_zh = label
                break

    metrics = [("可做武器", f"{len(specific)} 件")]
    if category:
        # 类别命中只给汇总数字（A20）——它动辄几百件，逐条列会把重点淹掉
        metrics.append(("可拿它顶", f"{len(category)} 件"))
    if type_zh:
        metrics.append(("限定", type_zh))

    sections: list[Section] = []
    if specific:
        head = specific[: MAX_WEAPONS_PER_TYPE or len(specific)]
        if not weapon_type:
            head = specific[:MAX_WEAPON_LIST]
        rows = []
        for weapon in head:
            rows.append(
                Line.of(
                    (_systems_text(weapon), "tag"),
                    (weapon.get("name") or "", "body"),
                    (f"R{weapon.get('rarity') or '?'}", "value"),
                )
            )
        rest = len(specific) - len(rows)
        footer = f"另有 {rest} 件" if rest else ""
        if not weapon_type and len(specific) > MAX_WEAPON_LIST:
            footer = (
                f"另有 {rest} 件；加武器类型看全部，例如 /mh 武器 {material_name} 太刀"
            )
        sections.append(
            Section(
                title="可做武器" + (f" · {type_zh}" if type_zh else ""),
                lines=rows,
                glyph="weapon",
                count="按稀有度" if not weapon_type else "全部",
                footer=footer,
                # 带类型时语义是「该类型全部」，件数可能几十，走两列压高度
                kind="grid" if weapon_type else "rows",
            )
        )

    if category:
        sections.append(
            Section(
                title="类别需求",
                lines=[
                    Line.of(
                        ("这些武器要求的是类别，不是点名要它", "muted"),
                    )
                ],
                glyph="note",
                footer=f"共 {len(category)} 件武器的类别槽可以拿它顶，此处不逐条列出",
            )
        )

    if not sections:
        sections.append(
            Section(
                title="可做武器",
                lines=[Line.of(("没有武器会用到它", "muted"))],
                glyph="weapon",
            )
        )

    return Card(
        title=material_name,
        sections=sections,
        rarity=material.rarity if material else None,
        badge="素材",
        metrics=metrics,
        footnote=_footnote(snapshot),
    )


def build_weapon_detail_card(snapshot: Snapshot, weapon_key: str) -> Card | None:
    """武器详情卡（A14 + A22）：攻击/会心/属性/防御 + 锋利度 + 孔位（含百龙孔）+ 固有属性 + 派生 + 素材。"""
    weapon = snapshot.weapon(weapon_key)
    if not weapon:
        return None

    element = weapon.get("element") or {}
    element_text = "—"
    if element.get("type") and element.get("type") != "None":
        element_text = f"{ELEMENT_LABELS.get(element['type'], element['type'])} {element.get('val') or 0}"

    metrics = [
        ("攻击", str(weapon.get("atk") if weapon.get("atk") is not None else "—")),
        ("会心", f"{weapon.get('crit') or 0}%"),
        ("属性", element_text),
    ]
    if weapon.get("def"):
        metrics.append(("防御", f"+{weapon['def']}"))

    sections: list[Section] = []

    # 锋利度：弓与弩没有这个字段（源数据里就是缺键），整块不画
    sharpness = weapon.get("sharpness")
    if sharpness:
        footer = ""
        if weapon.get("takumi"):
            footer = f"匠：共 +{weapon['takumi']}"
        sections.append(
            Section(
                title="锋利度",
                kind="sharpness",
                data={"values": sharpness},
                footer=footer,
            )
        )

    slots = weapon.get("slots") or []
    if any(slots) or any(weapon.get("hyakuryu_slots") or []):
        sections.append(
            Section(
                title="孔位",
                kind="slots",
                data={"normal": slots, "rampage": weapon.get("hyakuryu_slots") or []},
                footer="Lv 为孔位等级，×N 为该等级的孔数",
            )
        )

    special = weapon.get("special") or {}
    if special:
        rows = []
        for key, value in special.items():
            label = SPECIAL_LABELS.get(key, key)
            if isinstance(value, list):
                value = " ".join(str(item) for item in value)
            rows.append(Line.of((label, "tag"), (str(value), "body")))
        sections.append(
            Section(
                title="固有属性",
                lines=rows,
                glyph="weapon",
                footer="武器类型决定的信息（盾斧/斩击斧的瓶、铳枪的炮击等）",
            )
        )

    # 派生：父（由什么强化而来）与子（能强化成什么）都要显示。
    # V1.3.1 之前只有父，用户看不到这把武器还能往哪升。
    derive_rows: list[Line] = []
    parent_key = weapon.get("parent")
    if parent_key:
        parent = snapshot.weapon(parent_key)
        parent_name = (parent or {}).get("name") or parent_key
        derive_rows.append(
            Line.of(("来源", "tag"), (parent_name, "body"), ("强化而来", "muted"))
        )
    for child in snapshot.children_of(weapon_key):
        derive_rows.append(
            Line.of(
                ("可强化为", "tag"),
                (child.get("name") or "", "body"),
                (f"R{child.get('rarity') or '?'}", "value"),
            )
        )
    if derive_rows:
        sections.append(
            Section(
                title="派生",
                lines=derive_rows,
                glyph="weapon",
                footer="" if parent_key else "这是这条派生树的起点",
            )
        )

    recipe_rows: list[Line] = []
    recipe_footer = ""
    for system, recipe in snapshot.recipes_of(weapon_key).items():
        label = SYSTEM_LABELS.get(system, system)
        for item_id, count in (recipe.get("materials") or [])[:4]:
            recipe_rows.append(
                Line.of(
                    (f"[{label}]", "tag"),
                    (_item_name(snapshot, {"item_id": str(item_id)}), "body"),
                    (f"×{count}", "value"),
                )
            )
        category = recipe.get("category")
        if isinstance(category, list) and len(category) >= 3:
            recipe_rows.append(
                Line.of(
                    (f"[{label}]", "tag"),
                    (f"（类别）{category[2]}", "muted"),
                    (f"×{category[1]}", "value"),
                )
            )
    if recipe_rows:
        sections.append(
            Section(
                title="所需素材",
                lines=recipe_rows[:10],
                glyph="note",
                footer=recipe_footer or "同一系统内多条配方按行列出",
            )
        )

    return Card(
        title=weapon.get("name") or weapon_key,
        sections=sections,
        rarity=weapon.get("rarity"),
        badge=_weapon_type_zh(snapshot, weapon),
        metrics=metrics,
        footnote=_footnote(snapshot),
    )


def build_skill_card(snapshot: Snapshot, skill: str, decorations: list[dict]) -> Card:
    """技能卡：提供该技能的装饰珠（按孔位排序）。"""
    metrics = [("可用装饰珠", f"{len(decorations)} 个")]
    if decorations:
        metrics.append(
            (
                "孔位范围",
                f"Lv{decorations[0].get('slot')}–Lv{decorations[-1].get('slot')}",
            )
        )
    sections = [
        Section(
            title="提供该技能的装饰珠",
            lines=[
                Line.of(
                    (f"{item.get('slot')}孔", "tag"),
                    (item.get("name") or "", "body"),
                    (f"技能 Lv{item.get('skill_lv')}", "value"),
                )
                for item in decorations[:MAX_DECO_LIST]
            ],
            glyph="skill",
            count="按孔位",
            footer="孔位等级即珠子能镶嵌的最小孔；技能等级是单颗珠提供的等级",
        )
    ]
    return Card(
        title=skill,
        sections=sections,
        badge="技能",
        metrics=metrics,
        footnote=_footnote(snapshot),
    )


def build_material_deco_card(
    snapshot: Snapshot,
    material_name: str,
    material: MaterialInfo | None,
    decorations: list[dict],
) -> Card:
    """「素材 -> 可做装饰珠」。"""
    return Card(
        title=material_name,
        sections=[
            Section(
                title="可做装饰珠",
                lines=[
                    Line.of(
                        (f"{item.get('slot')}孔", "tag"),
                        (item.get("name") or "", "body"),
                        (f"{item.get('skill')}", "value"),
                    )
                    for item in decorations[:MAX_DECO_LIST]
                ],
                glyph="skill",
                count=f"共 {len(decorations)} 个",
                footer=(
                    f"另有 {len(decorations) - MAX_DECO_LIST} 个"
                    if len(decorations) > MAX_DECO_LIST
                    else ""
                ),
            )
        ],
        rarity=material.rarity if material else None,
        badge="素材",
        metrics=[("可做装饰珠", f"{len(decorations)} 个")],
        footnote=_footnote(snapshot),
    )


def handle_weapon(snapshot: Snapshot, argument: str) -> CommandResult:
    """`/mh 武器 <素材名或武器名> [武器类型]` —— 同命令双向自动识别（A12）。"""
    if not argument:
        return CommandResult(
            text=(
                "用法：/mh 武器 <素材名或武器名> [武器类型]\n"
                "例如：/mh 武器 怪异化的皮\n"
                "      /mh 武器 怪异化的皮 太刀\n"
                "      /mh 武器 炎火杰大剑"
            )
        )

    tokens = argument.split()
    weapon_type = None
    if len(tokens) >= 2 and tokens[-1] in WEAPON_TYPE_LABELS:
        weapon_type = WEAPON_TYPE_LABELS[tokens[-1]]
        tokens = tokens[:-1]
    query = " ".join(tokens).strip()

    # 先试「怪物系列 + 武器类型」（V1.3.1）：例「伞鸟枪」「雷神龙盾斧」。
    # 这类说法指的是「这只怪衍生出来的那条武器树里的某类武器」。
    if query:
        prefix, compound_type = _split_series_query(query)
        effective_type = weapon_type or compound_type
        series_names = snapshot.series_matching(prefix)
        if series_names:
            weapons = snapshot.weapons_in_series(series_names, effective_type)
            if weapons:
                return CommandResult(
                    card=build_series_card(
                        snapshot, query, series_names, weapons, effective_type
                    )
                )

    # 素材与武器都查一遍，再按**匹配质量**决定走哪条分支。
    #
    # 不能简单地「素材优先」：实测查「钢剑」（武器精确名）会被素材侧的
    # 模糊匹配抢走——「钢剑」与素材「钢蛋」编辑距离为 1，于是返回了「钢蛋」的
    # 武器列表。规则改成：**精确的武器名胜过模糊的素材名**，
    # 只有素材也精确命中（或两边命中质量相同）时才让素材优先（A12）。
    material_matches = snapshot.search_materials(query, limit=6)
    weapon_matches = snapshot.search_weapons(query, limit=12)
    best_material = next((m for m in material_matches if not m.ambiguous), None)
    best_weapon = next((m for m in weapon_matches if not m.ambiguous), None)

    use_weapon = best_weapon is not None and (
        best_material is None or best_weapon.score < best_material.score
    )

    if not use_weapon and best_material is not None:
        material = snapshot.material(best_material.key)
        specific, category = snapshot.weapons_for_material(
            best_material.key, weapon_type
        )
        card = build_weapon_list_card(
            snapshot, best_material.name, material, specific, category, weapon_type
        )
        if best_material.note:
            card.subtitle = best_material.note
        return CommandResult(card=card)

    if best_weapon is not None:
        card = build_weapon_detail_card(snapshot, best_weapon.key)
        if card:
            if best_weapon.note:
                card.subtitle = best_weapon.note
            return CommandResult(card=card)

    # 没命中：如果两边都有模糊候选，把候选列出来让用户挑
    if material_matches:
        lines = [f"「{query}」同时接近几个素材，请回复更具体的名字："]
        lines.extend(f"· {m.name}" for m in material_matches)
        return CommandResult(text="\n".join(lines))
    if not weapon_matches:
        return CommandResult(
            text=f"没有找到名为「{query}」的素材或武器。可以只输入名字的一部分再试试。"
        )

    lines = [f"「{query}」匹配到 {len(weapon_matches)} 把武器，请回复更具体的名字："]
    for match in weapon_matches:
        note = f"  {match.note}" if match.note else ""
        lines.append(f"· {match.name}{note}")
    lines.append("")
    lines.append("例如：/mh 武器 " + weapon_matches[0].name)
    return CommandResult(text="\n".join(lines))


def handle_skill(snapshot: Snapshot, argument: str) -> CommandResult:
    """`/mh 技能 <技能名或素材名>` —— 双向（A23 改版：取代原定的 /mh 装饰品）。"""
    if not argument:
        return CommandResult(
            text=(
                "用法：/mh 技能 <技能名或素材名>\n"
                "例如：/mh 技能 攻击\n"
                "      /mh 技能 修罗原珠"
            )
        )

    query = argument.strip()

    # 先按技能名（精确 -> 前缀 -> 包含 -> 模糊）
    skill_matches = snapshot.search_skills(query, limit=6)
    usable = [m for m in skill_matches if not m.ambiguous]
    if usable:
        target = usable[0]
        decorations = snapshot.decorations_for_skill(target.name)
        if decorations:
            card = build_skill_card(snapshot, target.name, decorations)
            if target.note:
                card.subtitle = target.note
            return CommandResult(card=card)

    # 再按素材名 -> 该素材能做的装饰珠
    material_matches = snapshot.search_materials(query, limit=6)
    usable_material = [m for m in material_matches if not m.ambiguous]
    if usable_material:
        target = usable_material[0]
        decorations = snapshot.decorations_for_material(target.key)
        material = snapshot.material(target.key)
        if decorations:
            card = build_material_deco_card(
                snapshot, target.name, material, decorations
            )
            if target.note:
                card.subtitle = target.note
            return CommandResult(card=card)
        return CommandResult(
            text=f"没有装饰珠需要「{target.name}」。可以用 /mh 素材 {target.name} 看它的其他用途。"
        )

    if skill_matches:
        lines = [f"「{query}」同时接近几个技能，请回复更具体的名字："]
        lines.extend(f"· {m.name}" for m in skill_matches)
        return CommandResult(text="\n".join(lines))
    if material_matches:
        lines = [f"「{query}」同时接近几个素材，请回复更具体的名字："]
        lines.extend(f"· {m.name}" for m in material_matches)
        return CommandResult(text="\n".join(lines))

    return CommandResult(
        text=f"没有找到名为「{query}」的技能或素材。可以只输入名字的一部分再试试。"
    )


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
        name="武器",
        usage="/mh 武器 <素材名或武器名> [武器类型]",
        summary="素材能做的武器 / 某把武器的详情（攻击、锋利度、孔位、派生、素材）",
        handler=handle_weapon,
        examples=(
            "/mh 武器 怪异化的皮",
            "/mh 武器 怪异化的皮 太刀",
            "/mh 武器 炎火杰大剑",
        ),
    ),
    Command(
        name="技能",
        usage="/mh 技能 <技能名或素材名>",
        summary="某个技能有哪些装饰珠 / 某素材能做哪些装饰珠",
        handler=handle_skill,
        examples=("/mh 技能 攻击", "/mh 技能 修罗原珠"),
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


def _item_rarity(snapshot: Snapshot, item_id) -> int:
    """按物品 id 取稀有度；查不到返回 0。"""
    item = snapshot.items.get(str(item_id))
    if item:
        try:
            return int(item.get("rarity") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


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
