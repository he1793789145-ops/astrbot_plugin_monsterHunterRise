#!/usr/bin/env python3
"""从 mhrise.kiranico.com 抓取素材的采集点（目的地）数据，并入快照。

用法::

    python fetch_gathering.py --probe                     # 只探几个素材，验证页面结构
    python fetch_gathering.py --out <snapshot.json>       # 抓全量并合并进快照
    python fetch_gathering.py --out ... --limit 20        # 只抓前 N 个（调试）

设计要求：

* **只抓需要的素材页**，不做整站镜像；请求间隔 + 并发上限，避免给对方压力。
* 抓取结果带来源标注（站点、抓取时间），快照里可追溯。
* 站点没有许可证声明，所以这里只提取「地图 / 难度 / 数量 / 概率」这类事实数据，
  不复制任何文案。
* 抓取失败不阻断：单个素材失败只记录，已抓到的照常写回。
"""

from __future__ import annotations

import argparse
import gzip
import html as html_mod
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://mhrise.kiranico.com"
LIST_URL = f"{BASE}/zh/data/items?view=material"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 抓取节奏：并发 3、每次请求后停 0.4s，避免给站点造成压力
MAX_WORKERS = 3
REQUEST_DELAY = 0.4
TIMEOUT = 30
RETRIES = 2

# 表头关键词，用于定位「目的地」小节
SECTION_KEYWORD = "目的地"


def fetch(url: str) -> str:
    """抓取一个页面，返回 HTML 文本。"""
    last_error = None
    for attempt in range(RETRIES + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept-Language": "zh-CN,zh;q=0.9",
                    "Accept-Encoding": "gzip",
                },
            )
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                # 站点是 UTF-8，但个别字段可能混杂字节，用 replace 兜底
                return raw.decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < RETRIES:
                time.sleep(1.0 + attempt)
    raise RuntimeError(f"抓取失败 {url}: {last_error}")


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


def parse_material_list(html: str) -> dict:
    """从素材列表页解析出 {素材名: 详情页 URL}。"""
    items: dict[str, str] = {}
    # 每个条目形如：<a href="..."> ... <p class="...">素材名</p>
    pattern = re.compile(
        r'<a href="(https://mhrise\.kiranico\.com/zh/data/items/\d+)"[^>]*>'
        r"([\s\S]{0,600}?)</a>",
        re.MULTILINE,
    )
    for url, block in pattern.findall(html):
        name_match = re.search(r'<p class="[^"]*">([^<]+)</p>', block)
        if not name_match:
            continue
        name = html_mod.unescape(name_match.group(1)).strip()
        if name and name not in items:
            items[name] = url
    return items


def parse_gathering(html: str) -> list[dict]:
    """解析「目的地」小节里的表格，返回 [{map, rank, quantity, chance}, ...]。"""
    index = html.find(SECTION_KEYWORD)
    if index < 0:
        return []

    section = html[index:]
    # 只取到下一个二级标题为止，避免把后面的表格也吃进来
    next_heading = re.search(r"<h2", section[10:])
    if next_heading:
        section = section[: next_heading.start() + 10]

    rows = re.findall(r"<tr>([\s\S]*?)</tr>", section)
    results: list[dict] = []
    for row in rows:
        cells = [
            html_mod.unescape(re.sub(r"<[^>]*>", "", cell)).strip()
            for cell in re.findall(r"<td[^>]*>([\s\S]*?)</td>", row)
        ]
        if len(cells) < 4:
            continue
        map_name, rank, quantity, chance = cells[0], cells[1], cells[2], cells[3]
        if not map_name or not rank:
            continue
        results.append(
            {
                "map": map_name,
                "rank": rank,
                "quantity": quantity,
                "chance": chance,
            }
        )
    return results


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def probe(names: list[str]) -> int:
    """先验证页面结构与数据是否存在，再决定要不要全量抓。"""
    print("抓取素材列表页 ...")
    listing = parse_material_list(fetch(LIST_URL))
    print(f"  列表页解析到 {len(listing)} 个素材\n")

    for name in names:
        url = listing.get(name)
        if not url:
            print(f"[{name}] 列表页里没有该素材")
            continue
        try:
            page = fetch(url)
        except RuntimeError as exc:
            print(f"[{name}] {exc}")
            continue
        points = parse_gathering(page)
        print(f"[{name}] {url}")
        if points:
            for point in points[:6]:
                print(
                    f"    {point['map']}  {point['rank']}  {point['quantity']}  {point['chance']}"
                )
            if len(points) > 6:
                print(f"    ...共 {len(points)} 条")
        else:
            print("    （没有「目的地」数据）")
        print()
        time.sleep(REQUEST_DELAY)
    return 0


