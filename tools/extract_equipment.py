#!/usr/bin/env python3
"""V1.3.0 装备数据提取模块：武器 / 素材配方反查 / 装饰品。

数据源
------
* ``mhrice`` —— 已 ``json.load`` 的 mhrice.json（游戏内部表转储）
* ``items``  —— 已 ``json.load`` 的 items.json（``"<id>" -> {name, material_category, ...}``）

唯一公开入口::

    from extract_equipment import build_equipment

    data = build_equipment(mhrice, items)
    # -> {"weapons": {...}, "material_recipes": {...},
    #     "decorations": {...}, "deco_index": {...}}

结构与键名严格按 ``_spec_v130.md`` §1 / §1.3 / §2 / §2.1。

本模块只读、不写文件、不联网。直接运行 ``python tools/extract_equipment.py``
会加载默认源数据并打印自检报告（条数、样例、字节数）。

实测与规格的差异（以实测为准，已在实现里落到对应分支）
------------------------------------------------------
1. **嵌套深度逐类型不同** —— 大剑 ``base.base.base.base.id``、轻弩 ``base.base.atk``。
   所以取字段一律用 :func:`find_key` 按 key 名递归查找，不写死路径。
2. **武器中文名** —— 名称串编号才是武器 id（``gun_lance.name_mr[183]`` 的名称串编号
   是 137，列表下标与 id 无关）。名称表：id<300 用 ``name.entries``，
   id>=300 用 ``name_mr.entries``；条目名形如 ``W_<Type>_<NNN>_Name`` /
   ``..._Name_MR``，一律用串里的数字匹配。
3. **``insect_glaive`` 的字段名是 ``insect_glaive_insect_lv``**（不是 ``insect_lv``），
   且位于 base_data 行顶层。
4. **装饰品 id 空间** —— ``decorations.param`` 里同时有 ``Deco``(0..107) 与
   ``MrDeco``(0..152) 两套 id，数值重合。规格只写了 ``d{Deco id}``；
   为了不互相覆盖，这里沿用游戏自身名称串的位移规则：
   ``Deco:n -> "d{n}"``、``MrDeco:n -> "d{200+n}"``
   （对应名称键 ``Decorations_{n:03d}_Name`` / ``Decorations_{200+n:03d}_Name``）。
   百龙珠 ``hyakuryu_decos`` 是第三套 id 空间，用 ``"h{n}"``。
5. **``weapons`` 条数是 3953**（4178 行原始数据，另 225 行是 ``#Rejected#``／
   空名／开发测试行）。其中 ``custom_table_no != 0`` 的原始有 904 把，
   过滤掉 17 把开发测试行后，``qurious`` 覆盖 887 把。
6. **``recipes["qurious"]`` 的 ``materials`` 是 ``[item_id, num]`` 对的并集** ——
   同一素材 id 在不同槽位可能带着不同数量（实测 2961 破怪的龙骨 有 x2 与 x3），
   并集按「对」去重（两份都留），不做加总也不按 id 覆盖。
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

# 消息表 content 数组里的简体中文列（实测自 I_0427_Name = 龍玉/龙玉）
COL_SIMPLIFIED = 13
COL_JAPANESE = 0

# 14 种武器：(内部表名, id 标签, 中文名)
WEAPON_TYPES: list[tuple[str, str, str]] = [
    ("great_sword", "GreatSword", "大剑"),
    ("long_sword", "LongSword", "太刀"),
    ("short_sword", "ShortSword", "单手剑"),
    ("dual_blades", "DualBlades", "双剑"),
    ("hammer", "Hammer", "大锤"),
    ("horn", "Horn", "狩猎笛"),
    ("lance", "Lance", "长枪"),
    ("gun_lance", "GunLance", "铳枪"),
    ("slash_axe", "SlashAxe", "斩击斧"),
    ("charge_axe", "ChargeAxe", "充能斧"),
    ("insect_glaive", "InsectGlaive", "操虫棍"),
    ("bow", "Bow", "弓"),
    ("light_bowgun", "LightBowgun", "轻弩炮"),
    ("heavy_bowgun", "HeavyBowgun", "重弩炮"),
]

# §1.2 类型专属字段的枚举 → 中文
CHARGE_AXE_BOTTLE = {
    "Power": "榴弹瓶",
    "StrongElement": "强属性瓶",
}
SLASH_AXE_BOTTLE = {
    "Power": "强击瓶",
    "StrongElement": "强属性瓶",
    "DownStamina": "减气瓶",
    "Paralyze": "麻痹瓶",
    "Dragon": "灭龙瓶",
    "Poison": "毒瓶",
}
GUN_LANCE_FIRE = {
    "Normal": "通常",
    "Radial": "放射",
    "Diffusion": "扩散",
}

# 「不该进 weapons」的日文名特征（开发测试行）
DEV_JP_TOKENS = ("テスト用", "斬れ味テスト", "カスタムT")
REJECTED = "#Rejected#"

# 配方三件套 + 怪异炼化
RECIPE_KINDS = ("product", "process", "change", "qurious")

# MrDeco 的 id 位移（与游戏名称串 Decorations_{200+n:03d}_Name 一致）
MR_DECO_OFFSET = 200

# 百龙装饰珠技能名。本地 hyakuryu_skill_name_msg 只到 skill id 258，
# 259..293 这一批只能外部补 —— 以 _probe_kd_final_mapping.json 为准。
# key = hyakuryu_decos 的 deco id（表里的 {"Deco": n}）。
RAMPAGE_DECO_SKILL: dict[int, str] = {
    0: "痛恨一击",
    1: "弱点特效【属性】",
    2: "防御打磨术Ⅰ",
    3: "防御打磨术Ⅱ",
    4: "鬼人激化",
    5: "大音量演奏",
    6: "瓶变形强化",
    7: "曲射效果持续",
    9: "龙种特效",
    10: "空栖系特效",
    11: "水栖系特效",
    12: "小型特效",
    13: "火异常状态特效",
    14: "水异常状态特效",
    15: "雷异常状态特效",
    16: "冰异常状态特效",
    17: "怨虎龙之魂",
    18: "天彗龙之魂",
    19: "霞龙之魂",
    20: "钢龙之魂",
    21: "炎王龙之魂",
    22: "风神龙之魂",
    23: "雷神龙之魂",
    24: "随从指挥术",
    25: "鬼火鸟加倍",
    26: "龙杭炮强化",
    27: "牙兽种特效",
    28: "劫血活性",
    29: "翔虫效果持续",
    30: "钻研",
    31: "蓄击强化",
    32: "鬼人强走",
    33: "猎虫激化",
    34: "防御重击",
}

_NAME_NUM_RE = re.compile(r"^W_[A-Za-z]+_(\d+)_Name")


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def find_key(obj, key, default=None):
    """按 key 名递归查找（DFS，先 dict 自身再按插入序下沉，最后 list）。

    武器 base_data 的嵌套深度逐类型不同（大剑 ``base.base.base.base.id``、
    轻弩 ``base.base.atk``），所以绝不可以写死路径。
    """
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = find_key(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_key(value, key)
            if found is not None:
                return found
    return default


def _msg_zh(entry) -> str:
    """消息条目的简中名：content[13]，空则退 content[0]。"""
    content = (entry or {}).get("content") or []
    zh = content[COL_SIMPLIFIED] if len(content) > COL_SIMPLIFIED else ""
    if zh and zh.strip():
        return zh.strip()
    jp = content[COL_JAPANESE] if content else ""
    return (jp or "").strip()


def _msg_index(table) -> dict[str, str]:
    """``entries`` -> ``{name: 简中名}``。"""
    out: dict[str, str] = {}
    for entry in (table or {}).get("entries") or []:
        out[entry.get("name") or ""] = _msg_zh(entry)
    return out


def _num_index(table, prefix: str) -> dict[int, str]:
    """``entries`` -> ``{名称串里的数字: 简中名}``。

    名称串形如 ``Decorations_0201_Name`` / ``PlayerSkill_000_Name`` /
    ``HyakuryuDeco_008_Name``。**一律用串里的数字匹配**，不要用下标，
    也不要自己拼零填充位数 —— 实测 ``Decorations_`` 的 MR 段是 4 位
    （``Decorations_0200_Name`` 对应 MrDeco:0），``PlayerSkill_`` 的 MR 段
    是 3 位（``PlayerSkill_200_Name``）。
    """
    pattern = re.compile(r"^" + re.escape(prefix) + r"(\d+)_Name")
    out: dict[int, str] = {}
    for entry in (table or {}).get("entries") or []:
        match = pattern.match(entry.get("name") or "")
        if match:
            out.setdefault(int(match.group(1)), _msg_zh(entry))
    return out


def _id_of(row, tag):
    """从一行里取出 ``{<Tag>: id}`` 的 id。"""
    return find_key(row, tag)


def _pair_list(items, nums):
    """``[{"Normal": n}, "None"]`` + ``[num]`` -> ``[[n, num]]``，跳过 None 槽。"""
    out = []
    for slot, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        item_id = next(iter(item.values()), None)
        if item_id is None:
            continue
        num = nums[slot] if nums and slot < len(nums) else 0
        out.append([item_id, num])
    return out


def _dedupe_pairs(pairs):
    """按 (item_id, num) 对去重，保留首次出现顺序。"""
    seen: dict[tuple[int, int], None] = {}
    for item_id, num in pairs:
        seen[(item_id, num)] = None
    return [[item_id, num] for item_id, num in seen]


# --------------------------------------------------------------------------
# 素材类别名（§1 的 category 第三项）
# --------------------------------------------------------------------------


class CategoryNames:
    """``{"LrHr": n}`` / ``{"Mr": n}`` -> 中文类别名。

    ``LrHr`` 查 ``material_category_msg`` 的 ``ICT_Name_{n:02d}``；
    ``Mr``  查 ``material_category_msg_mr`` 的 ``ICT_Name_{200+n}``。
    """

    def __init__(self, mhrice: dict):
        self.lrhr = _msg_index(mhrice.get("material_category_msg"))
        self.mr = _msg_index(mhrice.get("material_category_msg_mr"))

    def resolve(self, raw_id: int, tag: str) -> str | None:
        if tag == "Mr":
            name = self.mr.get(f"ICT_Name_{200 + raw_id}")
        else:
            name = self.lrhr.get(f"ICT_Name_{raw_id:02d}")
        if not name or REJECTED in name:
            return None
        return name

    def triples(self, material_category, material_category_num):
        """``{"LrHr": 2}, 15`` -> ``[2, 15, "矿石素材"]``；无类别 -> None。"""
        if not isinstance(material_category, dict) or not material_category:
            return None
        tag, raw_id = next(iter(material_category.items()))
        return [raw_id, material_category_num or 0, self.resolve(raw_id, tag)]


# --------------------------------------------------------------------------
# 武器
# --------------------------------------------------------------------------


def _series_name_tables(mhrice: dict) -> dict:
    """读系列名表，返回 ``{"base": {编号: 名}, "mr": {编号: 名}}``。

    条目名形如 ``WeaponSeries_Hunter_000``——**没有** ``_Name`` 后缀，
    所以不能复用武器名表那套正则（实测直接用 `_NAME_NUM_RE` 会一条都取不到）。
    """
    out: dict[str, dict[int, str]] = {}
    for key, table_name in (("base", "weapon_series"), ("mr", "weapon_series_mr")):
        table: dict[int, str] = {}
        for entry in (mhrice.get(table_name) or {}).get("entries") or []:
            match = re.search(r"_(\d+)$", entry.get("name") or "")
            if not match:
                continue
            content = entry.get("content") or []
            name = content[COL_SIMPLIFIED] if len(content) > COL_SIMPLIFIED else ""
            name = (name or "").strip()
            if name and REJECTED not in name:
                table[int(match.group(1))] = name
        out[key] = table
    return out


def _weapon_name_index(type_table) -> dict[int, dict]:
    """``{武器 id: 名称条目}``。

    规格：id<300 查 ``name.entries``，id>=300 查 ``name_mr.entries``。
    两张表的编号区间实测不重叠（name 0..149，name_mr 300..497，
    ``gun_lance.name_mr`` 里那条编号 137 是坏条目），所以先 name 后 name_mr
    用 ``setdefault`` 合并，效果与按 id 分表一致且更稳。
    """
    index: dict[int, dict] = {}
    for key in ("name", "name_mr"):
        for entry in (type_table.get(key) or {}).get("entries") or []:
            match = _NAME_NUM_RE.match(entry.get("name") or "")
            if match:
                index.setdefault(int(match.group(1)), entry)
    return index


def _weapon_recipes(type_table, tag, cat_names) -> dict[int, dict]:
    """三套配方：``{武器 id: {"product": {...}, "process": {...}, "change": {...}}}``。"""
    out: dict[int, dict] = collections.defaultdict(dict)
    for kind in ("product", "process", "change"):
        for row in (type_table.get(kind) or {}).get("param") or []:
            weapon_id = _id_of(row, tag)
            if weapon_id is None:
                continue
            materials = _pair_list(find_key(row, "item"), find_key(row, "item_num"))
            category = cat_names.triples(
                find_key(row, "material_category"),
                find_key(row, "material_category_num"),
            )
            entry = out[weapon_id].setdefault(kind, {"materials": [], "category": None})
            entry["materials"] = _dedupe_pairs(entry["materials"] + materials)
            if entry["category"] is None and category is not None:
                entry["category"] = category
    return out


def _qurious_recipes(
    mhrice: dict, cat_names: CategoryNames
) -> dict[tuple[str, int], dict]:
    """§1.1 怪异炼化：``custom_table_no`` -> 表行 -> 槽位 -> 素材行。

    返回 ``{(武器类型, 武器 id): {"materials": [...], "category": [...]|None}}``。
    """
    slot_rows: dict[int, dict] = {}
    for row in (mhrice.get("custom_buildup_weapon_material") or {}).get("param") or []:
        slot_rows[row.get("id")] = row

    rows_by_table_no: dict[int, list] = collections.defaultdict(list)
    for row in (mhrice.get("custom_buildup_wep_table") or {}).get("param") or []:
        rows_by_table_no[row.get("table_no")].append(row)

    out: dict[tuple[str, int], dict] = {}
    for type_name, tag, _zh in WEAPON_TYPES:
        for base_row in (mhrice.get(type_name) or {}).get("base_data", {}).get(
            "param"
        ) or []:
            table_no = find_key(base_row, "custom_table_no") or 0
            if not table_no:
                continue
            weapon_id = _id_of(base_row, tag)
            materials: list[list[int]] = []
            category = None
            for table_row in rows_by_table_no.get(table_no, []):
                for slot_id in table_row.get("id") or []:
                    if not slot_id:
                        continue
                    slot = slot_rows.get(slot_id)
                    if not slot:
                        continue
                    materials.extend(_pair_list(slot.get("item"), slot.get("item_num")))
                    if category is None:
                        category = cat_names.triples(
                            slot.get("material_category"),
                            slot.get("material_category_num"),
                        )
            out[(type_name, weapon_id)] = {
                "materials": _dedupe_pairs(materials),
                "category": category,
            }
    return out


def _weapon_parents(type_table, tag) -> dict[int, str | None]:
    """派生来源：``tree`` 行里 ``prev_weapon_type`` + ``prev_weapon_index`` 反查 id。"""
    tree = (type_table.get("tree") or {}).get("param") or []
    by_position: dict[tuple[str, int], int] = {}
    row_of: dict[int, dict] = {}
    for row in tree:
        weapon_id = _id_of(row, tag)
        if weapon_id is None:
            continue
        by_position[(_tree_key(row.get("tree_type")), row.get("index"))] = weapon_id
        row_of.setdefault(weapon_id, row)
    # 一个 weapon_id 只会有一行（实测 14 张表 dup_wid 全为 0）
    out: dict[int, str | None] = {}
    for weapon_id, row in row_of.items():
        prev_index = row.get("prev_weapon_index")
        if prev_index is None or prev_index < 0:
            out[weapon_id] = None
            continue
        parent_id = by_position.get(
            (_tree_key(row.get("prev_weapon_type")), prev_index)
        )
        out[weapon_id] = parent_id
    return out


def _weapon_series(type_table, tag, series_names: dict) -> dict[int, str]:
    """武器 id -> 系列名（如「伞鸟衍生」）。

    ## 系列是什么

    游戏把「同一只怪衍生出来的整条武器树」编成一个 `tree_type`，名字在
    `weapon_series` / `weapon_series_mr` 表里（条目名 `WeaponSeries_Hunter_NNN`）。
    用户会直接按系列找武器（「伞鸟枪」「雷神龙盾斧」），所以必须拿到它。

    ## tree_type 有两种形态，编号也是两套

    * `"Ore"`（字符串）→ 系列 000「矿石素材衍生」
    * `{"TreeType": n}`：
      - `n ∈ [1, 97]` → `weapon_series` 的 **n+1**
      - `n ≥ 98`    → `weapon_series_mr` 的 **n+102**

    两个公式都用实例核对过：大剑 id=2（`"Ore"`）落在「矿石素材衍生」，
    大剑 id=302（`TreeType 1`）落在「炎火衍生」——两把武器的实际系列都对。
    实测 128 个 tree_type **全部解析成功**，没有解析不到的。
    """
    base = series_names.get("base") or {}
    master = series_names.get("mr") or {}
    out: dict[int, str] = {}
    for row in (type_table.get("tree") or {}).get("param") or []:
        weapon_id = _id_of(row, tag)
        if weapon_id is None:
            continue
        tree_type = row.get("tree_type")
        name = ""
        if isinstance(tree_type, str):
            if tree_type == "Ore":
                name = base.get(0, "")
        elif isinstance(tree_type, dict):
            number = tree_type.get("TreeType")
            if isinstance(number, int):
                name = base.get(number + 1) or master.get(number + 102) or ""
        if name:
            out[weapon_id] = name
    return out


def _tree_key(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _weapon_special(type_name: str, base_row: dict) -> dict:
    """§1.2 类型专属字段（值都在 base_data 行顶层）。"""
    if type_name == "charge_axe":
        bottle = base_row.get("charge_axe_bottle_type")
        return {"bottle": CHARGE_AXE_BOTTLE.get(bottle, bottle)} if bottle else {}
    if type_name == "slash_axe":
        bottle = base_row.get("slash_axe_bottle_type")
        return {"bottle": SLASH_AXE_BOTTLE.get(bottle, bottle)} if bottle else {}
    if type_name == "gun_lance":
        special = {}
        fire = base_row.get("gun_lance_fire_type")
        if fire:
            special["fire"] = GUN_LANCE_FIRE.get(fire, fire)
        fire_lv = base_row.get("gun_lance_fire_lv")
        if fire_lv is not None:
            special["fire_lv"] = fire_lv
        return special
    if type_name == "insect_glaive":
        # 实测字段名是 insect_glaive_insect_lv
        insect_lv = base_row.get("insect_glaive_insect_lv")
        return {"insect_lv": insect_lv} if insect_lv is not None else {}
    if type_name == "horn":
        melody = base_row.get("horn_melody_type_list")
        return {"melody": melody} if melody is not None else {}
    if type_name == "bow":
        charge_lv = base_row.get("bow_default_charge_lv_limit")
        return {"charge_lv": charge_lv} if charge_lv is not None else {}
    return {}


def _hyakuryu_skills(base_row: dict) -> list[int]:
    """``hyakuryu_skill_id_list`` 里的 ``{"Skill": n}``；字符串 ``"None"`` 跳过。"""
    raw = find_key(base_row, "hyakuryu_skill_id_list") or []
    out = []
    for item in raw:
        if isinstance(item, dict):
            value = item.get("Skill")
            if value is not None:
                out.append(value)
    return out


def _weapon_element(base_row: dict) -> tuple[dict, dict | None]:
    element_type = find_key(base_row, "main_element_type")
    element_val = find_key(base_row, "main_element_val") or 0
    if not element_type or element_type == "None":
        element = {"type": "None", "val": 0}
    else:
        element = {"type": element_type, "val": element_val}

    sub_type = find_key(base_row, "sub_element_type")
    sub_val = find_key(base_row, "sub_element_val") or 0
    if sub_type and sub_type != "None":
        sub_element = {"type": sub_type, "val": sub_val}
    else:
        sub_element = None
    return element, sub_element


# --------------------------------------------------------------------------
# 装饰品
# --------------------------------------------------------------------------


def build_decorations(mhrice: dict):
    """§2 / §2.1：``(decorations, deco_index, skipped)``。"""
    deco_names = _num_index(mhrice.get("decorations_name_msg"), "Decorations_")
    deco_names_mr = _num_index(mhrice.get("decorations_name_msg_mr"), "Decorations_")
    hy_deco_names = _num_index(mhrice.get("hyakuryu_decos_name_msg"), "HyakuryuDeco_")
    skill_names = _num_index(mhrice.get("player_skill_name_msg"), "PlayerSkill_")
    skill_names_mr = _num_index(mhrice.get("player_skill_name_msg_mr"), "PlayerSkill_")
    hy_skill_names = _num_index(mhrice.get("hyakuryu_skill_name_msg"), "HyakuryuSkill_")

    def skill_zh(skill_ref):
        """``{"Skill": n}`` / ``{"MrSkill": n}`` 两种标签都要读。"""
        if not isinstance(skill_ref, dict) or not skill_ref:
            return None
        tag, value = next(iter(skill_ref.items()))
        if tag == "MrSkill":
            return skill_names_mr.get(200 + value)
        return skill_names.get(value)

    def first_skill(entry):
        refs = entry.get("skill_id_list") or []
        lvs = entry.get("skill_lv_list") or []
        for slot, ref in enumerate(refs):
            if isinstance(ref, dict) and ref:
                return skill_zh(ref), (lvs[slot] if slot < len(lvs) else 1)
        return None, 0

    # 产物表：{("Deco", n) / ("MrDeco", n): row}
    products = {}
    for row in (mhrice.get("decorations_product") or {}).get("param") or []:
        ident = row.get("id") or {}
        if ident:
            tag, value = next(iter(ident.items()))
            products[(tag, value)] = row
    hy_products = {}
    for row in (mhrice.get("hyakuryu_decos_product") or {}).get("param") or []:
        ident = row.get("id") or {}
        if ident:
            tag, value = next(iter(ident.items()))
            hy_products[(tag, value)] = row

    decorations: dict[str, dict] = {}
    skipped = collections.Counter()

    # --- 普通装饰品（Deco 与 MrDeco 是两套 id 空间，必须分开存） ---
    for row in (mhrice.get("decorations") or {}).get("param") or []:
        ident = row.get("id") or {}
        if not ident:
            continue
        tag, deco_id = next(iter(ident.items()))
        if tag == "MrDeco":
            # 名称表的 MR 段数字就是 200+id（实测 4 位零填充：MrDeco:1 -> 0201）
            name = deco_names_mr.get(MR_DECO_OFFSET + deco_id)
            key = f"d{MR_DECO_OFFSET + deco_id}"
        else:
            name = deco_names.get(deco_id)
            key = f"d{deco_id}"
        if not name or REJECTED in name:
            # MrDeco:0 是 #Rejected# 占位
            skipped[f"deco_{tag}"] += 1
            continue
        product = products.get((tag, deco_id))
        skill, skill_lv = first_skill(row)
        decorations[key] = {
            "name": name,
            "kind": "normal",
            "slot": row.get("decoration_lv"),
            "skill": skill,
            "skill_lv": skill_lv,
            "rarity": row.get("rare"),
            "materials": _pair_list(
                (product or {}).get("item_id_list"),
                (product or {}).get("item_num_list"),
            ),
        }

    # --- 百龙装饰珠（技能名以外部映射表为准：本地 hyakuryu_skill_name_msg 只到 258） ---
    for row in (mhrice.get("hyakuryu_decos") or {}).get("param") or []:
        ident = row.get("id") or {}
        if not ident:
            continue
        _tag, deco_id = next(iter(ident.items()))
        skill_ref = row.get("hyakuryu_skill_id") or {}
        skill_id = next(iter(skill_ref.values()), None) if skill_ref else None
        name = hy_deco_names.get(deco_id)
        skill = RAMPAGE_DECO_SKILL.get(deco_id)
        if skill is None and skill_id is not None:
            skill = hy_skill_names.get(skill_id)
        if not name or REJECTED in name:
            skipped["hyakuryu_no_name"] += 1
            continue
        product = hy_products.get(("Deco", deco_id))
        decorations[f"h{deco_id}"] = {
            "name": name,
            "kind": "rampage",
            "slot": row.get("decoration_lv"),
            "skill": skill,
            "skill_lv": 1,
            "rarity": row.get("rare"),
            "materials": _pair_list(
                (product or {}).get("item_id_list"),
                (product or {}).get("item_num_list"),
            ),
        }

    # --- 反查 ---
    by_skill: dict[str, dict[str, None]] = {}
    by_material: dict[str, dict[str, None]] = {}
    for key, deco in decorations.items():
        if deco.get("skill"):
            by_skill.setdefault(deco["skill"], {})[key] = None
        for item_id, _num in deco["materials"]:
            by_material.setdefault(str(item_id), {})[key] = None

    deco_index = {
        "by_material": {k: list(v) for k, v in by_material.items()},
        "by_skill": {k: list(v) for k, v in by_skill.items()},
    }
    return decorations, deco_index, skipped


# --------------------------------------------------------------------------
# 公开入口
# --------------------------------------------------------------------------

#: ``build_equipment`` 的最近一次跳过/覆盖统计（返回值只含契约的四个键）。
LAST_STATS: dict = {}


def build_equipment(mhrice: dict, items: dict) -> dict:
    """返回 ``{"weapons", "material_recipes", "decorations", "deco_index"}``。

    结构与键名严格按 ``_spec_v130.md`` §1 / §1.3 / §2 / §2.1。
    跳过统计（``#Rejected#`` / 空名 / 开发测试行等）写在模块级
    :data:`LAST_STATS`，返回值里只有契约规定的四个键。
    """
    cat_names = CategoryNames(mhrice)
    series_names = _series_name_tables(mhrice)

    weapons: dict[str, dict] = {}
    skipped = collections.Counter()
    skipped_examples: dict[str, list[str]] = collections.defaultdict(list)
    by_kind: dict[str, int] = collections.Counter()
    counted_custom_table_no = 0

    qurious = _qurious_recipes(mhrice, cat_names)

    # 反查索引收集器（dict 当有序 set 用）
    specific_refs: dict[int, dict[tuple[str, str], None]] = {}
    category_refs: dict[str, dict[tuple[str, str], None]] = {}

    for type_name, tag, type_zh in WEAPON_TYPES:
        type_table = mhrice.get(type_name) or {}
        base_rows = (type_table.get("base_data") or {}).get("param") or []
        name_index = _weapon_name_index(type_table)
        recipes_of = _weapon_recipes(type_table, tag, cat_names)
        parents = _weapon_parents(type_table, tag)
        series_of = _weapon_series(type_table, tag, series_names)

        for base_row in base_rows:
            weapon_id = _id_of(base_row, tag)
            if find_key(base_row, "custom_table_no"):
                counted_custom_table_no += 1
            entry = name_index.get(weapon_id)
            if entry is None:
                skipped["no_name_entry"] += 1
                continue
            content = entry.get("content") or []
            zh = content[COL_SIMPLIFIED] if len(content) > COL_SIMPLIFIED else ""
            jp = content[COL_JAPANESE] if content else ""
            zh = (zh or "").strip()
            jp = (jp or "").strip()
            name = zh or jp

            if not zh and not jp:
                skipped["empty_name"] += 1
                skipped_examples["empty_name"].append(f"{type_name}:{weapon_id}")
                continue
            if REJECTED in name:
                skipped["rejected"] += 1
                if len(skipped_examples["rejected"]) < 5:
                    skipped_examples["rejected"].append(
                        f"{type_name}:{weapon_id} {name}"
                    )
                continue
            if any(token in jp for token in DEV_JP_TOKENS):
                skipped["dev_row"] += 1
                if len(skipped_examples["dev_row"]) < 5:
                    skipped_examples["dev_row"].append(
                        f"{type_name}:{weapon_id} jp={jp}"
                    )
                continue

            key = f"{type_name}:{weapon_id}"
            recipes: dict[str, dict] = {}
            for kind in ("product", "process", "change"):
                if kind in recipes_of.get(weapon_id, {}):
                    recipes[kind] = recipes_of[weapon_id][kind]
            qurious_recipe = qurious.get((type_name, weapon_id))
            if qurious_recipe is not None:
                recipes["qurious"] = qurious_recipe

            element, sub_element = _weapon_element(base_row)
            takumi_list = find_key(base_row, "takumi_val_list")
            parent_id = parents.get(weapon_id)

            weapons[key] = {
                "type": type_name,
                "type_zh": type_zh,
                "id": weapon_id,
                "name": name,
                "rarity": find_key(base_row, "rare_type"),
                "atk": find_key(base_row, "atk"),
                "crit": find_key(base_row, "critical_rate"),
                "def": find_key(base_row, "def_bonus"),
                "element": element,
                "sub_element": sub_element,
                "sharpness": find_key(base_row, "sharpness_val_list"),
                "takumi": sum(takumi_list) if takumi_list is not None else None,
                "slots": find_key(base_row, "slot_num_list"),
                "hyakuryu_slots": find_key(base_row, "hyakuryu_slot_num_list"),
                "hyakuryu_skills": _hyakuryu_skills(base_row),
                "special": _weapon_special(type_name, base_row),
                "parent": f"{type_name}:{parent_id}" if parent_id is not None else None,
                # 系列名（如「伞鸟衍生」）——用户会直接按系列找武器
                "series": series_of.get(weapon_id) or "",
                "recipes": recipes,
            }

            for kind, recipe in recipes.items():
                ref = (key, kind)
                by_kind[kind] += 1
                for item_id, _num in recipe["materials"]:
                    specific_refs.setdefault(item_id, {})[ref] = None
                category_name = recipe["category"][2] if recipe["category"] else None
                if category_name:
                    category_refs.setdefault(category_name, {})[ref] = None

    # --- §1.3 反查索引 ---
    material_recipes: dict[str, dict] = {}
    for item_id, refs in specific_refs.items():
        material_recipes[str(item_id)] = {
            "specific": [list(ref) for ref in refs],
            "category": [],
        }
    # 类别命中：素材的 material_category 数组（items.json）里任一名字
    # 与该素材所在配方的 category 中文名相同即命中 —— 量很大，照实写。
    for item_key, item in (items or {}).items():
        if not isinstance(item, dict):
            continue
        for category_name in item.get("material_category") or []:
            refs = category_refs.get(category_name)
            if not refs:
                continue
            material_recipes.setdefault(
                str(item_key),
                {
                    "specific": [],
                    "category": [],
                },
            )["category"] = [list(ref) for ref in refs]

    decorations, deco_index, deco_skipped = build_decorations(mhrice)
    skipped.update(deco_skipped)

    LAST_STATS.clear()
    LAST_STATS.update(
        {
            "weapons": len(weapons),
            "skipped": dict(skipped),
            "skipped_examples": {k: list(v) for k, v in skipped_examples.items()},
            "counted_custom_table_no": counted_custom_table_no,
            "by_kind": {kind: by_kind.get(kind, 0) for kind in RECIPE_KINDS},
            "materials_specific": len(specific_refs),
            "materials_category_named": len(category_refs),
            "decorations": {
                "normal": sum(1 for d in decorations.values() if d["kind"] == "normal"),
                "rampage": sum(
                    1 for d in decorations.values() if d["kind"] == "rampage"
                ),
            },
        }
    )

    return {
        "weapons": weapons,
        "material_recipes": material_recipes,
        "decorations": decorations,
        "deco_index": deco_index,
    }


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------


def compact_size(obj) -> int:
    """规格里的体积口径：紧凑 JSON 的 UTF-8 字节数。"""
    return len(
        json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _load(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def selftest(mhrice_path: Path, items_path: Path) -> int:
    mhrice = _load(mhrice_path)
    items = _load(items_path)
    data = build_equipment(mhrice, items)
    stats = LAST_STATS
    weapons = data["weapons"]
    material_recipes = data["material_recipes"]
    decorations = data["decorations"]
    deco_index = data["deco_index"]

    print("=" * 78)
    print("源数据")
    print("=" * 78)
    print(f"  mhrice: {mhrice_path}")
    print(f"  items : {items_path}")
    print(f"  返回键: {list(data)}（契约只允许这四个）")

    print()
    print("=" * 78)
    print("[1] 武器")
    print("=" * 78)
    print(f"  weapons 条数           : {len(weapons)}")
    print(f"  跳过行计数             : {stats['skipped']}")
    for reason, examples in stats["skipped_examples"].items():
        print(f"    {reason}: {examples}")

    with_recipe = [k for k, w in weapons.items() if w["recipes"]]
    print(f"  至少 1 套配方的武器     : {len(with_recipe)}")
    for kind in RECIPE_KINDS:
        keys = [k for k, w in weapons.items() if kind in w["recipes"]]
        print(f"    recipes[{kind:<8}]: {len(keys):5d} 把")
    print(
        f"  custom_table_no!=0 原始 : {stats['counted_custom_table_no']}"
        f"（过滤掉开发测试行后 qurious 覆盖 {stats['by_kind']['qurious']} 把）"
    )
    print(
        f"  parent 非 null         : {sum(1 for w in weapons.values() if w['parent'])}"
    )
    print(
        f"  hyakuryu_skills 非空   : {sum(1 for w in weapons.values() if w['hyakuryu_skills'])}"
    )
    print(
        f"  sub_element 非 null    : {sum(1 for w in weapons.values() if w['sub_element'])}"
    )
    print(
        f"  sharpness 为 null      : {sum(1 for w in weapons.values() if w['sharpness'] is None)}"
    )

    print()
    print("=" * 78)
    print("[2] material_recipes 反查")
    print("=" * 78)
    specific_total = sum(len(v["specific"]) for v in material_recipes.values())
    category_total = sum(len(v["category"]) for v in material_recipes.values())
    print(f"  覆盖素材 id 数 : {len(material_recipes)}")
    print(f"  specific 条数  : {specific_total}")
    print(f"  category 条数  : {category_total}")
    print(f"  category 中文名种类 : {stats['materials_category_named']}")
    biggest = max(material_recipes.items(), key=lambda kv: len(kv[1]["category"]))
    print(
        f"  category 命中最多的素材: {biggest[0]} -> {len(biggest[1]['category'])} 条"
    )
    sample_item = "2585"
    print(
        f"  样例 material_recipes[{sample_item}]: "
        f"specific={len(material_recipes[sample_item]['specific'])} "
        f"category={len(material_recipes[sample_item]['category'])}"
    )
    print(f"    specific[:3]={material_recipes[sample_item]['specific'][:3]}")
    print(f"    category[:3]={material_recipes[sample_item]['category'][:3]}")

    print()
    print("=" * 78)
    print("[3] 装饰品")
    print("=" * 78)
    normal = [k for k, v in decorations.items() if v["kind"] == "normal"]
    rampage = [k for k, v in decorations.items() if v["kind"] == "rampage"]
    print(
        f"  decorations 条数 : {len(decorations)}  (normal={len(normal)}, rampage={len(rampage)})"
    )
    print(f"  deco_index.by_skill 键数    : {len(deco_index['by_skill'])}")
    print(f"  deco_index.by_material 键数 : {len(deco_index['by_material'])}")
    print(f"  by_skill['攻击'] = {deco_index['by_skill'].get('攻击')}")
    print(f"  by_material['337'] = {deco_index['by_material'].get('337')}")

    print()
    print("=" * 78)
    print("[4] 样例")
    print("=" * 78)
    sample = weapons.get("great_sword:302")
    print("--- great_sword:302 ---")
    print(
        json.dumps(sample, ensure_ascii=False, separators=(",", ":"))
        if sample
        else "  <缺失>"
    )

    print("--- charge_axe 里有瓶的（前 3 把） ---")
    shown = 0
    for key, weapon in weapons.items():
        if weapon["type"] != "charge_axe":
            continue
        bottle = weapon["special"].get("bottle")
        if not bottle:
            continue
        print(
            f"  {key} {weapon['name']} special={json.dumps(weapon['special'], ensure_ascii=False)}"
        )
        shown += 1
        if shown >= 3:
            break

    for key in ("d0", "h0"):
        print(f"--- {key} ---")
        print(
            json.dumps(decorations.get(key), ensure_ascii=False, separators=(",", ":"))
        )
    print("--- 大师位珠样例（MrDeco:58 -> d258 攻击珠Ⅱ【4】） ---")
    print(
        json.dumps(
            decorations.get(f"d{MR_DECO_OFFSET + 58}"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )

    print()
    print("=" * 78)
    print("[5] 体积（json.dumps ensure_ascii=False, separators=(',',':'))")
    print("=" * 78)
    blocks = {
        "weapons": weapons,
        "material_recipes": material_recipes,
        "decorations": decorations,
        "deco_index": deco_index,
    }
    total = 0
    for label, block in blocks.items():
        size = compact_size(block)
        total += size
        print(f"  {label:<20}: {size:9d} 字节 = {size / 1024:8.1f} KB")
    print(f"  {'四块合计':<20}: {total:9d} 字节 = {total / 1024:8.1f} KB")
    # 拆解：qurious 是体积大头
    no_q_weapons = {}
    for key, weapon in weapons.items():
        no_q_weapons[key] = {
            **weapon,
            "recipes": {k: v for k, v in weapon["recipes"].items() if k != "qurious"},
        }
    print(
        f"    （其中 weapons 去掉 recipes.qurious 后 = "
        f"{compact_size(no_q_weapons)} 字节 = {compact_size(no_q_weapons) / 1024:.1f} KB）"
    )
    spec_total = 256 * 1024 + 84 * 1024 + 47 * 1024
    print(
        f"  规格 §5 预算（武器配方 256K + 反查 84K + 装饰品 47K = {spec_total / 1024:.0f} KB）"
        f" -> 实测是它的 {total / spec_total:.1f} 倍"
    )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="V1.3.0 装备数据提取自检")
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path(r"D:\下载"),
        help="mhrice.json / items.json 所在目录",
    )
    parser.add_argument("--mhrice", type=Path, default=None)
    parser.add_argument("--items", type=Path, default=None)
    args = parser.parse_args(argv)
    mhrice_path = args.mhrice or (args.source_dir / "mhrice.json")
    items_path = args.items or (args.source_dir / "items.json")
    for path in (mhrice_path, items_path):
        if not path.exists():
            print(f"[extract_equipment] 源数据不存在：{path}", file=sys.stderr)
            return 2
    return selftest(mhrice_path, items_path)


if __name__ == "__main__":
    raise SystemExit(main())
