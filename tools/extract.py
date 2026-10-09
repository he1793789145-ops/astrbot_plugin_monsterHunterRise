#!/usr/bin/env python3
"""把 mhrice 源数据提取成插件用的紧凑快照。

用法::

    python extract.py                     # 用默认源路径
    python extract.py --out snapshot.json
    python extract.py --mhrice D:\\path\\mhrice.json --items D:\\path\\items.json
    python extract.py --pretty            # 缩进输出，便于人工检查

源数据是《怪物猎人崛起：曙光》的游戏文件转储，118MB 的 mhrice.json 里绝大多数
内容（防具/技能/装饰品/生态参数）与素材查询无关，所以这里只抽出五类数据：

    items     素材名、稀有度、类型
    monsters  怪物名、别名
    drops     掉落来源（反向索引，按素材聚合）
    quests    任务名、星级、地图、目标、奖励
    maps      map_no -> 地图名

产物写到 AstrBot 的 plugin_data 目录，插件运行时只读快照。
源文件更新后重跑本脚本即可，不必改插件代码。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# 常量：源数据里的枚举与本地化列
# --------------------------------------------------------------------------

# 消息表 content 数组里各语言的列号（实测自 I_0427_Name = 龍玉/龙玉）
COL_TRADITIONAL = 12
COL_SIMPLIFIED = 13

# 掉落表里 6 类来源：内部键 -> 中文标签
SOURCE_KINDS = [
    ("target_reward", "目标报酬"),
    ("hagitory_reward", "剥取"),
    ("capture_reward", "捕获报酬"),
    ("parts_break_reward", "部位破坏报酬"),
    ("drop_reward", "掉落物"),
    ("otomo_reward", "随从"),
]

# quest_rank -> 中文难度
RANK_LABELS = {"Low": "下位", "High": "上位", "Master": "大师"}

# quest_type 枚举 -> 中文标签
QUEST_TYPE_LABELS = {
    "HUNTING": "狩猎",
    "CAPTURE": "捕获",
    "KILL": "讨伐",
    "COLLECTS": "采集",
    "BOSSRUSH": "百龙夜行",
    "ARENA": "斗技场",
    "TOUR": "探索",
    "HYAKURYU": "百龙夜行",
    "TRAINING": "训练",
    "SPECIAL": "特殊",
}

# 每种掉落方式的「每个类型占多少槽」。
#
# 关键结构（此前理解错了，导致部位名只覆盖 59%）：
#   `{kind}_item_id_list` 的长度 = **类型列长度 × 10**，每个类型固定占 10 槽。
#   类型列是同一个行里的另一个字段：
#     parts_break_reward -> parts_break_list（RandomId，部位身份）
#     hagitory_reward    -> enemy_reward_type_list（MainBody / PartsLoss1 …）
#     drop_reward        -> drop_reward_type_list（DropItem / DropItem2 …）
#     target/capture/otomo -> 无类型列，即 1 个类型
#
# 实测六种方式在全部 248 行上都满足「长度 == 类型数 × 10」，无一例外。
#
# 踩过的坑：早先把类型列里的 `"None"` 占位符过滤掉再算组数，于是
# 爵银龙 3 个有效部位被当成「3 组 × 20 槽」，伞鸟 4 个被当成「4 组 × 15 槽」，
# 只能靠反推猜分段，部位名覆盖只有 59%。保留 "None" 占位符后，
# 组数取类型列全长、槽宽恒为 10，一切自洽。
SLOTS_PER_TYPE = 10

# 剥取来源（enemy_reward_type_list）-> 中文标签。
# 实测：MainBody 是本体剥取；PartsLoss1 的首个物品名 79/95 含「尾」
# （其余是「雌火龙的棘」这类同样来自尾巴的素材），故为**尾巴剥取**；
# PartsLoss2 实测为土砂龙的头壳/背甲，属另一个可切断部位。
CARVE_TYPE_LABELS = {
    "MainBody": "本体",
    "PartsLoss1": "尾巴",
    "PartsLoss2": "切断部位",
    "Unique1": "特殊",
}

# 掉落物来源（drop_reward_type_list）-> 中文标签。
# DropItem/DropItem2… 是不同掉落条件（打击掉落、环境掉落等），
# 源数据没有可读名字，统一显示为「掉落物」。
DROP_TYPE_LABELS = {
    "DropItem": "",
    "DropItem2": "",
    "DropItem3": "",
    "DropItem4": "",
    "DropItem5": "",
    "DropItem6": "",
}

# 部位名（游戏数据里是日文）-> 中文。
# 取自 `monsters[].collider_mapping.part_map` 的实际用词，只翻译常见部位；
# 未收录的词直接保留原文，避免误译成错误的部位。
PART_NAME_ZH = {
    "頭部": "头部",
    "頭": "头部",
    "首": "颈部",
    "胴体": "躯干",
    "胴": "躯干",
    "腹部": "腹部",
    "お腹": "腹部",
    "背中": "背部",
    "翼": "翼",
    "左翼": "左翼",
    "右翼": "右翼",
    "尻尾": "尾巴",
    "尾": "尾巴",
    "左脚": "左脚",
    "右脚": "右脚",
    "左前脚": "左前脚",
    "右前脚": "右前脚",
    "前脚": "前脚",
    "左後脚": "左后脚",
    "右後脚": "右后脚",
    "後脚": "后脚",
    "左腕": "左腕",
    "右腕": "右腕",
    "腕": "腕",
    "脚": "脚",
    "翼脚": "翼脚",
    "翼脚_右": "右翼脚",
    "角": "角",
    "牙": "牙",
}

# map_no -> 地图名。Stage_Name_01.._11 实测取自 map_name 表；
# 12 / 13 在源数据里没有名字，是按任务目标怪物推断的，故单独标注来源。
MAP_NAMES_VERIFIED = {
    1: "废神社",
    2: "沙原",
    3: "水没林",
    4: "冰封群岛",
    5: "熔岩洞",
    7: "翡叶要塞",
    9: "狱泉乡",
    10: "斗技场",
    11: "龙宫古城",
}
MAP_NAMES_INFERRED = {
    12: "密林",
    13: "城塞高地",
}
# 只在证明推断成立时才写进快照；若源数据自带这些编号的名字，以源数据为准。
MAP_NAMES_MR = {
    31: "密林",
    32: "城塞高地",
    41: "塔之秘境",
    42: "渊劫地狱",
}

HASH_CHUNK = 1 << 20


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def localize(entry: dict, prefer_simplified: bool = True) -> str:
    """从消息表条目里取中文名，简体优先，回落繁体再回落日文。"""
    content = entry.get("content") or []
    order = (
        (COL_SIMPLIFIED, COL_TRADITIONAL, 0)
        if prefer_simplified
        else (COL_TRADITIONAL, COL_SIMPLIFIED, 0)
    )
    for idx in order:
        if idx < len(content) and content[idx]:
            text = content[idx].strip()
            if text:
                return text
    return ""


def strip_tags(text: str) -> str:
    """去掉游戏文本里的富文本标记，并折叠空白。"""
    if not text:
        return ""
    text = re.sub(r"<[^>]*>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def is_rejected(text: str) -> bool:
    """源数据里被标记为废弃的条目（名字里带 #Rejected#）。"""
    return "#Rejected#" in text or not text.strip()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def unwrap(node):
    """有些表外层包了 {'param': [...]} 或 {'data_list': [...]}，这里取内层数组。"""
    if isinstance(node, list):
        return node
    if isinstance(node, dict):
        for key in ("param", "data_list", "entries"):
            inner = node.get(key)
            if isinstance(inner, list):
                return inner
    return []


def unwrap_entries(node) -> list:
    """消息表统一是 {'entries': [...]}。"""
    if isinstance(node, dict):
        entries = node.get("entries")
        if isinstance(entries, list):
            return entries
    return []


# --------------------------------------------------------------------------
# 各表提取
# --------------------------------------------------------------------------


def build_item_names(mhrice: dict) -> dict:
    """items_name_msg: I_0427_Name -> 简体中文名。"""
    names = {}
    for entry in unwrap_entries(mhrice.get("items_name_msg")):
        key = entry.get("name") or ""
        match = re.fullmatch(r"I_(\d+)_Name", key)
        if not match:
            continue
        text = localize(entry)
        if text:
            names[int(match.group(1))] = text
    return names


