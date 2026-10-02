#!/usr/bin/env python3
"""补全怪物（尤其小动物）的栖息地图，来源 gamecat.fun。

## 为什么需要这个

`mhrice.json` 里小动物的地图数据是空的：
`small_monsters[].habitat_area` 恒为 `null`，
`ecological.stage_info_list` 只有极少数条目有值（实测 37 只小动物里只有 2 条）。
所以「温暖的毛皮去哪打」只能回答「精灵鹿」，答不出「在哪张图」。

## 数据来源与解析

gamecat.fun（游猫网）的怪物页「怪物简介」段里有 `'''出现场地：'''` 字段，
形如 `[[场地/废神社|废神社]]、[[场地/沙原|沙原]]`，是静态 wikitext，可直接解析。

用 MediaWiki API 批量取（一次最多 50 个标题，37 只小怪 + 78 只大怪分三批即可）：

    api.php?action=query&titles=怪物/精灵鹿|怪物/甲虫&prop=revisions&rvprop=content
            &rvslots=main&rvsection=1&format=json&formatversion=2

## 许可证

该站未见明确的许可证声明（页脚只有备案号）。因此这里只提取「怪物 → 地图名」
这一事实性映射，不复制任何描述文案，并在快照 meta 里标注来源。是否使用请自行判断。

用法::

    python fetch_habitats.py --probe                 # 只验证解析
    python fetch_habitats.py --out <snapshot.json>   # 抓取并合并进快照
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://gamecat.fun/rise/zh/api.php"
CATEGORY = "Category:怪物"
PAGE_PREFIX = "怪物/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 30
BATCH = 50  # MediaWiki 单次 titles 上限
REQUEST_DELAY = 0.6  # 请求间隔，避免给对方压力
RETRIES = 4  # 被限流时重试次数
RETRY_BACKOFF = 2.5  # 退避基数（秒），第 n 次重试等 n * 基数

# `'''出现场地：'''` 后面那一行里，[[场地/XX|YY]] 的 XX
HABITAT_FIELD = re.compile(r"'''出现场地：'''(.*)")
PLACE_LINK = re.compile(r"\[\[场地/([^|\]]+)")


def fetch(params: dict) -> dict:
    """调用 MediaWiki API 并返回解析后的 JSON。

    站点在请求过密时会返回非 JSON（HTML 错误页或空响应），
    所以这里带重试与退避；仍然失败就抛 ValueError，由调用方决定是否跳过。
    """
    url = f"{API}?{urllib.parse.urlencode(params)}"
    last_error = ""
    for attempt in range(RETRIES):
        if attempt:
            time.sleep(RETRY_BACKOFF * attempt)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept-Encoding": "gzip",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"网络错误：{exc}"
            continue

        text = raw.decode("utf-8", errors="replace").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 被限流时常见：返回 HTML 或空体
            last_error = f"返回的不是 JSON（前 80 字符：{text[:80]!r}）"
            print(f"  [重试 {attempt + 1}/{RETRIES}] {last_error}")
    raise ValueError(f"API 请求失败：{last_error}")


def list_monster_pages() -> list[str]:
    """取 Category:怪物 下所有页面标题（去掉「怪物/」前缀）。"""
    data = fetch(
        {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": CATEGORY,
            "cmlimit": "500",
            "format": "json",
            "formatversion": "2",
        }
    )
    titles = []
    for member in (data.get("query") or {}).get("categorymembers") or []:
        title = member.get("title") or ""
        if title.startswith(PAGE_PREFIX):
            titles.append(title[len(PAGE_PREFIX) :])
    return [t for t in titles if "/" not in t]


def fetch_habitats(names: list[str]) -> dict:
    """批量取栖息地图，返回 {怪物名: [地图名, ...]}。"""
    habitats: dict[str, list[str]] = {}
    for start in range(0, len(names), BATCH):
        chunk = names[start : start + BATCH]
        titles = "|".join(PAGE_PREFIX + name for name in chunk)
        try:
            data = fetch(
                {
                    "action": "query",
                    "titles": titles,
                    "prop": "revisions",
                    "rvprop": "content",
                    "rvslots": "main",
                    "rvsection": "1",
                    "format": "json",
                    "formatversion": "2",
                }
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"  [警告] 批次 {start}-{start + len(chunk)} 抓取失败：{exc}")
            time.sleep(REQUEST_DELAY)
            continue

        for page in (data.get("query") or {}).get("pages") or []:
            title = page.get("title") or ""
            name = title.removeprefix(PAGE_PREFIX)
            revisions = page.get("revisions") or []
            if not revisions:
                continue
            content = ((revisions[0].get("slots") or {}).get("main") or {}).get(
                "content"
            ) or ""
            match = HABITAT_FIELD.search(content)
            if not match:
                continue
            maps = PLACE_LINK.findall(match.group(1))
            # 去重且保序
            seen = []
            for item in maps:
                if item not in seen:
                    seen.append(item)
            if seen:
                habitats[name] = seen
        print(
            f"  已处理 {min(start + BATCH, len(names))}/{len(names)}，累计 {len(habitats)} 只有地图"
        )
        time.sleep(REQUEST_DELAY)
    return habitats


def probe() -> int:
    """先验证解析是否正确。"""
    print(f"取 {CATEGORY} 成员 ...")
    names = list_monster_pages()
    print(f"  共 {len(names)} 个怪物页面")
    print(f"  样例：{names[:8]}")

    print("\n抓取栖息地 ...")
    habitats = fetch_habitats(names)
    print(f"\n拿到 {len(habitats)} 只怪物的地图\n")
    for name in sorted(habitats):
        print(f"  {name:<12} {'、'.join(habitats[name])}")
    return 0


def load_cache(path: Path) -> dict:
    """读取缓存里的栖息地图，失败返回空 dict。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    habitats = data.get("habitats")
    return habitats if isinstance(habitats, dict) else {}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="从 gamecat.fun 补全怪物栖息地图")
    parser.add_argument("--probe", action="store_true", help="只验证解析，不写快照")
    parser.add_argument("--out", type=Path, help="快照路径，结果合并进去")
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path("habitat_cache.json"),
        help="原始抓取结果缓存",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="忽略缓存强制联网重抓（默认缓存优先）",
    )
    args = parser.parse_args(argv)

    if args.probe:
        return probe()

    if not args.out:
        print("错误：需要 --out 指定快照路径（或加 --probe）", file=sys.stderr)
        return 2

    # ---- 缓存优先 ----
    # 该站会返回 JavaScript 反爬挑战页（不是限流，重试无用），而地图数据变化极少。
    # 所以缓存够用就直接用，不联网；需要更新时加 --refresh。
    cached = load_cache(args.cache)
    habitats = {}
    if cached and not args.refresh:
        print(f"使用缓存 {args.cache}（{len(cached)} 只怪物）")
        habitats = cached
    else:
        try:
            names = list_monster_pages()
            print(f"怪物页面 {len(names)} 个，开始抓取栖息地 ...")
            habitats = fetch_habitats(names)
            print(f"共 {len(habitats)} 只怪物拿到地图")
        except (ValueError, OSError) as exc:
            print(f"[警告] 抓取失败：{exc}", file=sys.stderr)
            if cached:
                print(f"[警告] 回退到缓存（{len(cached)} 只怪物）", file=sys.stderr)
                habitats = cached
            else:
                print(
                    "[警告] 没有可用缓存，栖息地图将保持缺失。"
                    "站点可能启用了反爬挑战，稍后重试或手动准备缓存。",
                    file=sys.stderr,
                )
                habitats = {}

    if habitats:
        args.cache.write_text(
            json.dumps(
                {
                    "source": f"{API} (gamecat.fun 游猫网)",
                    "fetched_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "habitats": habitats,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"原始结果已缓存到 {args.cache}")

    snapshot = json.loads(args.out.read_text(encoding="utf-8"))
    tables = snapshot["tables"]

    matched = 0
    for table_name in ("monsters", "small_monsters"):
        for entry in (tables.get(table_name) or {}).values():
            maps = habitats.get(entry.get("name") or "")
            if maps:
                entry["map_names"] = maps
                matched += 1

    snapshot["meta"].setdefault("notes", []).append(
        f"栖息地图来自 {API}（gamecat.fun，抓取于 {datetime.now(timezone.utc).date()}）；"
        "该站未声明许可证，仅提取「怪物→地图」事实映射"
    )
    args.out.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"已为 {matched} 只怪物写入栖息地图 -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
