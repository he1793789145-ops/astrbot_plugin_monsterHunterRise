#!/usr/bin/env python3
"""按正确顺序重建快照，并在最后校验各数据块都在。

## 为什么需要这个脚本

快照由三步依次叠加而成，**顺序不能乱**：

1. ``extract.py``      —— 从 mhrice.json / items.json 生成基础表（**会重写整个快照**）
2. ``fetch_gathering.py`` —— 并入采集点（Kiranico），写入 ``items[].gathering``
3. ``fetch_habitats.py``  —— 并入栖息地图（gamecat），写入 ``monsters[].map_names``

踩过的坑：只跑 1 + 3 而漏掉 2，``extract.py`` 已经把快照重写干净，
于是采集点数据全部丢失，但**没有任何报错**——卡片上只是安静地少了一节。
所以这里把三步串起来，并做存在性校验。

用法::

    python rebuild_snapshot.py                      # 重建到插件自带位置
    python rebuild_snapshot.py --out <路径>
    python rebuild_snapshot.py --skip-fetch         # 只跑 extract（离线可用）
    python rebuild_snapshot.py --loglevel quiet
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
PLUGIN_ROOT = TOOLS.parent
DEFAULT_OUT = PLUGIN_ROOT / "data" / "plugin_data" / "mh_material" / "snapshot.json"
CACHE_DIR = PLUGIN_ROOT / "data" / "plugin_data" / "mh_material"

# 每个数据块至少要有一条，否则说明对应那一步没生效
EXPECTED = {
    "items": 1000,
    "monsters": 50,
    "small_monsters": 20,
    "quests": 500,
    "maps": 10,
}


def run(script: str, args: list[str]) -> int:
    cmd = [sys.executable, str(TOOLS / script), *args]
    print(f"[rebuild] {' '.join(cmd[1:])}")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print(f"[rebuild] {script} 失败（退出码 {result.returncode}）", file=sys.stderr)
    return result.returncode


def verify(path: Path, skip_fetch: bool = False) -> bool:
    """校验快照各数据块与两个附加数据源。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    tables = data.get("tables") or {}

    ok = True
    print("\n[rebuild] 数据块校验")
    for table, minimum in EXPECTED.items():
        count = len(tables.get(table) or {})
        flag = "OK " if count >= minimum else "不足"
        if count < minimum:
            ok = False
        print(f"  {flag} {table}: {count}（期望 >= {minimum}）")

    items = tables.get("items") or {}
    with_gathering = sum(1 for v in items.values() if v.get("gathering"))
    monsters = tables.get("monsters") or {}
    small = tables.get("small_monsters") or {}
    with_map = sum(
        1 for v in list(monsters.values()) + list(small.values()) if v.get("map_names")
    )
    with_part = sum(
        1
        for v in monsters.values()
        for d in (v.get("drops") or [])
        if d.get("part_name")
    )

    print("\n[rebuild] 附加数据校验")
    with_anomaly = sum(1 for v in items.values() if v.get("anomaly_rewards"))
    checks = [("部位破坏部位名", with_part, 500), ("傀异调查报酬", with_anomaly, 50)]
    if not skip_fetch:
        # 这两项来自联网抓取。--skip-fetch 时本次没跑它们，不该判为失败
        # （否则 --skip-fetch 永远报「不足」，掩盖真正的问题）。
        checks = [
            ("采集点（Kiranico）", with_gathering, 40),
            ("栖息地图（gamecat）", with_map, 90),
            *checks,
        ]
    for label, count, minimum in checks:
        flag = "OK " if count >= minimum else "不足"
        if count < minimum:
            ok = False
        print(f"  {flag} {label}: {count} 条素材/怪物（期望 >= {minimum}）")
    if skip_fetch:
        print("  （--skip-fetch：跳过联网数据项的校验）")

    if not skip_fetch and with_gathering < 40:
        print(
            "\n[rebuild] 提示：采集点数据缺失或过少，"
            "通常是漏跑了 fetch_gathering.py（它必须在 extract.py 之后执行）。"
        )
    return ok


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="按正确顺序重建快照")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path(r"D:\下载"),
        help="mhrice.json / items.json 所在目录",
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="只跑 extract，不联网抓取（附加数据会缺失）",
    )
    args = parser.parse_args(argv)

    out = args.out
    out.parent.mkdir(parents=True, exist_ok=True)

    # 1) 基础表（会重写整个快照）
    if run("extract.py", ["--source-dir", str(args.source_dir), "--out", str(out)]):
        return 1

    # 2) 采集点
    if not args.skip_fetch and run(
        "fetch_gathering.py",
        [
            "--out",
            str(out),
            "--cache",
            str(CACHE_DIR / "gathering_cache.json"),
        ],
    ):
        return 1

    # 3) 栖息地图
    if not args.skip_fetch and run(
        "fetch_habitats.py",
        [
            "--out",
            str(out),
            "--cache",
            str(CACHE_DIR / "habitat_cache.json"),
        ],
    ):
        return 1

    ok = verify(out, skip_fetch=args.skip_fetch)
    print(f"\n[rebuild] 快照：{out}")
    print(
        "[rebuild] 校验通过" if ok else "[rebuild] 校验未通过，请检查上面的『不足』项"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