# 大师区间（enemy_type >= 76）到 monster_names_mr 的显式下标映射。
#
# 为什么用显式表而不是公式：这个区间的编号规律不成立——
# enemy_type 与数组下标的偏移在不同段不一致（82→9、93→20、94→21、107→31…），
# 试过的 et-73 / et-6 / 直接索引都会有 5~8 只错位。大师表只有 40 项，
# 且全部来自 monster_names_mr，逐条列举比猜公式可靠。
MR_INDEX_BY_ENEMY_TYPE = {
    76: 3,
    77: 4,
    78: 5,
    79: 6,
    80: 7,
    81: 8,
    82: 9,
    83: 10,
    84: 11,
    85: 12,
    86: 13,
    87: 14,
    88: 15,
    89: 16,
    90: 17,
    91: 18,
    92: 19,
    93: 20,
    94: 21,
    95: 22,
    96: 23,
    97: 24,
    98: 25,
    99: 26,
    100: 27,
    101: 28,
    102: 29,
    103: 30,
    104: 31,
    105: 32,
    106: 33,
    107: 31,
    108: 34,
    109: 33,
    110: 37,
    111: 35,
    112: 39,
    113: 34,
    114: 36,
    115: 38,
}

# 源数据无法区分、需人工指定的条目：这些 em 的掉落物名与基础种相同，
# 只能靠编号判定（em 2072/2073/2075/124 是「怪异克服」系列，掉落物沿用基础种名）。
EM_NAME_OVERRIDES = {
    2072: "怪异克服钢龙",
    2073: "怪异克服霞龙",
    2075: "怪异克服炎王龙",
    124: "冰呪龙",
}
# 注：2072/2073/2075 的掉落物名与基础种相同（都叫「钢龙的刚爪」等），
# 只能靠编号判定；「怪异克服天彗龙」在名字表里自带，不需要覆盖。

# 小动物的名字表（Ems -> 名字），只用于**无法从编号推出**的少数几只。
#
# 判定顺序见 build_monsters 里的小动物分支：
#   1. 名字表[enemy_type]（主规则，实测与掉落物一致）
#   2. 掉落物前缀签名（小动物掉自己的素材，「丸鸟的羽」-> 丸鸟）
#   3. 本表（兜底，用于编号对不上且掉落物不带自己名字的少数几只）
#
# 曾经踩过的坑：早先这张表是**按名字表位置逐条硬写的**，整体错位一位，
# 结果「药草」被标成毒狗龙的掉落（实际是丸鸟），24 只小动物全错。
# 现在改为以编号为主、掉落物校验，本表只留真正需要人工指定的。
SMALL_NAME_OVERRIDES = {
    # 掉落物不带自己名字（掉的是「温暖的毛皮 / 优质的毛皮 / 极品毛皮」这类通用素材），
    # 编号映射也会落到别的怪身上，所以只能人工指定
    3: "精灵鹿",
    1283: "精灵鹿",
    # 掉落物是通用素材，签名与编号都判不出
    7: "艾露猫",
    8: "梅拉露",
    44: "变形幼冰鲨",
}


def build_monsters(mhrice: dict, items: dict) -> dict:
    """返回 em -> 怪物信息。

    ## 为什么这里这么绕

    `mhrice.json` 里怪物名和怪物数据之间**没有可靠的关联字段**。踩过的坑：

    * 按 `monsters` 数组下标对齐 `monster_names` —— 只有 2/52 命中，全错；
    * 按 `monsters[i].id` 对齐 —— 只有 1/52 命中；
    * 按 `monsters[i].enemy_type` 直接索引名字表 —— 37/52，基础区间全对但大师区间偏移；
    * `enemy_type - 73` 索引 `monster_names_mr` —— 45/52，Master 区间还有 5 只差一位。

    所以最终用**两条独立证据合成**：

    1. **掉落物名字**（首选，游戏自带真值）：怪物的掉落素材名形如「爆鳞龙的爆腺」，
       取「」前的怪物名前缀；同一前缀出现 >= 2 次、且是名字表里已知的怪物名才採信。
    2. **编号公式**（兜底）：`enemy_type <= 75` 直接查基础表；
       `enemy_type >= 76` 用 `enemy_type - 73` 查大师表（该偏移恰好抵消两表的缺号），
       越界时再按字面 `EnemyIndex` 编号在两张表里找。

    实测 52 只基础种里 51 只可判定，唯一无签名的是 em=131（源数据里它没有掉落记录，
    按编号推出为「精灵鹿」，可能与游戏内名称有出入，属已知的源数据不一致）。
    """
    base_entries = unwrap_entries(mhrice.get("monster_names"))
    mr_entries = unwrap_entries(mhrice.get("monster_names_mr"))
    monsters_raw = mhrice.get("monsters") or []

    def names_of(entries) -> dict:
        result = {}
        for entry in entries:
            match = re.search(r"(\d+)", entry.get("name") or "")
            if match:
                result[int(match.group(1))] = strip_tags(localize(entry))
        return result

    base_by_num = names_of(base_entries)
    mr_by_num = names_of(mr_entries)
    # 按**数组位置**索引的名字（基础表 + 大师表）。
    # 小动物的 enemy_type 是这张合并表的位置索引，实测与掉落物一致：
    #   et=66 -> 位置 66 = 丸鸟（掉落物正是「丸鸟的羽」）
    #   et=51 -> 位置 51 = 野猪（掉落物「野猪的毛皮」）
    # 注意不能用 names_of 得到的「编号映射」——那是另一个体系，会对错。
    names_by_index = [strip_tags(localize(e)) for e in (base_entries + mr_entries)]
    # 索引 -> 名字（大师表按数组下标访问）
    mr_names = [strip_tags(localize(entry)) for entry in mr_entries]
    known_names = set(base_by_num.values()) | set(mr_by_num.values())

    # 掉落物里的怪物名前缀统计
    drop_tokens = build_drop_tokens(mhrice, items)

    def by_number(enemy_type) -> str:
        """编号公式：基础区间直接查表，大师区间走显式映射。"""
        if enemy_type is None:
            return ""
        if enemy_type <= 75 and enemy_type in base_by_num:
            return base_by_num[enemy_type]
        index = MR_INDEX_BY_ENEMY_TYPE.get(enemy_type)
        if index is not None and 0 <= index < len(mr_names):
            return mr_names[index]
        return mr_by_num.get(enemy_type) or base_by_num.get(enemy_type) or ""

    def signature(em) -> str:
        """从掉落物名字里取怪物名（需出现 >= 2 次且是已知名字）。"""
        counts = drop_tokens.get(em) or {}
        for token, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            if count >= 2 and token in known_names:
                return token
        return ""

    def weak_signature(ems) -> str:
        """小动物的兜底判定：掉落物名里出现过的任何已知怪物名。

        小动物掉自己的素材，名字通常直接出现在素材名里
        （「雪鹿的角」-> 雪鹿）。放宽到出现 1 次即可，因为小动物的掉落条目少。
        """
        counts = drop_tokens.get(-ems) or {}
        for token, _count in sorted(counts.items(), key=lambda kv: -kv[1]):
            if token in known_names:
                return token
        return ""

    def resolve_name(em, enemy_type) -> str:
        """合成最终怪物名。

        两条证据各有强弱，合成规则是：

        * 编号公式能区分**变体**（红莲爆鳞龙 / 怪异克服钢龙 / 霸主・火龙），
          但 Master 区间有 5 只偏一位；
        * 掉落物前缀是游戏自带真值，但**只认基础种**
          （红莲爆鳞龙的掉落也叫「爆鳞龙的爆腺」），分不出变体。

        所以：签名与公式**一致或互为前缀**时采用公式（保住变体名）；
        只有两者明显冲突（公式偏了一位）时才用签名纠错。
        """
        formula = by_number(enemy_type)
        sign = signature(em)
        if not sign:
            return formula
        if not formula:
            return sign
        if sign == formula or sign in formula or formula in sign:
            # 同族：公式更具体（变体名），用公式
            return formula if len(formula) >= len(sign) else sign
        # 冲突：公式大概率偏位，信签名
        return sign

    monsters: dict[str, dict] = {}
    for index, raw in enumerate(monsters_raw):
        em = (raw.get("em_type") or {}).get("Em")
        if not isinstance(em, int) or em <= 0:
            continue

        name = EM_NAME_OVERRIDES.get(em) or resolve_name(em, raw.get("enemy_type"))

        # ---- 别名：不采用 ----
        # 这个导出文件的别名表整体错位一位（别名表[0] 是「雌火竜」，对得上
        # 名字表[0] 雌火龙，但 `Alias_EnemyIndex001` 标的是「雌火竜 ヌシ・
        # リオレイア」，按编号取会取到下一只怪的别名）。
        # 逐条校验后 78 只里只剩极少数能自洽，且仍有「霸主・火龙」被当成
        # 「火龙」的错例。别名在卡片上只是装饰，错的名字比没有更糟，
        # 所以这里直接不输出。
        info = {"name": name, "kind": "large"}
        monsters[str(em)] = info

    # ---- 小动物（精灵鹿、野猪、飞甲虫…）----
    # 它们用独立的 Ems id 空间，掉落表里的键也是 Ems。
    #
    # 名字判定同样靠**掉落物前缀**（游戏一手数据）：小动物掉的素材多数带自己的名字
    # （「野猪的毛皮」「雪鹿的角」），实测 37 只里 30 只可判定。
    # 不能用 id / enemy_type 去索引名字表：实测两者都错位
    # （精灵鹿 id=3 → 名字表[3] 是「霸主・火龙」）。
    small_monsters: dict[str, dict] = {}
    for raw in mhrice.get("small_monsters") or []:
        ems = (raw.get("em_type") or {}).get("Ems")
        if not isinstance(ems, int) or ems <= 0:
            continue
        enemy_type = raw.get("enemy_type")
        # 小动物名判定顺序（按可靠性）：
        #   1. **掉落物前缀签名** —— 小动物掉自己的素材（「丸鸟的羽」-> 丸鸟），
        #      这是游戏一手数据，实测 37 只里 30 只可判定，且与用户看到的现象一致。
        #   2. 名字表[enemy_type]（按数组位置）—— 兜底，只在没有签名时用。
        #   3. SMALL_NAME_OVERRIDES —— 极少数既无签名、编号又对不上的。
        #
        # 为什么签名优先：这个导出文件的 `enemy_type` 编号**本身错乱**——
        # 逐只核对发现，有的要 [et-2]、有的要 [et]、有的要 [et+1]，
        # 没有任何单一公式能覆盖（同名表内部自相矛盾）。而掉落物名永远是对的。
        # 踩过的坑：早先按位置硬写覆盖表，整体错位一位，
        # 导致「药草」被标成毒狗龙的掉落（实际是丸鸟），24 只小动物全错。
        # 注意：掉落表的键对小动物是**负数**（见 iter_lot_rows），
        # signature() 收的是那个键，所以要传 -ems。早先误传正数 ems，
        # 于是签名静默失效（拿到的是大型怪的数据），整段逻辑形同虚设。
        name = signature(-ems)  # 1) 强签名：掉落物里出现 >=2 次的本名，最可靠
        if not name:
            # 2) 人工核对表：掉落物不带自己名字的少数几只。
            #    必须排在编号兜底**之前**——否则编号会先命中一个错误的怪名
            #    （实测 Ems=3 的编号位置是「爆鳞龙」，而它其实是精灵鹿）。
            name = SMALL_NAME_OVERRIDES.get(ems, "")
        if not name and isinstance(enemy_type, int):
            # 3) 编号兜底：名字表位置 = enemy_type - 2。
            # 该偏移是逐只核对出来的（对 13/16/19/25/26/27/29/34/39/40/41/42/43
            # 等已验证条目全部成立）；本文件的 enemy_type 编号体系不统一，
            # 没有通用公式，所以只在没有强签名与人工表时使用。
            index = enemy_type - 2
            if 0 <= index < len(names_by_index):
                name = names_by_index[index]
        if not name:
            # 4) 弱签名：掉落物里出现过一次的已知怪名。放最后——
            #    它容易抽到「巨大」「优质」这类通用词对应的怪，误判率高。
            name = weak_signature(ems)
        if not name:
            name = f"小怪{ems}"
        info = {"name": name, "kind": "small"}
        habitats = decode_habitat(raw, mhrice, small=True)
        if habitats:
            info["maps"] = habitats
        small_monsters[str(ems)] = info

    return {"monsters": monsters, "small_monsters": small_monsters}


