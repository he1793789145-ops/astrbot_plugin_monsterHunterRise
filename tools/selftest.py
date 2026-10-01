# -*- coding: utf-8 -*-
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

from importlib import import_module  # noqa: E402

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
    ("素材", "大地结晶"),      # 采集类素材：应有「采集点」小节
    ("素材", "妖辉石"),        # 仅任务报酬 + 采集点
    ("素材", "温暖的毛皮"),    # 小动物掉落：应标出精灵鹿/雪鹿与出现地图
    ("怪物", "爵银龙"),
    ("怪物", "爆鳞龙"),
    ("怪物", "精灵鹿"),        # 小动物：应有「出现地图」小节
    ("怪物", "野猪"),
    ("任务", "撕裂寂静者"),
    ("help", ""),
    ("素材", ""),
    ("不存在的子命令", ""),
    ("素材", "绝对不存在的素材名"),
]


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
    print(f"快照：{stats['materials']} 素材 / {stats['monsters']} 怪物 / {stats['quests']} 任务")
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
                safe = f"{name}_{argument or 'none'}".replace("/", "_").replace("\\", "_")
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
        print()

    print(f"自测完成，异常/空结果 {failures} 项")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
