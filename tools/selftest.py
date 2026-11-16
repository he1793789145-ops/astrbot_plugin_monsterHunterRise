"""离线自测：直接跑数据层与渲染层，不需要 AstrBot 运行时。

用法::

    python tools/selftest.py           # 跑查询并生成卡片到 _selftest_out/
    python tools/selftest.py --no-png  # 只跑逻辑，不生成图片

放在 tools/ 下是为了和 extract.py 一起构成「离线可验证」的部分：
插件的正确性不该只能靠把它装进正在运行的机器人来检验。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_NAME = PLUGIN_ROOT.name
# 插件内部用的是相对导入（AstrBot 以「包」的形式加载插件），
# 所以自测也要按包导入：把插件根目录的父目录加进 sys.path，
# 然后用包名 import，而不是把插件目录本身加进 sys.path。
sys.path.insert(0, str(PLUGIN_ROOT.parent))

# AstrBot 源码树（如果本地有）：main.py 依赖它的 API 模块，
# 有这份源码就能顺便把平台适配层也自测一遍；没有则跳过那部分。
_ASTRBOT_SRC = PLUGIN_ROOT.parent / "_ab_src"
if _ASTRBOT_SRC.is_dir():
    sys.path.insert(0, str(_ASTRBOT_SRC))

from importlib import import_module

_pkg = import_module(PLUGIN_NAME)
cmd = import_module(f"{PLUGIN_NAME}.commands")
_data = import_module(f"{PLUGIN_NAME}.data")
_render = import_module(f"{PLUGIN_NAME}.render")

SnapshotError = _data.SnapshotError
load_snapshot = _data.load_snapshot

QUERIES = [
    ("素材", "龙玉"),
    ("素材", "火龙的天鳞"),
    ("素材", "蜂蜜"),
    ("素材", "大地结晶"),  # 采集类素材：应有「采集点」小节
    ("素材", "妖辉石"),  # 仅任务报酬 + 采集点
    ("素材", "温暖的毛皮"),  # 小动物掉落：应标出精灵鹿/雪鹿与出现地图
    ("素材", "药草"),  # 小动物掉落归属：应显示丸鸟（曾经错标成毒狗龙）
    ("素材", "怪异化的凶刚角"),  # 傀异调查报酬：曾经显示「暂无掉落记录」
    ("素材", "怪异化的重骨"),  # 傀异调查报酬
    ("素材", "散发土香的重泥骨"),  # 野外采集点：应写清「水没林 · 骨冢」
    ("怪物", "爵银龙"),  # 部位破坏/剥取应细分到具体部位
    ("怪物", "爆鳞龙"),
    ("怪物", "精灵鹿"),  # 小动物：应有「出现地图」小节
    ("怪物", "野猪"),
    ("怪物", "丸鸟"),
    ("任务", "撕裂寂静者"),
    # V1.3.0：武器 / 技能 / 系列
    ("武器", "怪异化的皮"),  # 素材 -> 可做武器
    ("武器", "怪异化的皮 太刀"),  # 限定类型的全部
    ("武器", "炎火杰大剑"),  # 武器详情
    ("武器", "钢剑"),  # 中间武器：来源 + 可强化为
    ("武器", "村卫用防卫盾斧Ⅰ"),  # 类型专属信息：榴弹瓶
    ("武器", "伞鸟枪"),  # V1.3.1：怪物系列 + 武器类型
    ("武器", "雷神龙盾斧"),
    ("技能", "攻击"),
    ("技能", "修罗原珠"),  # 素材 -> 可做装饰珠
    # V1.4.0
    ("肉质", "爵银龙"),
    ("肉质", "泡狐龙"),  # 5 个状态变体
    ("肉质", "棘茶龙"),  # 7 个状态变体（展示时截到 4 个）
    ("肉质", "水兽"),  # 部位名清洗修好后才有 7 个部位
    ("装饰品", "攻击珠"),  # 不带【2】后缀也要命中
    ("装饰品", "痛恨龙珠"),  # 百龙珠：类别需求 + 评价值
    # V1.4.1：技能效果
    ("技能", "看破"),
    ("技能", "痛恨一击"),  # 百龙技能：只有总述，无分级
    # V1.5.0：解锁条件 / 成就 / 上下文帮助
    ("武器", "村卫用坚守大剑"),
    ("技能", "气血"),
    ("成就", ""),
    ("成就", "护符"),
    ("成就", "百龙"),
    ("怨虎龙", ""),  # 上下文帮助：应给出具体指令而非整页帮助
    ("伞鸟枪", ""),  # 系列查询也要能认出来
    ("阿巴阿巴不存在", ""),  # 完全认不出：一行说明 + 帮助图片
    # V1.5.1
    ("任务", "圆圆酸浆"),  # 结算奖励
    ("任务", "电光一闪"),  # 多来源分组
    ("素材", "蜂蜜"),  # 地图特产（采集点）
    ("素材", "大地结晶"),
    # V1.5.2：结算道具
    ("素材", "铋七彩石"),
    ("素材", "特产菇"),
    ("素材", "漂亮的尻子玉"),
    ("help", "武器"),
    ("help", ""),
    ("素材", ""),
    ("不存在的子命令", ""),
    ("素材", "绝对不存在的素材名"),
]

# 这些查询的结果必须包含指定小节/关键词，否则视为回归。
# 每一条都对应一个真实修过的 bug。
REQUIRED_SECTIONS = {
    "药草": ["丸鸟"],
    "怪异化的凶刚角": ["傀异调查", "Lv161"],
    "怪异化的重骨": ["傀异调查"],
    # 采集点必须精确到节点类型，而不只是地图名
    "散发土香的重泥骨": ["水没林", "骨冢"],
    "大地结晶": ["采集点", "矿脉"],
    # 部位破坏与剥取都要细分。V1.3.0 起部位名改用**官方简中名**
    # （parts_type 按 em_type.Em 取 text_for_monster_list），
    # 所以断言也随之改：爵银龙是「角破坏 / 翼破坏 / 前肢破坏」，
    # 不再是 part_map 那套内部碰撞体名（头部/翼/左前脚）。
    # 怪物卡按素材分块：素材名应作为分区标题出现，而不是只出现在行里
    "丸鸟": ["出现地图", "废神社", "丸鸟蛋", "药草"],
    "温暖的毛皮": ["精灵鹿"],
    # 大名的背壳那条 —— 用户最初报的 bug：曾错标成「左脚破坏」
    "大名盾蟹": ["背壳破坏"],
    # V1.3.0：素材 -> 武器 / 武器的类型专属信息
    "怪异化的皮 太刀": ["可做武器", "太刀"],
    "炎火杰大剑": ["锋利度", "孔位", "派生", "所需素材", "怪异炼化"],
    # V1.3.1：中间武器必须同时有「来源」与「可强化为」
    "钢剑": ["来源", "可强化为", "铁剑"],
    "村卫用防卫盾斧Ⅰ": ["榴弹瓶"],
    # V1.3.1：怪物系列 + 武器类型 -> 该系列的该类武器
    # V1.3.1：怪物系列 + 武器类型 -> 该系列的该类武器。
    # 键必须写成「指令 参数」——单独的「伞鸟枪」已被 V1.5.0 的上下文帮助占用。
    "武器 伞鸟枪": ["伞鸟衍生", "长枪"],
    "雷神龙盾斧": ["雷神龙衍生", "盾斧"],
    # V1.3.0：技能 -> 装饰珠 / 素材 -> 装饰珠
    "攻击": ["提供该技能的装饰珠", "攻击珠", "素材："],
    "修罗原珠": ["可做装饰珠", "素材："],
    # V1.4.1：技能效果（总述 + 逐等级）。
    # 用「指令 参数」作键 —— 单独的「攻击」已被上面那条占用。
    "技能 攻击": ["效果", "提高玩家的攻击力", "攻击力+3", "Lv7"],
    "技能 痛恨一击": ["效果", "负会心发动时"],
    # V1.4.0：分部位肉质（同一个怪物名在「怪物」与「肉质」下断言不同，
    # 所以这几条用「指令 参数」作键）
    "怪物 爵银龙": [
        "角破坏",
        "翼破坏",
        "前肢破坏",
        "本体剥取",
        "尾巴剥取",
        "弱点",
        "/mh 肉质",  # 怪物卡上要提示去哪查具体肉质
        "兽种",  # V1.4.2
        "古龙种",
    ],
    # V1.4.2：兽种。伞鸟在源数据的 species 表里被误标成 FlyingDragon，
    # 官方是鸟龙种 —— 这条断言就是防它退回去。
    "怪物 伞鸟": ["兽种", "鸟龙种"],
    "怪物 黑蚀龙": ["兽种", "？？？"],  # 游戏内也显示「？？？」，照抄
    # 肉质卡也要带兽种（V1.4.2 追加）
    "肉质 爵银龙": ["肉质", "晕", "弱点特效", "头部", "尾巴尖", "兽种", "古龙种"],
    "肉质 泡狐龙": ["肉质", "晕", "状态变化", "变化状态"],
    "肉质 棘茶龙": ["肉质", "状态变化", "变化状态"],
    # 部位名清洗修好后，水兽才有 7 个部位（此前是 0 个）
    "肉质 水兽": ["肉质", "头部", "7 个部位"],
    # V1.4.0：装饰品
    "装饰品 攻击珠": ["所需素材", "修罗原珠"],
    # V1.5.0：解锁条件（百龙珠已用外部图鉴核对过：土砂龙 + 大师进度 ≥1）
    "装饰品 痛恨龙珠": ["所需素材", "评价值", "土砂龙", "解锁条件", "大师进度"],
    "武器 村卫用坚守大剑": ["解锁条件", "集会所进度"],
    # 技能卡里每颗珠都要带解锁行；大师位技能也必须有总述（曾因 `_MR` 后缀差异全丢）
    "技能 气血": ["解锁", "入手", "击中大型怪物被破坏过的部位", "发动效果"],
    # V1.5.0：成就（勋章 100 枚）
    "成就 ": ["基础勋章", "大师勋章", "100"],
    "成就 护符": ["炎火村护符", "成为炎火村猎人"],
    "成就 百龙": ["百龙"],
    # V1.5.0：上下文帮助 —— 认出实体时给具体指令，而不是整页帮助
    "怨虎龙": ["/mh 怪物 怨虎龙", "/mh 肉质 怨虎龙"],
    "伞鸟枪": ["/mh 武器 伞鸟枪"],
    "阿巴阿巴不存在": ["没认出"],
    # V1.5.1：结算奖励 + 地图特产（采集点）。
    # 这两块数据此前因「int/str 键不一致」被静默丢光，断言就是防它再退回去。
    "任务 圆圆酸浆": ["结算奖励", "药草", "蜂蜜"],
    "素材 蜂蜜": ["采集点", "蜂巢", "废神社"],
    "素材 大地结晶": ["采集点", "矿脉"],
    # V1.5.2：结算道具（type=PayOff / CarryPayOff）此前被类型过滤挡在快照外，
    # 查询直接回「没有找到」。铋七彩石是用户实际反馈的那一个。
    "素材 铋七彩石": ["熔岩洞", "特产", "大师"],
    "素材 特产菇": ["废神社", "特产"],
    "素材 漂亮的尻子玉": ["河童蛙"],
    # /mh help <指令> 展开示例
    "help 武器": ["/mh 武器 怪异化的皮"],
}


def check_platform_layer() -> int:
    """平台适配层自测：参数解析、平台判定、卡片缓存清理。

    这部分要 import main.py，而 main.py 依赖 AstrBot 的 API 模块。
    没有 AstrBot 的机器上会跳过（并在输出里说明），不算失败。
    """
    print("===== 平台适配层 =====")
    try:
        main = import_module(f"{PLUGIN_NAME}.main")
    except Exception as exc:  # noqa: BLE001
        print(f"  [跳过] 无法导入 main.py（{type(exc).__name__}: {exc}）")
        print("         装好 AstrBot 或把其源码目录加入 sys.path 后才会检查。\n")
        return 0

    failures = 0

    print(f"  官方平台名：{sorted(main.OFFICIAL_PLATFORMS)}")
    for platform, expected in [
        ("aiocqhttp", False),
        ("qq_official", True),
        ("qq_official_webhook", True),
    ]:
        got = main._platform_name_to_official(platform)
        if got != expected:
            print(f"  [失败] 平台判定 {platform} → {got}（期望 {expected}）")
            failures += 1
        else:
            print(f"  [通过] 平台判定 {platform} → 官方={got}")

    parse_cases = {
        "/mh 素材 妖辉石": "素材 妖辉石",
        "mh 怪物 爵银龙": "怪物 爵银龙",
        "@小机器人 /mh 肉质 爵银龙": "肉质 爵银龙",
        "<@!123456> /mh 怪物 火龙": "怪物 火龙",
        "／mh　素材　龙玉": "素材 龙玉",
        "/mh": "",
        "mh": "",
    }
    for raw, expected in parse_cases.items():
        got = main.extract_argument(raw)
        if got == expected:
            print(f"  [通过] 解析 {raw!r} → {expected!r}")
        else:
            print(f"  [失败] 解析 {raw!r} → {got!r}（期望 {expected!r}）")
            failures += 1

    # 摘要降级不得超出官方平台的安全长度
    card = cmd.dispatch(load_snapshot(), "怪物 爵银龙").card  # 拿一张真实卡片来生成摘要
    summary = main.card_to_summary(card)
    if len(summary) <= main.SUMMARY_MAX_CHARS:
        print(f"  [通过] 摘要长度 {len(summary)} ≤ {main.SUMMARY_MAX_CHARS}")
    else:
        print(f"  [失败] 摘要长度 {len(summary)} > {main.SUMMARY_MAX_CHARS}")
        failures += 1
    if "图片发送失败" in summary:
        print("  [通过] 摘要含降级提示")
    else:
        print("  [失败] 摘要缺少降级提示")
        failures += 1

    # 表格类分区（肉质）的内容在 section.data 里，不在 section.lines 里，
    # 摘要必须能把表格摊成文本，否则只剩一个标题。
    meat_card = cmd.dispatch(load_snapshot(), "肉质 爵银龙").card
    meat_summary = main.card_to_summary(meat_card)
    for token in ("斩", "打", "弹", "晕", "头部"):
        if token in meat_summary:
            print(f"  [通过] 肉质摘要含表格内容（{token}）")
        else:
            print(f"  [失败] 肉质摘要缺少表格内容（{token}）")
            failures += 1
    if "完整内容见" in meat_summary:
        print("  [失败] 肉质摘要仍引导去 /mh help 看完整内容")
        failures += 1
    else:
        print("  [通过] 肉质摘要不误导到 /mh help")

    # 卡片缓存清理：在临时目录里造 50 张，剪枝后应只剩 40 张
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        fake_dir = Path(tmp)
        for index in range(50):
            path = fake_dir / f"mh_fake_{index:03d}.png"
            path.write_bytes(b"x")
            # mtime 递增，保证「最近」有确定顺序
            import os as _os

            _os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
        original_dir = _render.CARD_CACHE_DIR
        _render.CARD_CACHE_DIR = fake_dir
        try:
            _render._prune_card_files()
        finally:
            _render.CARD_CACHE_DIR = original_dir
        left = len(list(fake_dir.glob("*.png")))
        if left == _render.KEEP_CARD_FILES:
            print(f"  [通过] 缓存剪枝保留 {left} 张")
        else:
            print(f"  [失败] 缓存剪枝后剩 {left} 张（期望 {_render.KEEP_CARD_FILES}）")
            failures += 1

    print()
    return failures


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="mh_material 插件离线自测")
    parser.add_argument("--no-png", action="store_true", help="不生成卡片图片")
    parser.add_argument("--out", type=Path, default=PLUGIN_ROOT / "_selftest_out")
    args = parser.parse_args(argv)

    try:
        snapshot = load_snapshot()
    except SnapshotError as exc:
        print(f"[失败] {exc}")
        return 2

    stats = snapshot.stats()
    print(
        f"快照：{stats['materials']} 素材 / {stats['monsters']} 怪物 / {stats['quests']} 任务"
    )
    print(f"生成时间：{snapshot.generated_at}\n")

    failures = 0
    for name, argument in QUERIES:
        label = f"/mh {name} {argument}".strip()
        print(f"===== {label} =====")
        try:
            result = cmd.dispatch(snapshot, f"{name} {argument}".strip())
        except Exception as exc:  # noqa: BLE001
            print(f"  [异常] {exc}\n")
            failures += 1
            continue

        if result.text:
            body = result.text
            print("\n".join("  " + line for line in body.splitlines()[:18]))
            if len(body.splitlines()) > 18:
                print("  ...")
        elif result.card:
            print(f"  卡片标题：{result.card.title}")
            print(f"  副标题：{result.card.subtitle}")
            print(f"  分区数：{len(result.card.sections)}")
            for section in result.card.sections:
                print(f"    ■ {section.title}（{len(section.lines)} 行）")
                for line in section.lines[:2]:
                    print("        " + "".join(s for s, _ in line.segments))
            print(f"  脚注：{result.card.footnote}")

            if not args.no_png:
                kind, payload = _render.render_card(result.card)
                out_dir = args.out
                out_dir.mkdir(parents=True, exist_ok=True)
                safe = f"{name}_{argument or 'none'}".replace("/", "_").replace(
                    "\\", "_"
                )
                if kind == "image":
                    target = out_dir / f"{safe}.png"
                    target.write_bytes(Path(payload).read_bytes())
                    print(f"  卡片已生成：{target}")
                else:
                    target = out_dir / f"{safe}.html"
                    target.write_text(payload, encoding="utf-8")
                    print(f"  HTML 已生成：{target}")
        else:
            print("  [空结果]")
            failures += 1

        # 回归断言：这些查询必须包含指定小节/关键词。
        # 先按「指令 参数」找，再退回只按参数找 —— 同一个参数名可能属于
        # 两个指令（例如「爵银龙」既查 /mh 怪物 又查 /mh 肉质），
        # 只按参数索引会让两边互相打架。
        required = REQUIRED_SECTIONS.get(f"{name} {argument}".strip())
        if required is None:
            required = REQUIRED_SECTIONS.get(argument)
        if required:
            # 卡片内容分散在多个地方：分区标题/行文本/脚注/计数/指标条/副标题，
            # 以及 kind="table" 那种把数据放在 data 里的表格。
            chunks = []
            # 文本与卡片可能同时存在（例如「没认出 + 帮助图片」），两边都要看
            if result.text:
                chunks.append(result.text)
            if result.card:
                for section in result.card.sections:
                    chunks.append(section.title)
                    chunks.append(section.footer or "")
                    chunks.append(section.count or "")
                    chunks.append(
                        "\n".join(
                            "".join(s for s, _ in line.segments)
                            for line in section.lines
                        )
                    )
                    data = section.data or {}
                    chunks.append(
                        " ".join(str(item) for item in data.get("headers") or [])
                    )
                    for row in data.get("rows") or []:
                        chunks.append(" ".join(str(cell) for cell in row))
                    chunks.append(str(data.get("note") or ""))
                chunks.append(
                    "\n".join(
                        f"{label} {value}"
                        for label, value in (result.card.metrics or [])
                    )
                )
                chunks.append(result.card.subtitle or "")
                chunks.append(result.card.footnote or "")
            else:
                chunks.append(result.text or "")
            haystack = "\n".join(chunks)
            for keyword in required:
                if keyword in haystack:
                    print(f"  [通过] 含「{keyword}」")
                else:
                    print(f"  [失败] 缺少「{keyword}」")
                    failures += 1
        print()

    failures += check_platform_layer()

    print(f"自测完成，异常/空结果 {failures} 项")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