def decode_habitat(raw: dict, mhrice: dict, small: bool = False) -> list[int]:
    """解出怪物出现的 map_no 列表。

    `habitat_area.flag` 是位掩码，实测 `bit(map_no - 1)`：

    * 雌火龙 flag=3（0b11）→ map 1 废神社 + map 2 沙原
    * 土砂龙 flag=13（0b1101）→ map 1 + 3 水没林 + 4 冰封群岛

    大型怪与小型怪的表不同：大型怪在 `monster_list`（用 `Em` 键），
    小动物用 `small_monsters` 里自己的 `habitat_area`。
    `ecological.stage_info_list` 覆盖不全（多数小动物为空），不能作为地图来源。
    """
    if small:
        # 小动物的 habitat_area 在源数据里恒为 null，只能靠 ecological.stage_info_list；
        # 而它多数为空（实测 37 只里只有精灵鹿一族的 2 条有值）。
        # 地图数据的完整来源见 README 的「小动物栖息地图」一节。
        ecol = raw.get("ecological") or {}
        return [
            s.get("map_type")
            for s in (ecol.get("stage_info_list") or [])
            if isinstance(s.get("map_type"), int)
        ]

    ems = (raw.get("em_type") or {}).get("Ems")
    em = (raw.get("em_type") or {}).get("Em")
    target = ("Ems", ems) if isinstance(ems, int) else ("Em", em)

    for entry in unwrap(mhrice.get("monster_list")):
        types = entry.get("em_type") or {}
        if types.get(target[0]) != target[1]:
            continue
        flags = (entry.get("habitat_area") or {}).get("flag") or []
        mask = flags[0] if flags and isinstance(flags[0], int) else 0
        return [number for number in range(1, 32) if mask & (1 << (number - 1))]
    return []


def build_drop_tokens(mhrice: dict, items: dict) -> dict:
    """统计每只怪物掉落素材名里的「XX的YY」前缀，用于反推怪物名。

    返回 {em: {前缀: 出现次数}}。掉落物名字是游戏自带的数据，
    形如「爆鳞龙的爆腺」，是判定怪物归属最可靠的一手证据。
    """
    tokens: dict[int, dict] = {}
    for em, _rank, row in iter_lot_rows(mhrice):
        bucket = tokens.setdefault(em, {})
        for kind, _label in SOURCE_KINDS:
            id_list = row.get(f"{kind}_item_id_list")
            if not isinstance(id_list, list):
                continue
            for index in range(len(id_list)):
                item_id = _item_id_at(id_list, index)
                if item_id is None:
                    continue
                item = items.get(item_id)
                if not item:
                    continue
                name = item["name"]
                if "的" in name:
                    prefix = name.split("的")[0]
                    bucket[prefix] = bucket.get(prefix, 0) + 1
    return tokens


