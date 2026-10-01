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

    @property
    def is_rare(self) -> bool:
        """稀有素材：稀有度 >= 7（玉/天鳞/秘棘一档）。"""
        return self.rarity >= 7

    def maps_for(self, monster_name: str) -> list[str]:
        """某只来源怪物的栖息地图（空列表表示数据缺失）。"""
        return self.monster_maps.get(monster_name) or []

    def gathering_by_map(self, max_maps: int = 6) -> list[dict]:
        """采集点按地图聚合，地图内按难度、概率排序。

        返回 [{'map':..., 'entries':[...], 'best':35.0}, ...]
        """
        rank_order = {"下位": 0, "上位": 1, "大师等级": 2, "大师": 2}
        buckets: dict[str, dict] = {}
        for point in self.gathering:
            map_name = point.get("map") or "未知地图"
            bucket = buckets.setdefault(
                map_name, {"map": map_name, "entries": [], "best": 0.0}
            )
            bucket["entries"].append(point)
            bucket["best"] = max(bucket["best"], _chance_value(point.get("chance")))

        ordered = sorted(buckets.values(), key=lambda b: (-b["best"], b["map"]))
        for bucket in ordered:
            bucket["entries"].sort(
                key=lambda p: (
                    rank_order.get(p.get("rank") or "", 9),
                    -_chance_value(p.get("chance")),
                )
            )
        return ordered[:max_maps]

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

    @property
    def is_small(self) -> bool:
        return self.kind == "small"

    def grouped_by_rank_then_kind(self) -> list[tuple[str, list[dict]]]:
        """按难度分组，组内按概率降序。

        部位破坏的条目会把部位名合并到 label 里（例如「部位破坏报酬·头部」），
        否则同一只怪会出现十几行「部位破坏报酬」却看不出差在哪。
        """
        order = {"Master": 0, "High": 1, "Low": 2, "": 3}
        buckets: dict[str, list[dict]] = {}
        for drop in self.drops:
            buckets.setdefault(drop.get("rank") or "", []).append(drop)
        result = []
        for rank in sorted(buckets, key=lambda r: order.get(r, 9)):
            entries = sorted(buckets[rank], key=lambda d: -float(d.get("chance") or 0))
            prepared = [self._with_part_label(entry) for entry in entries]
            result.append((rank, prepared))
        return result

    @staticmethod
    def _with_part_label(entry: dict) -> dict:
        """给部位破坏的记录补一个带部位名的 label。"""
        if entry.get("kind") != "parts_break_reward" or not entry.get("part_name"):
            return entry
        labelled = dict(entry)
        labelled["kind_label"] = (
            f"{entry.get('kind_label') or '部位破坏报酬'}·{entry['part_name']}"
        )
        return labelled


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

        self._item_by_name: dict[str, list[str]] = {}
        self._item_by_rarity: dict[int, list[str]] = {}
        self._monster_by_name: dict[str, list[str]] = {}
        self._quest_by_name: dict[str, list[int]] = {}

        self._build_indices()

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

    def material(self, item_id: str) -> MaterialInfo | None:
        item = self.items.get(str(item_id))
        if not item:
            return None
        sources = item.get("sources") or []
        monsters = {s.get("monster") for s in sources}
        return MaterialInfo(
            item_id=str(item_id),
            name=item.get("name") or "",
            rarity=int(item.get("rarity") or 0),
            item_type=item.get("type") or "",
            sources=sources,
            monster_count=len(monsters),
            gathering=item.get("gathering") or [],
            monster_maps=self._maps_by_monster_name(),
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
                # 别名命中的条目要提示一下，否则用户看不出为什么匹配上了
                note = ""
                if entry.get("name") != name:
                    note = f"别名：{name}"
                matches.append(
                    Match(
                        key=key, name=entry.get("name") or name, score=score, note=note
                    )
                )

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
            best.values(), key=lambda m: (m.score, len(m.name), m.name)
        ):
            # 重名的保留最靠前（匹配度最高）的那条
            kept = by_display.get(match.name)
            if kept is None or (match.score, len(match.key)) < (
                kept.score,
                len(kept.key),
            ):
                by_display[match.name] = match

        ordered = sorted(
            by_display.values(), key=lambda m: (m.score, len(m.name), m.name)
        )
        return ordered[:limit]


def _chance_value(chance) -> float:
    """把「35%」这类文本概率解析成数字，解析不了回 0。"""
    if isinstance(chance, (int, float)):
        return float(chance)
    if not chance:
        return 0.0
    match = re.search(r"(\d+(?:\.\d+)?)", str(chance))
    return float(match.group(1)) if match else 0.0


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