def crawl(listing: dict, limit: int | None = None) -> tuple[dict, list]:
    """并发抓取所有素材页，返回 ({素材名: [采集点]}, 失败列表)。"""
    targets = list(listing.items())
    if limit:
        targets = targets[:limit]

    print(f"开始抓取 {len(targets)} 个素材页（并发 {MAX_WORKERS}）...")
    gathered: dict[str, list] = {}
    failures: list = []
    done = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {}
        for name, url in targets:
            futures[pool.submit(_fetch_one, name, url)] = name
        for future in as_completed(futures):
            name = futures[future]
            done += 1
            try:
                points = future.result()
            except Exception as exc:  # noqa: BLE001
                failures.append((name, str(exc)))
            else:
                if points:
                    gathered[name] = points
            if done % 50 == 0 or done == len(targets):
                print(
                    f"  进度 {done}/{len(targets)}，已有采集点 {len(gathered)} 个，失败 {len(failures)}"
                )
    return gathered, failures


def _fetch_one(name: str, url: str) -> list:
    time.sleep(REQUEST_DELAY)  # 每个请求前稍作停顿
    return parse_gathering(fetch(url))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="抓取 Kiranico 采集点数据")
    parser.add_argument("--probe", action="store_true", help="只探测几个素材，验证结构")
    parser.add_argument(
        "--probe-names",
        nargs="*",
        default=["温暖的毛皮", "蜂蜜", "大地结晶", "炸药"],
        help="探测用的素材名",
    )
    parser.add_argument("--out", type=Path, help="快照路径（抓取结果合并进去）")
    parser.add_argument("--limit", type=int, help="只抓前 N 个（调试用）")
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path("gathering_cache.json"),
        help="原始抓取结果缓存，便于重跑不重复抓",
    )
    args = parser.parse_args(argv)

    if args.probe:
        return probe(args.probe_names)

    if not args.out:
        print("错误：需要 --out 指定快照路径（或加 --probe 只探测）", file=sys.stderr)
        return 2

    print("抓取素材列表页 ...")
    listing = parse_material_list(fetch(LIST_URL))
    print(f"  列表页解析到 {len(listing)} 个素材")

    gathered, failures = crawl(listing, args.limit)

    # 缓存原始结果
    args.cache.write_text(
        json.dumps(
            {
                "source": LIST_URL,
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "gathering": gathered,
                "failures": failures,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"\n抓取完成：{len(gathered)} 个素材有采集点，{len(failures)} 个失败")
    print(f"原始结果已缓存到 {args.cache}")

    # 合并进快照
    snapshot = json.loads(args.out.read_text(encoding="utf-8"))
    items = snapshot["tables"]["items"]
    matched = 0
    for item in items.values():
        points = gathered.get(item["name"])
        if points:
            item["gathering"] = points
            matched += 1
    snapshot["meta"].setdefault("notes", []).append(
        f"采集点数据来自 {LIST_URL}（抓取于 {datetime.now(timezone.utc).date()}），"
        "该站点未声明许可证，仅提取事实性字段"
    )
    args.out.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"已把 {matched} 个素材的采集点写入快照 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