def build_anomaly_rewards(mhrice: dict, items: dict, monster_names: dict) -> dict:
    """提取「傀异调查（怪异调查）」里怪异化怪物的掉落。

    这是**独立于 monster_lot 的一套报酬表**，`monster_lot` 里完全没有这些素材。
    踩过的坑：早先只读 `monster_lot` / `monster_lot_mr`，于是所有怪异化素材
    （怪异化的凶刚角 等，稀有度 9）都查不到任何来源，卡片显示「暂无掉落记录」。

    源表 `mystery_reward_item.param` 每条形如::

        {"em_type": {"Em": 7}, "lv_lower_limit": 161, "lv_upper_limit": 199,
         "hagibui_probability": 40, "reward_item": {"Normal": 2911},
         "item_num": 1, "is_special_mystery": false}

    即「某只怪异化怪物，在某个傀异等级区间内，以某概率掉落某素材」。
    返回 {item_id: [记录, ...]}，记录按怪物聚合并合并连续等级区间。
    """
    table = mhrice.get("mystery_reward_item") or {}
    params = table.get("param") if isinstance(table, dict) else None
    if not isinstance(params, list):
        return {}

    # 先按 (item_id, em) 收集等级区间
    grouped: dict[int, dict] = {}
    for entry in params:
        if not isinstance(entry, dict):
            continue
        reward = entry.get("reward_item")
        item_id = reward.get("Normal") if isinstance(reward, dict) else None
        if not isinstance(item_id, int):
            continue
        em = (entry.get("em_type") or {}).get("Em")
        if not isinstance(em, int):
            continue
        low = entry.get("lv_lower_limit")
        high = entry.get("lv_upper_limit")
        if not isinstance(low, int) or not isinstance(high, int):
            continue
        probability = entry.get("hagibui_probability")
        quantity = entry.get("item_num")

        per_item = grouped.setdefault(item_id, {})
        per_monster = per_item.setdefault(em, {"segments": []})
        per_monster["segments"].append(
            {
                "low": low,
                "high": high,
                "chance": float(probability)
                if isinstance(probability, (int, float))
                else 0.0,
                "quantity": quantity if isinstance(quantity, int) else 0,
                "is_special": bool(entry.get("is_special_mystery")),
            }
        )

    def merge_segments(segments: list[dict]) -> list[dict]:
        """合并「等级区间相邻且概率/数量相同」的段。

        例如 201-219 / 220-220 / 221-260 三段概率都是 40%，合并成 201-260；
        300-300 的特殊条目与 161-300 概率相同，也一并并掉。
        但概率不同的段**绝不合并**——雷狼龙 161-199 是 35%、201+ 是 40%，
        合并后取最大值会把 35% 那段误报成 40%。

        `is_special_mystery` 不参与比较：它只标记「特殊傀异」这类玩法差异，
        不影响掉落概率，若参与比较会产生「Lv161-300 40%，Lv300 40%」这种冗余行。
        """
        ordered = sorted(
            segments, key=lambda s: (s["low"], s["high"], s["chance"], s["quantity"])
        )
        merged: list[dict] = []
        for seg in ordered:
            if (
                merged
                and seg["low"] <= merged[-1]["high"] + 1
                and seg["chance"] == merged[-1]["chance"]
                and seg["quantity"] == merged[-1]["quantity"]
            ):
                merged[-1]["high"] = max(merged[-1]["high"], seg["high"])
            else:
                merged.append(dict(seg))
        return merged

    result: dict[int, list[dict]] = {}
    for item_id, per_item in grouped.items():
        records = []
        for em, info in per_item.items():
            segments = merge_segments(info["segments"])
            records.append(
                {
                    "em": em,
                    "monster": monster_names.get(str(em), f"Em{em}"),
                    "segments": [
                        {
                            "low": s["low"],
                            "high": s["high"],
                            "chance": s["chance"],
                            "quantity": s["quantity"],
                            "is_special": s["is_special"],
                        }
                        for s in segments
                    ],
                    "level_min": min(s["low"] for s in segments),
                    "level_max": max(s["high"] for s in segments),
                    "chance": max(s["chance"] for s in segments),
                }
            )
        # 等级门槛低的排前面（更容易达成的先看到），再按概率降序
        records.sort(key=lambda r: (r["level_min"], -r["chance"], r["monster"]))
        result[item_id] = records
    return result


def build_item_table(items_json: dict) -> dict:
    """items.json: id -> {name, rarity, type}。"""
    table = {}
    for key, value in items_json.items():
        if not isinstance(value, dict):
            continue
        name = (value.get("name") or "").strip()
        if not name:
            continue
        table[int(key)] = {
            "name": name,
            "rarity": int(value.get("rarity") or 0),
            "type": value.get("type") or "",
        }
    return table


def iter_lot_rows(mhrice: dict):
    """遍历所有掉落行，产出 (em, rank, row)。

    掉落表里大型怪与小动物**用不同的键**：

    * 大型怪：``{"em_types": {"Em": 98}}``
    * 小动物：``{"em_types": {"Ems": 3}}``

    两者是**独立的 id 空间**（Ems=3 是精灵鹿，Em=3 是奇怪龙），不能混用。
    早先只认 ``Em``，把小动物（精灵鹿、野猪、飞甲虫…）的掉落整批丢掉了，
    于是「温暖的毛皮」这类素材被显示成「没有来源」。
    """
    for key in ("monster_lot", "monster_lot_mr"):
        for row in unwrap(mhrice.get(key)):
            types = row.get("em_types") or {}
            em = types.get("Em")
            if isinstance(em, int) and em > 0:
                yield em, row.get("quest_rank") or "", row
                continue
            ems = types.get("Ems")
            if isinstance(ems, int) and ems > 0:
                # 小动物用负数偏移，避免与大型怪的 em 空间冲突
                yield -ems, row.get("quest_rank") or "", row


def _item_id_at(sequence, index):
    """item_id_list 的元素形如 {'Normal': 519} / {'None': ...}，取出数字 id。"""
    if not isinstance(sequence, list) or index >= len(sequence):
        return None
    item = sequence[index]
    if isinstance(item, dict):
        for value in item.values():
            if isinstance(value, int) and value > 0:
                return value
        return None
    if isinstance(item, int) and item > 0:
        return item
    return None


def build_official_part_names(mhrice: dict) -> dict:
    """em -> {RandomId: 官方中文部位名}，来自 `parts_type` + `hunter_note_msg`。

    **只在 `part_map` 查不到时兜底**。`parts_type` 的 text 是 GUID，要在
    消息表的 `entries[].guid` 里查出文本（查到的是「头部 / 尾巴 / 翼足 / 背鳍…」
    这类官方用词）。按 monster 的 `enemy_type` 过滤后确定该怪用哪个词。

    为什么不与 `part_map` 竞争：实测有冲突个例——爵银龙 `RandomId=5`
    在 `part_map` 里是「翼」，`parts_type` 却给「手臂」。`part_map` 是逐怪数据
    且与掉落内容吻合（刚翼 -> 翼），所以以它为准，`parts_type` 只补空缺。
    """
    # 1) GUID -> 文本（扫描所有消息表的 entries）
    guid_text: dict[str, str] = {}
    for table in mhrice.values():
        if not isinstance(table, dict):
            continue
        entries = table.get("entries")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            guid = entry.get("guid")
            if not isinstance(guid, str):
                continue
            content = entry.get("content") or []
            text = (
                content[13]
                if len(content) > 13 and content[13]
                else (content[0] if content else "")
            )
            if text:
                guid_text.setdefault(guid.lower(), text)
    if not guid_text:
        return {}

    # 2) (enemy_type, RandomId) -> 名称
    by_enemy: dict[tuple[int, int], str] = {}
    table = mhrice.get("parts_type") or {}
    params = table.get("params") if isinstance(table, dict) else None
    for entry in params or []:
        if not isinstance(entry, dict):
            continue
        random_id = (entry.get("broken_parts_types") or {}).get("RandomId")
        if not isinstance(random_id, int):
            continue
        for info in entry.get("text_infos") or []:
            name = ""
            for field in ("text", "text_for_monster_list"):
                guid = info.get(field)
                if isinstance(guid, str) and guid.lower() in guid_text:
                    name = guid_text[guid.lower()]
                    break
            if not name:
                continue
            for enemy in info.get("enemy_type_list") or []:
                em = (enemy or {}).get("Em")
                if isinstance(em, int):
                    by_enemy.setdefault((em, random_id), name)

    # 3) 按敌人类型归属到怪物：em -> enemy_type
    enemy_type_of = {}
    for raw in mhrice.get("monsters") or []:
        em = (raw.get("em_type") or {}).get("Em")
        enemy_type = raw.get("enemy_type")
        if isinstance(em, int) and isinstance(enemy_type, int):
            enemy_type_of[em] = enemy_type

    result: dict = {}
    for em, enemy_type in enemy_type_of.items():
        names = {}
        for (enemy, random_id), name in by_enemy.items():
            if enemy == enemy_type:
                names[str(random_id)] = name
        if names:
            result[em] = names
    return result


