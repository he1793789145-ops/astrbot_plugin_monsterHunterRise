# -*- coding: utf-8 -*-
"""把插件打成可分发压缩包。

排除自测产物与 __pycache__；快照会一并打进包里，所以对方开箱即用，
不必先跑 tools/extract.py。

用法::

    python tools/package.py
    python tools/package.py --out D:\\somewhere\\mh_material.zip
"""

from __future__ import annotations

import argparse
import hashlib
import zipfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_NAME = PLUGIN_ROOT.name

# 需要打进包里的文件/目录（相对插件根目录）
INCLUDE_FILES = (
    "main.py",
    "commands.py",
    "data.py",
    "render.py",
    "metadata.yaml",
    "requirements.txt",
    "README.md",
    "tools/extract.py",
    "tools/fetch_gathering.py",
    "tools/fetch_habitats.py",
    "tools/selftest.py",
    "tools/verify_astrbot_args.py",
    "data/plugin_data/mh_material/snapshot.json",
)

# 明确排除（即使将来放进目录也不打进去）
EXCLUDE_DIRS = {"__pycache__", "_selftest_out", ".git"}


def collect() -> list[Path]:
    files: list[Path] = []
    for relative in INCLUDE_FILES:
        path = PLUGIN_ROOT / relative
        if not path.exists():
            print(f"[警告] 缺少文件，跳过：{relative}")
            continue
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        files.append(path)
    return files


def build(out_path: Path) -> int:
    files = collect()
    if not files:
        print("[失败] 没有可打包的文件")
        return 2

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            # 压缩包内保留顶层目录，解压后直接就是插件目录
            arcname = f"{PLUGIN_NAME}/{path.relative_to(PLUGIN_ROOT).as_posix()}"
            archive.write(path, arcname)

    size = out_path.stat().st_size
    digest = hashlib.sha256(out_path.read_bytes()).hexdigest()
    print(f"已打包 {len(files)} 个文件 -> {out_path}")
    print(f"大小：{size / 1024:.1f} KB")
    print(f"SHA256：{digest}")
    print("\n压缩包内容：")
    with zipfile.ZipFile(out_path) as archive:
        for info in sorted(archive.infolist(), key=lambda i: i.filename):
            print(f"  {info.file_size:>10,}  {info.filename}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="打包 mh_material 插件")
    parser.add_argument(
        "--out",
        type=Path,
        default=PLUGIN_ROOT.parent / f"{PLUGIN_NAME}-1.0.0.zip",
        help="输出 zip 路径",
    )
    args = parser.parse_args(argv)
    return build(args.out)


if __name__ == "__main__":
    raise SystemExit(main())
