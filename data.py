"""快照加载、索引与查询。

数据来源是 ``tools/extract.py`` 生成的 snapshot.json。本模块只读快照，
不碰原始游戏数据，因此查询是纯内存操作，适合群消息的高频调用。

设计上刻意做成「按需、可缺表」：以后加配装/技能表时，旧快照仍能正常工作。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 素材名匹配的排序权重：精确 > 前缀 > 包含
_MATCH_EXACT = 0
_MATCH_PREFIX = 1
_MATCH_CONTAINS = 2
_MATCH_NORMALIZED = 3  # 归一化后相等（去「的」・空格、全半角、繁→简）
_MATCH_FUZZY_1 = 4  # 归一化后编辑距离 1（如「搔鸟」误打成「骚鸟」）
_MATCH_FUZZY_2 = 5  # 归一化后编辑距离 2 —— 仅在候选唯一时才采用


class SnapshotError(RuntimeError):
    """快照缺失或损坏。"""


# --------------------------------------------------------------------------
# 路径解析
# --------------------------------------------------------------------------


def _candidate_paths() -> list[Path]:
    """按优先级列出快照可能的位置。

    1. 环境变量 MH_MATERIAL_SNAPSHOT（便于测试与自定义部署）
    2. AstrBot 的 plugin_data 目录（正式部署位置）
    3. 相对本包的 data/plugin_data（插件自带/开发时）
    """
    paths: list[Path] = []

    if override := os.environ.get("MH_MATERIAL_SNAPSHOT"):
        paths.append(Path(override))

    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        paths.append(
            Path(get_astrbot_plugin_data_path()) / "mh_material" / "snapshot.json"
        )
    except Exception:  # noqa: BLE001, S110 - 有意兜底：本模块必须能在 AstrBot 之外导入
        # 脱离 AstrBot 运行时会 ImportError；其它异常（例如包结构变化）也不该
        # 让整个插件在启动阶段炸掉，下面的相对路径仍能兜住。
        pass

    plugin_root = Path(__file__).resolve().parent
    paths.append(plugin_root / "data" / "plugin_data" / "mh_material" / "snapshot.json")
    return paths


# --------------------------------------------------------------------------
# 查询结果的数据结构
# --------------------------------------------------------------------------


@dataclass
class Match:
    """一次名字匹配的结果。"""

    key: str
    name: str
    score: int
    note: str = ""
    # 模糊候选内部的推荐次序。所有距离 1 的候选分数相同，若不带这个次序，
    # 后续按「分数+长度+名称」重排就会把顺序打乱（实测「骚鸟」的候选
    # 搔鸟/伞鸟/丸鸟 会被排成 丸鸟/伞鸟/搔鸟）。
    rank: int = 0
    # True 表示「有多个同样接近的候选，需要用户自己选」——
    # 此时**不做自动匹配**，由命令层把候选列出来。
    ambiguous: bool = False


@dataclass
class MaterialInfo:
    """素材的完整信息，供渲染层直接使用。"""

    item_id: str
    name: str
    rarity: int
    item_type: str
    sources: list[dict] = field(default_factory=list)
    monster_count: int = 0
    gathering: list[dict] = field(default_factory=list)
    monster_maps: dict[str, list[str]] = field(default_factory=dict)
    # 傀异调查（怪异调查）里怪异化怪物的掉落。结构与 sources 不同：
    # 它是「怪物 + 傀异等级区间 + 概率」，来自 mystery_reward_item 表。
    anomaly_rewards: list[dict] = field(default_factory=list)
    # 野外采集点（含节点类型）：来自 item_pop_lot + maps，
    # 形如「水没林 · 骨冢 · 大师 20%→x1」。
    gathering_nodes: list[dict] = field(default_factory=list)

    @property
    def is_rare(self) -> bool:
        """稀有素材：稀有度 >= 7（玉/天鳞/秘棘一档）。"""
        return self.rarity >= 7

    @property
    def has_anomaly(self) -> bool:
        return bool(self.anomaly_rewards)

    def gathering_node_groups(
        self, max_maps: int = 6, max_sites: int = 4
    ) -> list[dict]:
        """采集点按「地图 · 节点 · 采集点」聚合。

        同一张图上同一种节点常有**多个采集点**（矿脉①②…），掉落率不同，
        所以按 `pop_id` 分开并附序号——这正是早先 Kiranico 表里
        「同一地图同一难度出现两块不同概率」的真实原因。

        返回 [{'map', 'node', 'site', 'ranks': [{'rank', 'outcomes': [...]}], 'best'}, ...]
        """
        rank_order = {"下位": 0, "上位": 1, "大师": 2}
        buckets: dict[tuple, dict] = {}
        for point in self.gathering_nodes:
            key = (
                point.get("map_no"),
                point.get("map"),
                point.get("node"),
                point.get("pop_id"),
            )
            bucket = buckets.setdefault(
                key,
                {
                    "map_no": point.get("map_no") or 0,
                    "map": point.get("map") or "未知地图",
                    "node": point.get("node") or "采集点",
                    "pop_id": point.get("pop_id"),
                    "ranks": {},
                    "best": 0.0,
                },
            )
            rank = point.get("rank") or ""
            entry = bucket["ranks"].setdefault(rank, {"rank": rank, "outcomes": []})
            chance = _chance_value(point.get("chance"))
            entry["outcomes"].append(
                {
                    "quantity": point.get("quantity") or 0,
                    "chance": point.get("chance") or 0,
                    "value": chance,
                }
            )
            bucket["best"] = max(bucket["best"], chance)

        ordered = sorted(
            buckets.values(),
            key=lambda b: (
                b["map_no"],
                b["node"],
                b["pop_id"] if b["pop_id"] is not None else 0,
            ),
        )
        for bucket in ordered:
            ranks = sorted(
                bucket["ranks"].values(), key=lambda r: rank_order.get(r["rank"], 9)
            )
            for rank in ranks:
                rank["outcomes"].sort(key=lambda o: -o["value"])
            bucket["ranks"] = ranks

        # 同「地图 · 节点」有多个采集点时编号（①②③）
        site_counter: dict[tuple, int] = {}
        for bucket in ordered:
            group_key = (bucket["map_no"], bucket["node"])
            site_counter[group_key] = site_counter.get(group_key, 0) + 1
            bucket["site"] = site_counter[group_key]

        # 地图数量上限按「地图」算
        seen_maps: list[int] = []
        trimmed: list[dict] = []
        for bucket in ordered:
            if bucket["map_no"] not in seen_maps:
                if len(seen_maps) >= max_maps:
                    continue
                seen_maps.append(bucket["map_no"])
            trimmed.append(bucket)
        return trimmed[: max_sites * max_maps]

    def source_monster_names(self) -> list[str]:
        """所有能给出该素材的怪物名（含傀异调查里的怪异化怪物）。

        用于「来源怪物 N 只」这类计数——不然怪异化素材会显示成「来源怪物 0 只」，
        而牌面上其实列着角龙、雷狼龙、千刃龙。
        """
        names = {entry.get("monster") for entry in self.sources if entry.get("monster")}
        names |= {
            entry.get("monster")
            for entry in self.anomaly_rewards
            if entry.get("monster")
        }
        return sorted(names)

    def maps_for(self, monster_name: str) -> list[str]:
        """某只来源怪物的栖息地图（空列表表示数据缺失）。"""
        return self.monster_maps.get(monster_name) or []

    def gathering_grouped(self, max_maps: int = 6) -> list[dict]:
        """采集点按「地图 → 难度 → 产出」三级聚合。

        Kiranico 原始表是 `地图 | 难度 | 数量 | 概率` 四列、**没有表头**，
        且同一「地图 + 难度」会出现多行——那是**不同的采集点或产出档位**
        （页面没有标记能区分，见 fetch_gathering.parse_gathering 的说明）。

        早先按「一行一个采集点」铺开，卡片上就成了
        「水没林  大师等级 20%  大师等级 10%」：难度重复、数量丢了，
        看起来像两个不明所以的百分比。这里改成三级结构，
        让展示层能写成「难度：20% 得 ×1」这种自解释的形式。

        返回 [{'map':..., 'ranks':[{'rank':..., 'outcomes':[...]}], 'best':...}, ...]
        """
        rank_order = {"下位": 0, "上位": 1, "大师等级": 2, "大师": 2}
        maps: dict[str, dict] = {}

        for point in self.gathering:
            map_name = point.get("map") or "未知地图"
            rank = point.get("rank") or ""
            chance = _chance_value(point.get("chance"))
            entry = maps.setdefault(
                map_name, {"map": map_name, "ranks": {}, "best": 0.0}
            )
            bucket = entry["ranks"].setdefault(rank, {"rank": rank, "outcomes": []})
            bucket["outcomes"].append(
                {
                    "quantity": point.get("quantity") or "",
                    "chance": point.get("chance") or "",
                    "value": chance,
                }
            )
            entry["best"] = max(entry["best"], chance)

        ordered = sorted(maps.values(), key=lambda m: (-m["best"], m["map"]))[:max_maps]
        for entry in ordered:
            ranks = sorted(
                entry["ranks"].values(), key=lambda r: rank_order.get(r["rank"], 9)
            )
            for rank in ranks:
                rank["outcomes"].sort(key=lambda o: -o["value"])
            entry["ranks"] = ranks
        return ordered

    def grouped_by_monster(self, max_monsters: int = 8) -> list[dict]:
        """按怪物聚合，按该怪物的最高概率降序；超出上限时截断。

        返回 [{'name':..., 'map':..., 'entries':[...], 'best':12.0}, ...]
        """
        buckets: dict[str, dict] = {}
        for source in self.sources:
            name = source.get("monster") or "未知"
            bucket = buckets.setdefault(
                name, {"name": name, "entries": [], "best": 0.0}
            )
            bucket["entries"].append(source)
            bucket["best"] = max(bucket["best"], float(source.get("chance") or 0))

        ordered = sorted(buckets.values(), key=lambda b: (-b["best"], b["name"]))
        for bucket in ordered:
            bucket["entries"].sort(
                key=lambda e: (-float(e.get("chance") or 0), e.get("kind") or "")
            )
        return ordered[:max_monsters]


@dataclass
class MonsterInfo:
    """怪物（大型怪或小动物）的完整掉落表。"""

    key: str
    name: str
    alias: str
    drops: list[dict] = field(default_factory=list)
    kind: str = "large"
    map_names: list[str] = field(default_factory=list)
    # 弱点汇总（V1.3.0）：{"elements","motion","part","immune"}。
    # 只取肉质表的通常状态——源数据里没有字段说明各变体对应哪个状态。
    weakness: dict | None = None

    @property
    def is_small(self) -> bool:
        return self.kind == "small"

    def grouped_by_material(self, max_materials: int = 10) -> list[dict]:
        """按**素材**聚合：一个素材一个方块，方块内是它的各种获取方式。

        返回 [{'item_id', 'entries', 'best', 'method_count'}, ...]，
        按该素材的最高获取概率降序（最容易入手的排在前面）。

        每个素材内部按概率降序，并保留难度与方式标签，
        所以方块里读起来就是「这只怪的这份素材怎么拿、各多少概率」。
        """
        buckets: dict[str, dict] = {}
        for drop in self.drops:
            key = str(drop.get("item_id"))
            bucket = buckets.setdefault(
                key, {"item_id": key, "entries": [], "best": 0.0}
            )
            bucket["entries"].append(drop)
            bucket["best"] = max(bucket["best"], float(drop.get("chance") or 0))

        ordered = sorted(buckets.values(), key=lambda b: (-b["best"], b["item_id"]))
        for bucket in ordered:
            bucket["entries"].sort(
                key=lambda d: (
                    -float(d.get("chance") or 0),
                    d.get("rank_label") or "",
                    d.get("kind_label") or "",
                )
            )
            bucket["method_count"] = len(bucket["entries"])
        return ordered[:max_materials]

    def material_count(self) -> int:
        """掉落的素材种数（去重）。"""
        return len({str(drop.get("item_id")) for drop in self.drops})


@dataclass
class QuestInfo:
    """任务的完整信息。"""

    quest_no: int
    name: str
    level: str
    level_num: int
    enemy_level_label: str
    map_name: str
    quest_type: str
    is_event: bool
    targets: list[str] = field(default_factory=list)
    bosses: list[str] = field(default_factory=list)
    rewards: list[dict] = field(default_factory=list)


# --------------------------------------------------------------------------
# 快照
# --------------------------------------------------------------------------


class Snapshot:
    """加载后的快照，带好用的查询接口。"""

    def __init__(self, raw: dict, path: Path | None = None) -> None:
        self.raw = raw
        self.path = path
        self.meta: dict = raw.get("meta") or {}
        tables = raw.get("tables") or {}
        self.items: dict[str, dict] = tables.get("items") or {}
        self.monsters: dict[str, dict] = tables.get("monsters") or {}
        self.small_monsters: dict[str, dict] = tables.get("small_monsters") or {}
        self.quests_raw: list[dict] = tables.get("quests") or []
        self.maps: dict[str, dict] = tables.get("maps") or {}
        self.weapons: dict[str, dict] = tables.get("weapons") or {}
        self.qurious_tables: dict[str, dict] = tables.get("qurious_tables") or {}
        self.decorations: dict[str, dict] = tables.get("decorations") or {}
        self.deco_index: dict = tables.get("deco_index") or {}

        # 繁→简映射供模块级 _normalize() 使用（它就是为查询服务的）
        global _TRAD_SIMP
        _TRAD_SIMP = tables.get("trad_simp") or {}

        self._item_by_name: dict[str, list[str]] = {}
        self._item_by_rarity: dict[int, list[str]] = {}
        self._monster_by_name: dict[str, list[str]] = {}
        self._quest_by_name: dict[str, list[int]] = {}
        self._weapon_by_name: dict[str, list[str]] = {}
        self._skill_to_decos: dict[str, list[str]] = {}

        self._build_indices()
        self._build_recipe_index()

    # ---- 建索引 ---------------------------------------------------------

    def _build_indices(self) -> None:
        for key, item in self.items.items():
            name = (item.get("name") or "").strip()
            if name:
                self._item_by_name.setdefault(name, []).append(key)
            self._item_by_rarity.setdefault(int(item.get("rarity") or 0), []).append(
                key
            )

        for key, monster in self.monsters.items():
            for candidate in (monster.get("name"), monster.get("alias")):
                text = (candidate or "").strip()
                if text:
                    self._monster_by_name.setdefault(text, []).append(key)

        # 小动物并入同一份可搜索索引；key 加 's' 前缀以区分 id 空间
        # （大型怪的 Em 与小动物的 Ems 数值会重叠，例如都有 3）
        self._monster_lookup: dict[str, dict] = dict(self.monsters)
        for ems, monster in self.small_monsters.items():
            prefixed = f"s{ems}"
            self._monster_lookup[prefixed] = monster
            for candidate in (monster.get("name"), monster.get("alias")):
                text = (candidate or "").strip()
                if text:
                    self._monster_by_name.setdefault(text, []).append(prefixed)

        for quest in self.quests_raw:
            name = (quest.get("name") or "").strip()
            if name:
                self._quest_by_name.setdefault(name, []).append(int(quest["quest_no"]))

    # ---- 素材 -----------------------------------------------------------

    def _build_recipe_index(self) -> None:
        """加载时现算「素材 → 可做武器」反查索引。

        ## 为什么不放进快照

        这份索引有 11.7 万条引用、约 **3.2 MB**（快照里最大的一块），
        但它完全可以从武器配方推导——3953 把武器扫一遍是毫秒级。
        宁可加载时多花几十毫秒，也不让快照平白大 3 MB。

        ## 两个索引

        * `_recipes_by_material`：素材被**点名**写进配方（「明确要它」）
        * `_recipes_by_category`：配方要求的是**类别**（「可以拿它顶」）
          —— 后者量大得多（实测怪异化素材一类可命中 887 件），
          所以卡片上只给汇总数字，不逐条列。
        """
        by_material: dict[str, list[tuple[str, str]]] = {}
        by_category: dict[str, list[tuple[str, str]]] = {}

        def add(materials, category, weapon_key: str, system: str) -> None:
            for pair in materials or []:
                if isinstance(pair, list) and pair:
                    by_material.setdefault(str(pair[0]), []).append(
                        (weapon_key, system)
                    )
            if isinstance(category, list) and len(category) >= 3 and category[2]:
                by_category.setdefault(str(category[2]), []).append(
                    (weapon_key, system)
                )

        for weapon_key, weapon in self.weapons.items():
            name = (weapon.get("name") or "").strip()
            if name:
                self._weapon_by_name.setdefault(name, []).append(weapon_key)
            for system, recipe in (weapon.get("recipes") or {}).items():
                if isinstance(recipe, dict):
                    add(
                        recipe.get("materials"),
                        recipe.get("category"),
                        weapon_key,
                        system,
                    )
            table_id = weapon.get("qurious")
            if table_id is not None:
                recipe = self.qurious_tables.get(str(table_id))
                if isinstance(recipe, dict):
                    add(
                        recipe.get("materials"),
                        recipe.get("category"),
                        weapon_key,
                        "qurious",
                    )

        self._recipes_by_material = by_material
        self._recipes_by_category = by_category
        for skill, keys in (self.deco_index.get("by_skill") or {}).items():
            self._skill_to_decos[skill] = list(keys)

    # ---- 武器 -----------------------------------------------------------

    def weapon(self, key: str) -> dict | None:
        return self.weapons.get(key)

    def recipes_of(self, weapon_key: str) -> dict[str, dict]:
        """某把武器的全部配方（含从共享表展开的怪异炼化）。"""
        weapon = self.weapons.get(weapon_key) or {}
        result = {
            system: recipe
            for system, recipe in (weapon.get("recipes") or {}).items()
            if isinstance(recipe, dict)
        }
        table_id = weapon.get("qurious")
        if table_id is not None:
            recipe = self.qurious_tables.get(str(table_id))
            if isinstance(recipe, dict):
                result["qurious"] = recipe
        return result

    def weapons_for_material(
        self, item_id: str, weapon_type: str | None = None
    ) -> tuple[list[dict], list[dict]]:
        """返回 (点名要它的武器, 类别上可以拿它顶的武器)。

        `weapon_type` 传武器表键（如 `long_sword`）时只返回该类型。
        """
        item = self.items.get(str(item_id)) or {}
        categories = item.get("categories") or []

        def collect(refs: list[tuple[str, str]]) -> list[dict]:
            seen: dict[str, dict] = {}
            for weapon_key, system in refs:
                weapon = self.weapons.get(weapon_key)
                if not weapon:
                    continue
                if weapon_type and weapon.get("type") != weapon_type:
                    continue
                entry = seen.setdefault(
                    weapon_key, {**weapon, "key": weapon_key, "systems": []}
                )
                if system not in entry["systems"]:
                    entry["systems"].append(system)
            return sorted(
                seen.values(),
                key=lambda w: (-int(w.get("rarity") or 0), w.get("name") or ""),
            )

        specific = collect(self._recipes_by_material.get(str(item_id), []))
        category: list[dict] = []
        seen_keys = {entry["key"] for entry in specific}
        for name in categories:
            for ref in self._recipes_by_category.get(name, []):
                weapon = self.weapons.get(ref[0])
                if not weapon or ref[0] in seen_keys:
                    continue
                if weapon_type and weapon.get("type") != weapon_type:
                    continue
                seen_keys.add(ref[0])
                category.append({**weapon, "key": ref[0], "systems": [ref[1]]})
        return specific, category

    def search_weapons(self, query: str, limit: int = 12) -> list[Match]:
        return self._search(query, self._weapon_by_name, self.weapons, limit)

    # ---- 装饰品 ---------------------------------------------------------

    def decorations_for_material(self, item_id: str) -> list[dict]:
        keys = (self.deco_index.get("by_material") or {}).get(str(item_id)) or []
        return [self.decorations[key] for key in keys if key in self.decorations]

    def decorations_for_skill(self, skill: str) -> list[dict]:
        keys = self._skill_to_decos.get(skill) or []
        result = [self.decorations[key] for key in keys if key in self.decorations]
        return sorted(
            result, key=lambda d: (int(d.get("slot") or 0), d.get("name") or "")
        )

    def search_skills(self, query: str, limit: int = 12) -> list[Match]:
        """按技能名搜索（技能不是实体表，这里合成 Match 供候选提示用）。"""
        index = {name: [name] for name in self._skill_to_decos}
        table = {name: {"name": name, "skill": name} for name in self._skill_to_decos}
        return self._search(query, index, table, limit)

    def material(self, item_id: str) -> MaterialInfo | None:
        item = self.items.get(str(item_id))
        if not item:
            return None
        sources = item.get("sources") or []
        anomaly = item.get("anomaly_rewards") or []
        # 来源怪物计数要把傀异调查里的怪异化怪物算进去，
        # 否则怪异化素材会显示「来源怪物 0 只」却列着一堆怪物。
        monsters = {s.get("monster") for s in sources}
        monsters |= {a.get("monster") for a in anomaly}
        monsters.discard(None)
        return MaterialInfo(
            item_id=str(item_id),
            name=item.get("name") or "",
            rarity=int(item.get("rarity") or 0),
            item_type=item.get("type") or "",
            sources=sources,
            monster_count=len(monsters),
            gathering=item.get("gathering") or [],
            monster_maps=self._maps_by_monster_name(),
            anomaly_rewards=anomaly,
            gathering_nodes=item.get("gathering_nodes") or [],
        )

    def _maps_by_monster_name(self) -> dict[str, list[str]]:
        """怪物名 -> 栖息地图。用于在素材卡片上标出「去哪张图打」。

        大型怪与小动物同名时取并集（例如「镰鼬龙」既是大型怪也是小动物条目）。
        """
        if getattr(self, "_maps_cache", None) is None:
            merged: dict[str, list[str]] = {}
            for table in (self.monsters, self.small_monsters):
                for entry in table.values():
                    name = entry.get("name")
                    maps = entry.get("map_names") or []
                    if not name or not maps:
                        continue
                    bucket = merged.setdefault(name, [])
                    for item in maps:
                        if item not in bucket:
                            bucket.append(item)
            self._maps_cache = merged
        return self._maps_cache

    def search_materials(self, query: str, limit: int = 12) -> list[Match]:
        """素材名匹配：精确 → 前缀 → 包含，逐级放宽。"""
        return self._search(query, self._item_by_name, self.items, limit)

    # ---- 怪物 -----------------------------------------------------------

    def monster(self, key: str) -> MonsterInfo | None:
        """按 key 取怪物；key 以 's' 前缀表示小动物。"""
        text = str(key)
        if text.startswith("s"):
            raw = self.small_monsters.get(text[1:])
            if not raw:
                return None
            return MonsterInfo(
                key=text,
                name=raw.get("name") or "",
                alias=raw.get("alias") or "",
                drops=raw.get("drops") or [],
                kind="small",
                map_names=list(raw.get("map_names") or []),
            )

        raw = self.monsters.get(text)
        if not raw:
            return None
        return MonsterInfo(
            key=text,
            name=raw.get("name") or "",
            alias=raw.get("alias") or "",
            drops=raw.get("drops") or [],
            kind=raw.get("kind") or "large",
            map_names=list(raw.get("map_names") or []),
            weakness=raw.get("weakness"),
        )

    def search_monsters(self, query: str, limit: int = 12) -> list[Match]:
        """怪物名匹配，顺带查别名。大型怪与小动物一起搜（小动物 key 加 's' 前缀）。"""
        return self._search(query, self._monster_by_name, self._monster_lookup, limit)

    # ---- 任务 -----------------------------------------------------------

    def quest(self, quest_no: int) -> QuestInfo | None:
        for raw in self.quests_raw:
            if int(raw.get("quest_no") or -1) == int(quest_no):
                return self._quest_info(raw)
        return None

    def search_quests(self, query: str, limit: int = 12) -> list[Match]:
        """任务名匹配。任务没有别名，只按名字。"""
        matches: list[Match] = []
        for name, quest_nos in self._quest_by_name.items():
            score = _score(query, name)
            if score is None:
                continue
            for quest_no in quest_nos:
                matches.append(Match(key=str(quest_no), name=name, score=score))
        matches.sort(key=lambda m: (m.score, m.name))
        return matches[:limit]

    def quests_rewarding(self, item_id: str) -> list[QuestInfo]:
        """哪些任务的奖励里含该素材。"""
        target = str(item_id)
        found: list[QuestInfo] = []
        for raw in self.quests_raw:
            rewards = raw.get("rewards") or []
            if any(str(r.get("item_id")) == target for r in rewards):
                found.append(self._quest_info(raw))
        return found

    def top_quests_rewarding(
        self, item_id: str, limit: int = 5
    ) -> tuple[list[dict], int]:
        """产出该素材概率最高的若干个任务（走任务的通用素材报酬），返回 (任务列表, 总数)。

        注意：这条链路只覆盖约 5% 的素材（蜂蜜、蘑菇这类消耗品为主）。怪物素材
        的任务信息要靠 top_quests_dropping()。
        """
        target = str(item_id)
        scored: list[dict] = []
        for raw in self.quests_raw:
            chances = [
                float(r.get("chance") or 0)
                for r in (raw.get("rewards") or [])
                if str(r.get("item_id")) == target
            ]
            if not chances:
                continue
            info = self._quest_info(raw)
            scored.append(
                {
                    "quest_no": info.quest_no,
                    "name": info.name,
                    "level": info.level,
                    "level_num": info.level_num,
                    "enemy_level_label": info.enemy_level_label,
                    "map": info.map_name,
                    "type": info.quest_type,
                    "is_event": info.is_event,
                    "chance": max(chances),
                }
            )
        scored.sort(key=lambda q: (-q["chance"], q["quest_no"]))
        return scored[:limit], len(scored)

    def top_quests_dropping(
        self, material: MaterialInfo, limit: int = 5
    ) -> tuple[list[dict], int]:
        """「去哪个任务刷这个素材」——按任务目标报酬推导。

        做法：素材 → 哪些怪物在「目标报酬」里给（这是任务结算奖励），
        再找 boss_em_type 含这些怪物的任务。用 boss_em_type 而不是
        tgt_em_type，因为任务目标栏可能只写 1~2 只，实际出场怪物更多。
        """
        # 怪物名 -> 该怪物给出的最高目标报酬概率
        by_monster: dict[str, float] = {}
        for source in material.sources:
            if source.get("kind") != "target_reward":
                continue
            name = source.get("monster") or ""
            if name:
                by_monster[name] = max(
                    by_monster.get(name, 0.0), float(source.get("chance") or 0)
                )
        if not by_monster:
            return [], 0

        scored: list[dict] = []
        for raw in self.quests_raw:
            for boss in raw.get("bosses") or []:
                if boss in by_monster:
                    info = self._quest_info(raw)
                    scored.append(
                        {
                            "quest_no": info.quest_no,
                            "name": info.name,
                            "level": info.level,
                            "level_num": info.level_num,
                            "enemy_level_label": info.enemy_level_label,
                            "map": info.map_name,
                            "type": info.quest_type,
                            "is_event": info.is_event,
                            "monster": boss,
                            "chance": by_monster[boss],
                        }
                    )
                    break
        scored.sort(key=lambda q: (-q["chance"], q["quest_no"]))
        return scored[:limit], len(scored)

    def _quest_info(self, raw: dict) -> QuestInfo:
        map_no = raw.get("map_no")
        map_name = raw.get("map") or self.map_name(map_no)
        return QuestInfo(
            quest_no=int(raw.get("quest_no") or 0),
            name=raw.get("name") or "",
            level=raw.get("level") or "",
            level_num=int(raw.get("level_num") or 0),
            enemy_level_label=raw.get("enemy_level_label") or "",
            map_name=map_name,
            quest_type=raw.get("type") or "",
            is_event=bool(raw.get("is_event")),
            targets=list(raw.get("targets") or []),
            bosses=list(raw.get("bosses") or []),
            rewards=list(raw.get("rewards") or []),
        )

    # ---- 地图 -----------------------------------------------------------

    def map_name(self, map_no: Any) -> str:
        if map_no is None:
            return ""
        entry = self.maps.get(str(map_no))
        if not entry:
            return ""
        return entry.get("name") or ""

    def map_is_inferred(self, map_no: Any) -> bool:
        entry = self.maps.get(str(map_no))
        return bool(entry) and entry.get("origin") == "inferred"

    # ---- 供帮助/统计使用 ------------------------------------------------

    @property
    def generated_at(self) -> str:
        return self.meta.get("generated_at") or ""

    def stats(self) -> dict:
        return {
            "materials": len(self.items),
            "monsters": len(self.monsters),
            "small_monsters": len(self.small_monsters),
            "quests": len(self.quests_raw),
            "maps": len(self.maps),
        }

    def rare_material_names(self, limit: int = 8) -> list[str]:
        """挑几个稀有素材当帮助里的示例。"""
        names: list[str] = []
        for rarity in (9, 8, 7):
            for key in self._item_by_rarity.get(rarity, []):
                name = self.items.get(key, {}).get("name")
                if name and name not in names:
                    names.append(name)
                if len(names) >= limit:
                    return names
        return names

    # ---- 内部 -----------------------------------------------------------

    @staticmethod
    def _search(
        query: str,
        name_index: dict[str, list[str]],
        table: dict[str, dict],
        limit: int,
    ) -> list[Match]:
        query = (query or "").strip()
        if not query:
            return []

        matches: list[Match] = []
        for name, keys in name_index.items():
            score = _score(query, name)
            if score is None:
                continue
            for key in keys:
                entry = table.get(key) or {}
                display = entry.get("name") or name
                # 别名命中的条目要提示一下，否则用户看不出为什么匹配上了；
                # 归一化命中（例如漏了「的」）也算模糊，同样要提示
                # —— 否则用户会以为「火龙天鳞」真的存在。
                if entry.get("name") != name:
                    note = f"别名：{name}"
                elif score >= _MATCH_NORMALIZED:
                    note = f"已自动匹配为「{display}」"
                else:
                    note = ""
                matches.append(Match(key=key, name=display, score=score, note=note))

        # 精确/前缀/包含/归一化全都没命中时，才动用编辑距离。
        # 放在最后是为了让「精确命中」永远优先，模糊只作兜底。
        # `_search` 是 staticmethod，所以这里按类名调用。
        if not matches:
            matches = Snapshot._fuzzy_matches(query, name_index, table)

        # 同一个 key 可能既被名字又被别名命中，去重保留最高分；
        # 小动物的多个 Ems（精灵鹿有 3 和 1283）会给出重名条目，
        # 这里按「显示名」再去一次重，避免候选列表出现「精灵鹿、精灵鹿」。
        best: dict[str, Match] = {}
        by_display: dict[str, Match] = {}
        for match in matches:
            current = best.get(match.key)
            if current is None or match.score < current.score:
                best[match.key] = match

        for match in sorted(
            best.values(), key=lambda m: (m.score, m.rank, len(m.name), m.name)
        ):
            # 重名的保留最靠前（匹配度最高）的那条
            kept = by_display.get(match.name)
            if kept is None or (match.score, match.rank, len(match.key)) < (
                kept.score,
                kept.rank,
                len(kept.key),
            ):
                by_display[match.name] = match

        ordered = sorted(
            by_display.values(), key=lambda m: (m.score, m.rank, len(m.name), m.name)
        )
        return ordered[:limit]

    @staticmethod
    def _fuzzy_matches(
        query: str,
        name_index: dict[str, list[str]],
        table: dict[str, dict],
    ) -> list[Match]:
        """编辑距离兜底（免依赖，不做拼音）。

        * 归一化后距离 **<= 1**：候选**唯一**时自动匹配并提示
          「已自动匹配为「搔鸟」」；候选**多个**时**绝不替用户猜**，
          把它们按「形近程度」排好序、标记 `ambiguous=True`
          交给命令层列出来让用户选。实测「骚鸟」同时接近
          搔鸟 / 伞鸟 / 丸鸟——这时候替你选一个才是错的。
        * 距离 **2 只在候选唯一时**才采用。否则返回空，让上层提示「没找到」。
        """
        target = _normalize(query)
        if not target:
            return []
        close: list[tuple[int, str, str]] = []
        far: list[tuple[int, str, str]] = []
        for name, keys in name_index.items():
            candidate = _normalize(name)
            if not candidate:
                continue
            distance = _edit_distance(target, candidate, max_distance=2)
            bucket = close if distance == 1 else far if distance == 2 else None
            if bucket is None:
                continue
            penalty = _shape_penalty(target, candidate)
            for key in keys:
                bucket.append((penalty, name, key))

        # 形近程度只用来**排序**（把最像的排前面），不用来替用户拍板
        close.sort(key=lambda item: (item[0], item[1]))
        far.sort(key=lambda item: (item[0], item[1]))

        if close:
            chosen = close
            score = _MATCH_FUZZY_1
            ambiguous = len({name for _p, name, _k in chosen}) > 1
        elif len({name for _p, name, _k in far}) == 1:
            # 距离 2 还要看它占名字多长——3 个字错 2 个其实并不接近
            # （实测「攻击珠」会唯一命中「电击袋」，纯属噪音）。
            # 只在「错字占比 <= 1/3」时接受。
            only = far[0][1]
            longest = max(len(target), len(_normalize(only)))
            if longest == 0 or 2 / longest > 1 / 3:
                return []
            chosen = far
            score = _MATCH_FUZZY_2
            ambiguous = False
        else:
            return []

        result: list[Match] = []
        for rank, (_penalty, name, key) in enumerate(chosen):
            entry = table.get(key) or {}
            display = entry.get("name") or name
            if ambiguous:
                note = "相似候选"
            elif rank == 0:
                note = f"已自动匹配为「{display}」"
            else:
                note = ""
            result.append(
                Match(
                    key=key,
                    name=display,
                    score=score,
                    note=note,
                    rank=rank,
                    ambiguous=ambiguous,
                )
            )
        return result


def _chance_value(chance) -> float:
    """把「35%」这类文本概率解析成数字，解析不了回 0。"""
    if isinstance(chance, (int, float)):
        return float(chance)
    if not chance:
        return 0.0
    match = re.search(r"(\d+(?:\.\d+)?)", str(chance))
    return float(match.group(1)) if match else 0.0


# 归一化时丢弃的字符：「的」这类虚词、各种中点与空白。
# 这就是「火龙天鳞」能匹配到「火龙的天鳞」的原因。
_STRIPPED_CHARS = frozenset("的・·∙• 　\t\u00a0")

# 繁→简映射。**不手写**：由快照里的 `trad_simp` 提供，
# 而那张表是提取时从游戏数据自己的繁中/简中并列文本生成的。
_TRAD_SIMP: dict[str, str] = {}


def _shape_penalty(query: str, name: str) -> int:
    """两个字串在「同一个位置换了字」时，换掉的字有多不像（越小越像）。

    免依赖的近似办法：CJK 统一汉字区按**部首与笔画**排序，形近字往往相邻，
    所以用码位距离作近似。

    实测用例：`骚鸟` 有两个距离 1 的候选——`搔鸟`（搔/骚 共享「蚤」部件）
    与 `丸鸟`。单看编辑距离无法区分，而这个惩罚值把 `搔鸟` 排在前面，
    正是用户期望的结果（他报的例子就是「搔鸟」误打成「骚鸟」）。
    长度不等（插入/删除）时返回 0，不参与排序。
    """
    if len(query) != len(name):
        return 0
    return sum(abs(ord(a) - ord(b)) for a, b in zip(query, name) if a != b)


def _normalize(text: str) -> str:
    """查询/名称的归一化形式，用于模糊匹配。

    处理四件事：
    1. **全角→半角**（用户输入法很容易打出全角字母数字）
    2. **去掉分隔与虚词** —— 「的」「・」「·」「空格」。这就是「火龙天鳞」能匹配到
       「火龙的天鳞」的原因。
    3. **繁→简** —— 用从快照里带的 `trad_simp` 表（该表由游戏数据自己的
       繁中/简中并列条目生成，不是手写表）。
    4. 去掉首尾空白
    """
    if not text:
        return ""
    result = []
    for char in text:
        code = ord(char)
        # 全角 ASCII（！-～）→ 半角
        if 0xFF01 <= code <= 0xFF5E:
            char = chr(code - 0xFEE0)
        elif code == 0x3000:
            char = " "
        if char in _STRIPPED_CHARS:
            continue
        result.append(_TRAD_SIMP.get(char, char))
    return "".join(result).strip()


def _edit_distance(left: str, right: str, max_distance: int = 2) -> int:
    """Levenshtein 距离，超过 max_distance 提前返回 max_distance + 1。

    名称都很短（素材名 3–8 字），全表扫描是毫秒级，不需要索引。
    """
    if left == right:
        return 0
    if abs(len(left) - len(right)) > max_distance:
        return max_distance + 1
    previous = list(range(len(right) + 1))
    for i, char_left in enumerate(left, start=1):
        current = [i]
        best = i
        for j, char_right in enumerate(right, start=1):
            cost = 0 if char_left == char_right else 1
            value = min(
                previous[j] + 1,  # 删除
                current[j - 1] + 1,  # 插入
                previous[j - 1] + cost,  # 替换
            )
            current.append(value)
            best = min(best, value)
        if best > max_distance:
            return max_distance + 1
        previous = current
    return previous[-1]


def _score(query: str, name: str) -> int | None:
    """返回匹配分（越小越精确），不匹配返回 None。"""
    if not query or not name:
        return None
    if name == query:
        return _MATCH_EXACT
    if name.startswith(query):
        return _MATCH_PREFIX
    if query in name:
        return _MATCH_CONTAINS
    normalized_query = _normalize(query)
    if normalized_query and normalized_query == _normalize(name):
        return _MATCH_NORMALIZED
    return None


# --------------------------------------------------------------------------
# 单例加载
# --------------------------------------------------------------------------

_SNAPSHOT: Snapshot | None = None


def load_snapshot(path: Path | None = None) -> Snapshot:
    """加载快照（进程内缓存）。"""
    global _SNAPSHOT
    if path is None and _SNAPSHOT is not None:
        return _SNAPSHOT

    candidates = [path] if path else _candidate_paths()
    for candidate in candidates:
        if candidate and candidate.exists():
            try:
                with candidate.open("r", encoding="utf-8") as handle:
                    raw = json.load(handle)
            except json.JSONDecodeError as exc:
                raise SnapshotError(f"快照解析失败：{candidate}（{exc}）") from exc
            snapshot = Snapshot(raw, candidate)
            if path is None:
                _SNAPSHOT = snapshot
            return snapshot

    searched = "、".join(str(p) for p in candidates if p)
    raise SnapshotError(
        f"未找到快照文件，已查找：{searched}。请先运行 tools/extract.py 生成。"
    )


def search_suggestions(snapshot: Snapshot, query: str, limit: int = 10) -> list[str]:
    """候选列表用的紧凑展示文本。"""
    out: list[str] = []
    for match in snapshot.search_materials(query, limit=limit):
        material = snapshot.material(match.key)
        if material:
            out.append(f"{material.name}（稀有度 {material.rarity}）")
    return out


def iter_source_labels(snapshot: Snapshot) -> Iterable[tuple[str, str]]:
    labels = (snapshot.raw.get("tables") or {}).get("source_labels") or {}
    return labels.items()