def build_part_names(mhrice: dict) -> dict:
    """em / Ems -> {RandomId: 部位中文名}。

    部位名取自 `monsters[].collider_mapping.part_map`（游戏内日文名），
    按 `PART_NAME_ZH` 翻成中文。

    键来自 `part_map`；`parts_break_list` 里的 `RandomId` 就是这些键
    （个别怪会给出 65535 这类特殊值，查不到就留空）。

    ## 关于清洗而不是丢弃

    源数据里同一个部位有多种写法：

        頭部 / 頭部_ダメージアタリ / ダメージ部位00　頭 / Group0_頭 / damage_胴

    早先把带 `ダメージ`/`Group`/`damage` 字样的**整条丢掉**，于是伞鸟这类
    只有内部写法的怪完全没有部位名，部位细分率被压到 61%。
    现在改成**先清洗出其中的汉字部位词**（`ダメージ部位00　頭` -> `頭`），
    只有确实提炼不出部位词时才丢弃。
    """
    result: dict = {}
    for raw in (mhrice.get("monsters") or []) + (mhrice.get("small_monsters") or []):
        types = raw.get("em_type") or {}
        key = types.get("Em") or types.get("Ems")
        if not isinstance(key, int):
            continue
        part_map = (raw.get("collider_mapping") or {}).get("part_map") or {}
        if not isinstance(part_map, dict):
            continue
        # 同一个 em 可能有多条怪兽记录（基础种/变体），**合并而不是覆盖**。
        # 踩过的坑：早先直接 result[key] = names，后一条记录会覆盖前一条，
        # 于是角龙、妃蜘蛛这些本来有完整 part_map 的怪反而没有部位名。
        names = result.setdefault(key, {})
        for random_id, values in part_map.items():
            if not isinstance(values, list) or not values:
                continue
            cleaned = clean_part_label(str(values[0]).strip())
            translated = translate_part(cleaned) if cleaned else ""
            # 已有更好（非空）的名字时不覆盖
            if translated and not names.get(str(random_id)):
                names[str(random_id)] = translated

    # 用官方名表补 part_map 查不到的空缺（只补空，不覆盖）
    for em, official in build_official_part_names(mhrice).items():
        names = result.setdefault(em, {})
        for random_id, name in official.items():
            if name and not names.get(random_id):
                names[random_id] = name

    return {key: names for key, names in result.items() if names}


# 内部标记：出现在部位名里但本身不是部位词的前后缀
_INTERNAL_PART_MARKERS = (
    "ダメージアタリ",
    "ダメージ部位",
    "damage",
    "Damage",
    "Group",
    "Gropu",  # 源数据里的拼写错误（人鱼龙等），一并当内部前缀处理
    "シェル弾き",
    "EmHitDamage",
    "アタリ",
    "部位分け",
)

# 内部前缀/后缀，清洗时剥掉。`Gropu` 是源数据的拼写错误，必须一起处理，
# 否则 `Gropu5_左脚` 会原样漏到卡片上。
_INTERNAL_AFFIX = re.compile(
    r"^(?:ダメージ部位|ダメージアタリ|Group|group|Gropu|gropu|damage|Damage)\d*[_　\s：:]*"
    r"|[_　\s：:]*(?:ダメージ部位|ダメージアタリ|Group|group|Gropu|gropu|damage|Damage|部位分け)\d*$"
)


def clean_part_label(name: str) -> str:
    """从内部写法里提炼出部位词；提炼不出就返回空字符串。

    `ダメージ部位00　頭` -> `頭`；`Group0_頭` -> `頭`；`頭部_ダメージアタリ` -> `頭部`；
    `damage_頭部` -> `頭部`；`Gropu5_左脚` -> `左脚`；
    纯 `EmHitDamageRSData` / 纯 `Group0` / 纯数字 -> ``
    """
    text = (name or "").strip()
    if not text:
        return ""
    text = _INTERNAL_AFFIX.sub("", text).strip("_　 ：:")
    if not text or text.isdigit():
        return ""
    # 仍残留内部词：只取其中的汉字片段（部位词都是汉字）
    if any(marker in text for marker in _INTERNAL_PART_MARKERS):
        match = re.search(r"[\u4e00-\u9fff]+", text)
        text = match.group(0) if match else ""
    return text


def translate_part(name: str) -> str:
    """日文部位词 -> 中文；**翻不出来就返回空**（绝不把日文显示给用户）。

    先精确匹配，再按「最长已知词」做包含匹配，
    这样 `デブ時頭`（河童蛙的膨胀状态头部）能落到 `头部`。
    """
    if not name:
        return ""
    if name in PART_NAME_ZH:
        return PART_NAME_ZH[name]
    best = ""
    for japanese, chinese in PART_NAME_ZH.items():
        if japanese and japanese in name and len(japanese) > len(best):
            best = japanese
            best_zh = chinese
    return best_zh if best else ""


def is_internal_part_label(name: str) -> bool:
    """判断是否是内部标记而非部位名。"""
    if not name:
        return True
    if any(marker in name for marker in _INTERNAL_PART_MARKERS):
        return True
    # 纯数字/编号（例如 part_map 值为「10」「1」「5」）不是部位名
    return bool(name.isdigit())


# 日文汉字 -> 简体，用于别名与怪物名的比对
_JA_TO_ZH = str.maketrans({"竜": "龙", "龍": "龙", "獣": "兽", "鎧": "铠", "鎌": "镰"})


def alias_matches_name(alias: str, name: str) -> bool:
    """别名的汉字部分是否与怪物名自洽。

    别名形如「雌火竜 リオレイア」「角竜 ディアブロス」。取开头的汉字段，
    把日文汉字转成简体后与怪物名比较。返回 False 说明这个别名多半属于别的怪，
    调用方应丢弃它——宁可少显示，也不要给用户错的名字。

    例：别名「雌火竜」-> 雌火龙，与名字「雌火龙」一致 -> 保留；
        别名「角竜」-> 角龙，而名字是「奇怪龙」 -> 丢弃。
    """
    if not alias or not name:
        return False
    head = re.match(r"^([\u4e00-\u9fff]+)", alias.strip())
    if not head:
        # 别名没有汉字开头（少见），无法校验，保守丢弃
        return False
    normalized = head.group(1).translate(_JA_TO_ZH)
    cleaned = name.replace("・", "").replace(" ", "")
    return normalized in cleaned or cleaned in normalized


def source_type_labels(kind: str, row: dict, part_lookup: dict) -> list[str]:
    """取「这一行的每个类型叫什么」，用于把奖品细分到具体部位/来源。

    返回的类型数与 `{kind}_item_id_list` 的类型数一致；
    下标 i 的奖品属于类型 `i // SLOTS_PER_TYPE`。
    空字符串表示该类型没有可读名字（例如未收录的部位名）。
    """
    if kind == "parts_break_reward":
        labels = []
        for entry in row.get("parts_break_list") or []:
            random_id = entry.get("RandomId") if isinstance(entry, dict) else None
            labels.append(
                part_lookup.get(str(random_id), "") if random_id is not None else ""
            )
        return labels
    if kind == "hagitory_reward":
        return [
            CARVE_TYPE_LABELS.get(value, "")
            for value in (row.get("enemy_reward_type_list") or [])
        ]
    if kind == "drop_reward":
        return [
            DROP_TYPE_LABELS.get(value, "")
            for value in (row.get("drop_reward_type_list") or [])
        ]
    return []


def compose_kind_label(kind: str, base_label: str, detail: str) -> str:
    """把「方式 + 细分」拼成给用户看的标签。

    * 部位破坏 + 头部 -> 头部破坏
    * 剥取     + 尾巴 -> 尾巴剥取
    * 掉落物 / 目标报酬等没有细分，保持原样
    """
    if not detail:
        return base_label
    if kind == "parts_break_reward":
        return f"{detail}破坏"
    if kind == "hagitory_reward":
        return f"{detail}剥取"
    return base_label


def build_drops(
    mhrice: dict, items: dict, part_names: dict | None = None
) -> tuple[dict, dict]:
    """返回 (素材 -> 来源列表, 怪物em -> 掉落列表)。

    两个方向都建，是因为插件既要支持「素材->哪只怪掉」，
    也要支持「这只怪掉什么」。
    """
    sources: dict[str, list] = {}
    monster_drops: dict[str, list] = {}
    part_names = part_names or {}

    for em, rank, row in iter_lot_rows(mhrice):
        part_lookup = part_names.get(abs(em)) or {}

        for kind, label in SOURCE_KINDS:
            id_list = row.get(f"{kind}_item_id_list")
            if not isinstance(id_list, list):
                continue
            num_list = row.get(f"{kind}_num_list") or []
            prob_list = row.get(f"{kind}_probability_list") or []
            type_labels = source_type_labels(kind, row, part_lookup)

            for index in range(len(id_list)):
                item_id = _item_id_at(id_list, index)
                if item_id is None:
                    continue
                # 概率为 0 的记录没有任何信息量
                probability = prob_list[index] if index < len(prob_list) else 0
                if not isinstance(probability, (int, float)) or probability <= 0:
                    continue
                quantity = num_list[index] if index < len(num_list) else 0

                # 类型下标：每个类型固定 SLOTS_PER_TYPE 槽
                detail = ""
                if type_labels:
                    group_index = index // SLOTS_PER_TYPE
                    if group_index < len(type_labels):
                        detail = type_labels[group_index]

                record = {
                    "item_id": item_id,
                    "em": em,
                    "monster": "",
                    "rank": rank,
                    "rank_label": RANK_LABELS.get(rank, rank),
                    "kind": kind,
                    "kind_label": compose_kind_label(kind, label, detail),
                    "source_detail": detail,
                    "quantity": int(quantity or 0),
                    "chance": float(probability),
                }
                sources.setdefault(str(item_id), []).append(dict(record))
                monster_drops.setdefault(str(em), []).append(dict(record))

    return sources, monster_drops


# 采集点类别（pop_category）-> 中文节点名。
#
# 每一个都是按该类别下实际出现的物品**核对过**的：
#   5 -> 灵鹤石/铁矿石/大地结晶（矿脉）
#   6 -> 厚重龙骨/扭曲的重怪骨/龙骨【小】（骨冢）
#   1 -> 药草/解毒草/火药草    2 -> 蓝蘑菇/硝化伞菇    3 -> 苦虫/光虫
#   4 -> 怪力种子/打消果实     11 -> 特产菇/竹笋/酸浆  13 -> 大木桶/炸药
# 源数据没有给出这些类别的官方名字（map_icon_list 的 GUID 解析出来是
# **物品名与说明**，不是节点名），所以这里用核对后的描述性名称。
NODE_NAMES = {
    1: "药草",
    2: "蘑菇",
    3: "捕虫点",
    4: "果实",
    5: "矿脉",
    6: "骨冢",
    7: "蜂巢",
    8: "蜘蛛网",
    10: "粪堆",
    11: "特产",
    12: "团子素材",
    13: "木桶",
    14: "古老手记",
    16: "巢穴",
}


def node_name(category) -> str:
    if not isinstance(category, int):
        return "采集点"
    return NODE_NAMES.get(category, f"采集点{category}")


def build_gathering_nodes(mhrice: dict, map_names: dict) -> dict:
    """野外采集点：素材 -> [{map, node, rank, quantity, chance}]。

    ## 数据来源（全部是游戏本体数据，无需联网抓网站）

    * `item_pop_lot.param` —— 每条 = **(pop_id, field_type)** 一组采集掉落：
        - `pop_id`    采集点类型 id（同一类型在多张图出现，故有多条）
        - `field_type` **就是地图号**；`-1` 表示该类型在这些图上共用一套掉落
        - `lower_*` / `upper_*` / `master_*` 分别是下位/上位/大师的物品、数量、概率
    * `maps[地图号].pops[].kind.Item.behavior` —— 给出 `pop_id` 出现在哪些地图，
      以及 `pop_category`（节点类型）。

    ## 为什么这么重要

    早先采集点数据来自 Kiranico 抓取，**只有地图名、没有节点类型**，
    于是「散发土香的重泥骨」只能回答「水没林」，答不出「水没林的骨冢」。
    游戏数据本身就有节点信息与**逐地图**的掉落，实测与 Kiranico 数值完全一致
    （重泥骨：水没林·大师 20% x1 / 10% x2），而且覆盖更全（180 项 vs 47 项）。
    """
    table = mhrice.get("item_pop_lot") or {}
    params = table.get("param") if isinstance(table, dict) else None
    if not isinstance(params, list):
        return {}

    # 1) pop_id -> 地图集合 / 节点类别
    pop_maps: dict[int, set[int]] = {}
    pop_categories: dict[int, set[int]] = {}
    maps_table = mhrice.get("maps")
    if isinstance(maps_table, dict):
        for map_no, map_data in maps_table.items():
            if not isinstance(map_data, dict):
                continue
            try:
                map_number = int(map_no)
            except (TypeError, ValueError):
                continue
            for entry in map_data.get("pops") or []:
                kind = (entry.get("kind") or {}).get("Item")
                if not isinstance(kind, dict):
                    continue
                behavior = kind.get("behavior") or {}
                pop_id = behavior.get("pop_id")
                if not isinstance(pop_id, int):
                    continue
                pop_maps.setdefault(pop_id, set()).add(map_number)
                category = behavior.get("pop_category")
                if isinstance(category, int):
                    pop_categories.setdefault(pop_id, set()).add(category)

    # 2) 先按 (pop_id, field_type) 建索引。
    #
    # 同一个 pop_id 通常有两类条目：`field_type = -1`（**基础/通用**掉落）
    # 与 `field_type = 地图号`（**该地图专用**掉落）。专用条目是对基础的覆盖，
    # 不能两者都展开——否则同一张图的掉落会被算两遍，卡片上出现
    # 「40%→x2  40%→x2」这种重复。
    by_pop: dict[int, dict[int, dict]] = {}
    for entry in params:
        if not isinstance(entry, dict):
            continue
        pop_id = entry.get("pop_id")
        field_type = entry.get("field_type")
        if not isinstance(pop_id, int) or not isinstance(field_type, int):
            continue
        by_pop.setdefault(pop_id, {})[field_type] = entry

    # 3) 逐条采集掉落展开
    result: dict[int, list[dict]] = {}
    seen: set[tuple] = set()
    for pop_id, entries in by_pop.items():
        all_maps = sorted(pop_maps.get(pop_id, set()))
        categories = pop_categories.get(pop_id) or set()
        node = node_name(next(iter(sorted(categories)), None))

        for map_number in all_maps:
            # 有该地图的专用条目就用它，否则退回 -1 的通用条目
            entry = entries.get(map_number) or entries.get(-1)
            if entry is None:
                continue
            for rank, rank_label in (
                ("lower", "下位"),
                ("upper", "上位"),
                ("master", "大师"),
            ):
                ids = entry.get(f"{rank}_id") or []
                nums = entry.get(f"{rank}_num") or []
                probs = entry.get(f"{rank}_probability") or []
                for index, raw in enumerate(ids):
                    if not isinstance(raw, dict):
                        continue
                    item_id = raw.get("Normal")
                    if not isinstance(item_id, int):
                        continue
                    probability = probs[index] if index < len(probs) else 0
                    if not isinstance(probability, (int, float)) or probability <= 0:
                        continue
                    quantity = nums[index] if index < len(nums) else 0
                    # 同一采集点的多个槽位可能出现完全相同的产出，去重
                    dedupe_key = (
                        item_id,
                        pop_id,
                        map_number,
                        rank_label,
                        int(quantity or 0),
                        float(probability),
                    )
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    result.setdefault(item_id, []).append(
                        {
                            "map": map_names.get(map_number, f"地图{map_number}"),
                            "map_no": map_number,
                            # 同图同节点可能有多个采集点（矿脉①②），用 pop_id 区分
                            "pop_id": pop_id,
                            "node": node,
                            "rank": rank_label,
                            "quantity": int(quantity or 0),
                            "chance": float(probability),
                        }
                    )

    # 4) 排序：地图 -> 节点 -> 难度 -> 概率降序
    rank_order = {"下位": 0, "上位": 1, "大师": 2}
    for records in result.values():
        records.sort(
            key=lambda r: (
                r["map_no"],
                r["node"],
                rank_order.get(r["rank"], 9),
                -r["chance"],
            )
        )
    return result


def build_map_names(mhrice: dict) -> tuple[dict, dict]:
    """从源数据读地图名；源数据缺的编号用推断值补齐。

    返回 (map_no -> 名字, map_no -> 'source' 来源标注)。
    """
    names: dict[int, str] = {}
    origin: dict[int, str] = {}

    for table, offset in (("map_name", 0), ("map_name_mr", 0)):
        for entry in unwrap_entries(mhrice.get(table)):
            key = entry.get("name") or ""
            match = re.fullmatch(r"Stage_Name_(\d+)(?:_\w+)?", key)
            if not match:
                continue
            number = int(match.group(1))
            text = strip_tags(localize(entry))
            if is_rejected(text) or not text:
                continue
            # Stage_Name_01.._11 对应 map_no 1..11；_3x/_4x 是大师图，单独映射
            if number <= 11 or number in MAP_NAMES_MR:
                names[number] = text
                origin[number] = "source"

    # 源数据里没有名字的编号：用已验证/推断的表补齐
    for number, text in MAP_NAMES_VERIFIED.items():
        names.setdefault(number, text)
        origin.setdefault(number, "source")
    for number, text in MAP_NAMES_INFERRED.items():
        if number not in names:
            names[number] = text
            origin[number] = "inferred"

    return names, origin


def build_quest_names(mhrice: dict) -> dict:
    """任务名消息表索引：quest_no -> 简中任务名。

    实测命名规律是 QN{quest_no:0>6}_01，_02/_03 是描述文本，只取 _01。
    """
    names: dict[int, str] = {}
    tables = (
        "quest_hall_msg",
        "quest_hall_msg_mr",
        "quest_hall_msg_mr2",
        "quest_village_msg",
        "quest_village_msg_mr",
        "quest_arena_msg",
        "quest_dlc_msg",
        "quest_tutorial_msg",
    )
    for table in tables:
        for entry in unwrap_entries(mhrice.get(table)):
            key = entry.get("name") or ""
            match = re.fullmatch(r"QN(\d+)_01(?:_\w+)?", key)
            if not match:
                continue
            quest_no = int(match.group(1))
            text = strip_tags(localize(entry))
            if text and quest_no not in names:
                names[quest_no] = text
    return names


def build_quest_rewards(mhrice: dict, items: dict) -> dict:
    """quest_no -> 奖励条目列表。

    quest_data_for_reward[_mr] 只给奖励表编号，真正的物品在
    reward_id_lot_table[_mr] 里。
    """
    lot_tables: dict[int, dict] = {}
    for table in ("reward_id_lot_table", "reward_id_lot_table_mr"):
        for row in unwrap(mhrice.get(table)):
            table_id = row.get("id")
            if isinstance(table_id, int):
                lot_tables[table_id] = row

    rewards: dict[int, list] = {}
    for table in ("quest_data_for_reward", "quest_data_for_reward_mr"):
        for row in unwrap(mhrice.get(table)):
            quest_no = row.get("quest_numer")
            if not isinstance(quest_no, int):
                continue
            collected = []
            # 主奖励表 + 若干附加奖励表
            indexes = [row.get("common_material_reward_table_index")]
            add = row.get("additional_quest_reward_table_index")
            if isinstance(add, list):
                indexes.extend(add)
            for table_id in indexes:
                lot = lot_tables.get(table_id)
                if not lot:
                    continue
                id_list = lot.get("item_id_list") or []
                num_list = lot.get("num_list") or []
                prob_list = lot.get("probability_list") or []
                for index in range(len(id_list)):
                    item_id = _item_id_at(id_list, index)
                    if item_id is None:
                        continue
                    chance = prob_list[index] if index < len(prob_list) else 0
                    if not isinstance(chance, (int, float)) or chance <= 0:
                        continue
                    item = items.get(item_id)
                    if not item:
                        continue
                    collected.append(
                        {
                            "item_id": item_id,
                            "name": item["name"],
                            "rarity": item["rarity"],
                            "quantity": int(num_list[index] or 0)
                            if index < len(num_list)
                            else 0,
                            "chance": float(chance),
                        }
                    )
            if collected:
                rewards[quest_no] = collected
    return rewards


def build_quests(mhrice: dict, monsters: dict, maps: dict, rewards: dict) -> list:
    """任务主表：本体 + 大师 + 活动，统一成一种结构。"""
    quest_names = build_quest_names(mhrice)
    quests = []

    sources = (
        ("normal_quest_data", False),
        ("normal_quest_data_mr", False),
        ("dl_quest_data", True),
        ("dl_quest_data_mr", True),
    )
    for table, is_event in sources:
        for row in unwrap(mhrice.get(table)):
            quest_no = row.get("quest_no")
            if not isinstance(quest_no, int):
                continue
            quest_types = [
                t for t in (row.get("quest_type") or []) if t and t != "None"
            ]
            type_label = "/".join(QUEST_TYPE_LABELS.get(t, t) for t in quest_types)

            map_no = row.get("map_no")

            def collect_names(sequence) -> list:
                names = []
                for entry in sequence or []:
                    em = (entry or {}).get("Em")
                    if isinstance(em, int) and em > 0:
                        monster = monsters.get(str(em))
                        if monster and monster["name"] not in names:
                            names.append(monster["name"])
                return names

            # tgt_em_type 是任务目标栏（通常 2 个槽），boss_em_type 是本任务实际
            # 会出场的怪物（最多 7 个槽）。素材「去哪个任务刷」必须用后者，
            # 否则会把「打岩龙」这种错误信息给到用户。
            targets = collect_names(row.get("tgt_em_type"))
            bosses = collect_names(row.get("boss_em_type"))

            quests.append(
                {
                    "quest_no": quest_no,
                    "name": quest_names.get(
                        quest_no, row.get("dbg_name") or f"任务{quest_no}"
                    ),
                    "has_zh_name": quest_no in quest_names,
                    "level": row.get("quest_level") or "",
                    "level_num": _quest_level_num(row.get("quest_level")),
                    "enemy_level": row.get("enemy_level") or "",
                    "enemy_level_label": RANK_LABELS.get(
                        row.get("enemy_level"), row.get("enemy_level") or ""
                    ),
                    "map_no": map_no,
                    "map": maps.get(map_no, "") if isinstance(map_no, int) else "",
                    "type": type_label,
                    "is_event": is_event,
                    "targets": targets,
                    "bosses": bosses,
                    "rewards": rewards.get(quest_no, []),
                }
            )
    quests.sort(key=lambda q: q["quest_no"])
    return quests


def _quest_level_num(level) -> int:
    """QL1 -> 1，QL7Ex -> 7（Ex 是高难度标记）；拿不到时回 0。"""
    if not isinstance(level, str):
        return 0
    match = re.match(r"QL(\d+)", level.strip())
    return int(match.group(1)) if match else 0


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def load_existing_map_names(out_path: Path) -> dict:
    """读已存在快照里的 map_names，用于「新值为空时沿用旧值」。

    栖息地图来自外部抓取（`fetch_habitats.py`），而 `extract.py` 会**重写整个
    快照**。于是只要重建时没跑抓取那一步（例如用了 `--skip-fetch`），
    地图就会**静默消失**——卡片上只是安静地少一行「出现地图」，没有任何报错。

    这里把已有的地图名读进来，新算出来为空时沿用，
    让重建顺序不再影响正确性。
    """
    try:
        data = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    tables = data.get("tables") or {}
    carried: dict[str, list[str]] = {}
    for table_name in ("monsters", "small_monsters"):
        for key, entry in (tables.get(table_name) or {}).items():
            names = entry.get("map_names")
            if names:
                carried[f"{table_name}:{key}"] = names
    return carried


def build_snapshot(
    mhrice_path: Path, items_path: Path, previous_map_names: dict | None = None
) -> dict:
    print(f"读取 {mhrice_path} ...")
    mhrice = load_json(mhrice_path)
    print(f"读取 {items_path} ...")
    items_json = load_json(items_path)

    items_table = build_item_table(items_json)
    print(f"  素材/物品: {len(items_table)}")

    monster_tables = build_monsters(mhrice, items_table)
    monsters = monster_tables["monsters"]
    small_monsters = monster_tables["small_monsters"]
    print(f"  怪物: {len(monsters)} 只大型 + {len(small_monsters)} 只小动物")

    maps, map_origin = build_map_names(mhrice)
    print(
        f"  地图: {len(maps)}（其中 {sum(1 for v in map_origin.values() if v == 'inferred')} 个为推断）"
    )

    part_names = build_part_names(mhrice)
    print(f"  部位名表: {len(part_names)} 只怪物")

    rewards = build_quest_rewards(mhrice, items_table)
    print(f"  含奖励的任务: {len(rewards)}")

    sources, monster_drops = build_drops(mhrice, items_table, part_names)
    print(f"  有掉落的素材: {len(sources)}（含小动物掉落）")

    quests = build_quests(mhrice, monsters, maps, rewards)
    print(f"  任务: {len(quests)}")

    # 把来源记录补上怪物名，并按概率降序排列。
    # 小动物的 em 是负数（见 iter_lot_rows），查 small_monsters 表。
    def monster_name(em) -> str:
        """em 为负表示小动物（见 iter_lot_rows），查 small_monsters 表。"""
        value = int(em)
        if value < 0:
            info = small_monsters.get(str(-value))
        else:
            info = monsters.get(str(value))
        return info["name"] if info else f"Em{value}"

    for item_id, entries in sources.items():
        for entry in entries:
            entry["monster"] = monster_name(entry["em"])
        entries.sort(key=lambda e: (-e["chance"], e["monster"], e["kind"]))
    for em, entries in monster_drops.items():
        name = monster_name(em)
        for entry in entries:
            entry["monster"] = name
        entries.sort(key=lambda e: (-e["chance"], e["kind"]))

    # 傀异调查（怪异调查）报酬：来自 mystery_reward_item，独立于 monster_lot。
    # 不并进 sources/monster_drops —— 那些是「难度 + 方式 + 概率」的结构，
    # 傀异调查是「怪物 + 等级区间 + 概率」，硬塞进去会破坏两边的语义。
    anomaly_rewards = build_anomaly_rewards(
        mhrice, items_table, {em: info["name"] for em, info in monsters.items()}
    )
    print(
        f"  傀异调查报酬: {len(anomaly_rewards)} 个素材、"
        f"{sum(len(v) for v in anomaly_rewards.values())} 条怪物记录"
    )

    # 野外采集点（含节点类型，例如「水没林 · 骨冢」）。来自游戏本体数据，
    # 取代早期的 Kiranico 抓取：既有节点名，也是逐地图的。
    gathering_nodes = build_gathering_nodes(mhrice, maps)
    print(
        f"  野外采集点: {len(gathering_nodes)} 个素材、"
        f"{sum(len(v) for v in gathering_nodes.values())} 条记录"
    )

    # 纳入**全部素材类物品**，而不是只保留「查得到来源」的那些。
    #
    # 踩过的坑：这里先后用「有怪物掉落」和「有怪物掉落或任务报酬」当过滤条件，
    # 结果把只在野外采集的素材（温暖的毛皮、野猪的毛皮…）整条丢掉了，
    # 用户查询时得到「没有找到」，而不是「这个素材没有掉落记录」。
    # 查不到来源是数据事实，应该照实展示，不该表现为「这个素材不存在」。
    MATERIAL_TYPES = {"Material", "OffcutsMaterial"}
    # 掉落记录里出现过的物品 id —— 即使不是「素材」类型也必须进表。
    # 踩过的坑：小动物会掉结算道具（丸鸟蛋 type=CarryPayOff）和消耗品
    # （生肉、药草、怪物体液），这些被类型过滤挡掉后，掉落记录指向一个
    # 不存在的物品，卡片上就出现**空白的素材名**（「掉落物 1个 100%」后面没字）。
    referenced = {
        str(entry["item_id"]) for entries in sources.values() for entry in entries
    }
    # 傀异调查报酬引用的素材同样要收进来
    referenced |= {str(item_id) for item_id in anomaly_rewards}

    items_out = {}
    no_source = 0
    for item_id, item in items_table.items():
        key = str(item_id)
        if item["type"] not in MATERIAL_TYPES and key not in referenced:
            continue
        entries = sources.get(key, [])
        nodes = gathering_nodes.get(item_id, [])
        # 有傀异调查报酬 / 野外采集点但没进 sources 的，也不算「没有来源」
        if (
            not entries
            and not anomaly_rewards.get(item_id)
            and not nodes
            and item["type"] in MATERIAL_TYPES
        ):
            no_source += 1
        # 没有来源的（野外采集等）保持空列表，卡片会说明「未记录掉落来源」
        items_out[key] = {
            "name": item["name"],
            "rarity": item["rarity"],
            "type": item["type"],
            "sources": entries,
            "anomaly_rewards": anomaly_rewards.get(item_id, []),
            "gathering_nodes": nodes,
        }
    print(
        f"  快照内素材: {len(items_out)}"
        f"（其中 {no_source} 个没有掉落来源，属采集/特殊获取；"
        f"另含 {len(referenced)} 个掉落记录引用的非素材类物品）"
    )

    # 怪物只保留有掉落的，其余是环境生物
    carried = previous_map_names or {}
    monsters_out = {}
    for em, info in monsters.items():
        drops = monster_drops.get(em)
        if not drops:
            continue
        entry = {**info, "drops": drops}
        names = entry.get("map_names") or carried.get(f"monsters:{em}") or []
        if names:
            entry["map_names"] = names
        monsters_out[em] = entry
    print(f"  快照内怪物: {len(monsters_out)} 只大型")

    # 小动物：只保留有掉落的，并补上栖息地图
    small_out = {}
    for ems, info in small_monsters.items():
        drops = monster_drops.get(str(-int(ems)))
        if not drops:
            continue
        entry = {**info, "drops": drops}
        # 地图名一并给出，卡片可直接显示「出现在哪些地图」。
        # 源数据里大多小动物没有栖息数据，需要外部抓取补；新值为空时沿用旧值，
        # 避免漏跑抓取那一步就把地图静默抹掉。
        names = [maps.get(n, "") for n in (info.get("maps") or []) if maps.get(n)]
        if not names:
            names = carried.get(f"small_monsters:{ems}") or []
        entry["map_names"] = names
        small_out[ems] = entry
    print(f"  快照内小动物: {len(small_out)} 只（含掉落与栖息地图）")

    maps_out = {
        str(number): {"name": name, "origin": map_origin.get(number, "inferred")}
        for number, name in sorted(maps.items())
    }

    return {
        "meta": {
            "schema": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "generator": "astrbot_plugin_mh_material/tools/extract.py",
            "game": "Monster Hunter Rise: Sunbreak",
            "source_files": {
                "mhrice": {
                    "name": mhrice_path.name,
                    "sha256": file_sha256(mhrice_path),
                },
                "items": {"name": items_path.name, "sha256": file_sha256(items_path)},
            },
            "notes": [
                "地图 12=密林 / 13=城塞高地 为按任务目标怪物推断，非源数据自带",
                "部位破坏报酬未附部位文字标签，仅有部位枚举",
                "素材名取自 items.json（简中），源数据名字表仅覆盖约 1100 项",
            ],
        },
        "tables": {
            "items": items_out,
            "monsters": monsters_out,
            "small_monsters": small_out,
            "quests": quests,
            "maps": maps_out,
            "quest_type_labels": QUEST_TYPE_LABELS,
            "rank_labels": RANK_LABELS,
            "source_labels": {key: label for key, label in SOURCE_KINDS},
        },
    }


def default_source_dir() -> Path:
    """源文件的默认位置，可用 --source-dir 覆盖。"""
    return Path(r"D:\下载")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成 mh_material 插件快照")
    parser.add_argument("--mhrice", type=Path, help="mhrice.json 路径")
    parser.add_argument("--items", type=Path, help="items.json 路径")
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=default_source_dir(),
        help="源文件目录（默认 D:\\下载）",
    )
    parser.add_argument("--out", type=Path, required=True, help="快照输出路径")
    parser.add_argument("--pretty", action="store_true", help="缩进输出")
    args = parser.parse_args(argv)

    mhrice_path = args.mhrice or (args.source_dir / "mhrice.json")
    items_path = args.items or (args.source_dir / "items.json")

    for path in (mhrice_path, items_path):
        if not path.exists():
            print(f"错误：源文件不存在 {path}", file=sys.stderr)
            return 2

    snapshot = build_snapshot(
        mhrice_path, items_path, previous_map_names=load_existing_map_names(args.out)
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        if args.pretty:
            json.dump(snapshot, handle, ensure_ascii=False, indent=2)
        else:
            json.dump(snapshot, handle, ensure_ascii=False, separators=(",", ":"))

    size_kb = args.out.stat().st_size / 1024
    print(f"\n快照已写入 {args.out}（{size_kb:.1f} KB）")
    return 0


# --------------------------------------------------------------------------
# 未来扩展占位：配装 / 技能查询
# --------------------------------------------------------------------------
# 用户已确认日后可能增加「配装 / 技能」查询。源数据里现成可用：
#   armor.json          防具（part/series_name/skills/decorations/resistances/...）
#   equip_skills.json   技能（name/description/max_level/levelDescriptions）
#   normal_decos.json   装饰品（name/skills/crafting_materials/...）
# 届时按下面的形状补两个提取函数，并把结果挂到 tables 下即可，
# 不必改动 drops/quests 的既有结构：
#
#   def extract_armor(path: Path) -> dict:      # -> {"armor": {...}}
#   def extract_skills(path: Path) -> dict:     # -> {"skills": {...}, "decorations": {...}}
#
# data.py 按需加载表，缺失时跳过，因此旧快照不会因为新表缺席而报错。


if __name__ == "__main__":
    raise SystemExit(main())
