"""平台适配测试台：不启动 AstrBot，直接检查插件在两种平台下的回复形态。

用法::

    python tools/platform_sim.py            # 跑全部用例
    python tools/platform_sim.py -v         # 顺带打印每条回复的消息链

为什么需要它：插件的平台差异只有「怎么把文本与图片交给宿主」这一处，
但那处一旦写错，在 QQ 官方机器人上表现是「只收到文字」「图片消失」或
「同一条消息重复发送被限流」。这些现象在 OneBot 上完全看不出来，
所以用一个假事件把两种平台的发送语义显式跑一遍。

模拟依据（AstrBot 4.28.2 源码）：
* ``core/pipeline/context_utils.py`` 每个 yield 触发一次独立发送；
* ``qqofficial_message_event.py`` 的 ``_split_message_chain_by_media``
  把「文本 + 图片」切成两块分别发出，即一条消息链也可能变成两次 API 调用；
* 同一 ``msg_id`` 被动回复有次数上限（群 5 次），所以官方平台要合并发送。

本文件只用于自测，不进插件包。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_NAME = PLUGIN_ROOT.name
sys.path.insert(0, str(PLUGIN_ROOT.parent))

# AstrBot 的源码树（若本地存在则用真实组件类，否则用等价的最小替身）。
_ASTRBOT_SRC = PLUGIN_ROOT.parent / "_ab_src"
if _ASTRBOT_SRC.is_dir():
    sys.path.insert(0, str(_ASTRBOT_SRC))


def _load_components():
    """优先用 AstrBot 真实组件；不可用时退回最小替身。"""
    try:
        from astrbot.api.message_components import Image, Plain  # type: ignore

        return Plain, Image, True
    except Exception:  # noqa: BLE001 - 测试台允许在没有 AstrBot 的机器上跑

        class Plain:
            def __init__(self, text: str = "") -> None:
                self.text = text

        class Image:
            def __init__(self, file: str = "", path: str = "") -> None:
                self.file = file
                self.path = path

            @staticmethod
            def fromFileSystem(path: str):
                return Image(file=Path(path).resolve().as_uri(), path=str(path))

            @staticmethod
            def fromURL(url: str):
                return Image(file=url)

        return Plain, Image, False


Plain, Image, REAL_COMPONENTS = _load_components()


@dataclass
class FakeResult:
    """模拟 AstrBot 的 MessageEventResult（只保留本插件用到的字段）。"""

    chain: list = field(default_factory=list)
    use_markdown_: bool | None = None

    def use_markdown(self, use: bool | None = True):
        self.use_markdown_ = use
        return self


@dataclass
class FakeEvent:
    """模拟 AstrMessageEvent 的平台相关面。"""

    platform: str
    message_str: str
    results: list[FakeResult] = field(default_factory=list)

    def get_platform_name(self) -> str:
        return self.platform

    def get_message_str(self) -> str:
        return self.message_str

    def plain_result(self, text: str) -> FakeResult:
        return FakeResult(chain=[Plain(text=text)])

    def image_result(self, url_or_path: str) -> FakeResult:
        if url_or_path.startswith("http"):
            return FakeResult(chain=[Image.fromURL(url_or_path)])
        return FakeResult(chain=[Image.fromFileSystem(url_or_path)])

    def chain_result(self, chain: list) -> FakeResult:
        return FakeResult(chain=list(chain))


def chain_kinds(result: FakeResult) -> str:
    return "+".join("text" if isinstance(c, Plain) else "image" for c in result.chain)


def markdown_label(value: bool | None) -> str:
    return {None: "默认", True: "md", False: "纯文本"}[value]


def media_chunks(result: FakeResult) -> int:
    """按 ``_split_message_chain_by_media`` 规则数一条链会被切成几次发送。

    连续两个媒体组件之间才切块；文本不打断。文本 + 1 张图 = 1 块。
    """
    chunks = 0
    has_media = False
    for component in result.chain:
        is_media = not isinstance(component, Plain)
        if is_media and has_media:
            chunks += 1
            has_media = False
        has_media = has_media or is_media
    return chunks + 1


def check(label: str, actual, expected, verbose: bool) -> bool:
    ok = actual == expected
    flag = "OK  " if ok else "FAIL"
    print(f"[{flag}] {label}：{actual!r}（期望 {expected!r}）")
    return ok


def run(verbose: bool) -> int:
    from importlib import import_module

    main = import_module(f"{PLUGIN_NAME}.main")

    print(f"AstrBot 真实组件类：{'是' if REAL_COMPONENTS else '否（使用替身）'}")
    print(f"官方平台名：{sorted(main.OFFICIAL_PLATFORMS)}")
    print()

    failures = 0

    # ---- 1. 平台判定 ---------------------------------------------------
    print("平台判定：")
    for platform, expect_official in [
        ("aiocqhttp", False),
        ("qq_official", True),
        ("qq_official_webhook", True),
    ]:
        event = FakeEvent(platform=platform, message_str="/mh help")
        if not check(
            f"  {platform:22s} 官方={main.is_official_platform(event)}",
            main.is_official_platform(event),
            expect_official,
            verbose,
        ):
            failures += 1

    # ---- 2. 参数解析 ---------------------------------------------------
    print()
    print("参数解析：")
    for raw, expected in {
        "/mh 素材 妖辉石": "素材 妖辉石",
        "mh 怪物 爵银龙": "怪物 爵银龙",
        "@小机器人 /mh 肉质 爵银龙": "肉质 爵银龙",
        "<@!123456> /mh 怪物 火龙": "怪物 火龙",
        "／mh　素材　龙玉": "素材 龙玉",
        "/mh": "",
        "/mh   ": "",
    }.items():
        if not check(f"  {raw!r}", main.extract_argument(raw), expected, verbose):
            failures += 1

    # ---- 3. 发送形态 ---------------------------------------------------
    print()
    print("发送形态（纯文本 + 图片，用替身卡片走真实分支）：")
    render_stub = _StubCard()
    for platform, expect_calls, expect_md in [
        # OneBot：文本一条 + 图片一条（两次 yield = 两次发送），Markdown 语义不动
        ("aiocqhttp", 2, None),
        # 官方：合并成一条，强制纯文本（避开 markdown 模板校验与回退）
        ("qq_official", 1, False),
        ("qq_official_webhook", 1, False),
    ]:
        event = FakeEvent(platform=platform, message_str="/mh help")
        results = _drive_plain_plus_image(main, event, render_stub)
        calls = sum(media_chunks(r) for r in results)
        if not check(f"  {platform:22s} 发送次数", calls, expect_calls, verbose):
            failures += 1
        md = results[0].use_markdown_ if results else None
        if not check(f"  {platform:22s} markdown", md, expect_md, verbose):
            failures += 1
        if verbose:
            for result in results:
                print(
                    f"        chain={chain_kinds(result)} "
                    f"markdown={markdown_label(result.use_markdown_)}"
                )

    # ---- 4. 图片不可用时的降级 -----------------------------------------
    print()
    print("图片失败降级：")
    for platform in ("qq_official", "qq_official_webhook"):
        event = FakeEvent(platform=platform, message_str="/mh help")
        results = _drive_plain_plus_image(main, event, render_stub, broken=True)
        if not check(f"  {platform:22s} 发送次数", len(results), 1, verbose):
            failures += 1
        if not check(
            f"  {platform:22s} 消息链", chain_kinds(results[0]), "text", verbose
        ):
            failures += 1
        body = results[0].chain[0].text
        if "图片发送失败" not in body:
            print("[FAIL]   降级文本缺少提示语")
            failures += 1
        if len(body) > main.SUMMARY_MAX_CHARS:
            print(f"[FAIL]   降级文本超长：{len(body)} > {main.SUMMARY_MAX_CHARS}")
            failures += 1

    print()
    if failures:
        print(f"失败 {failures} 项")
        return 1
    print("全部通过")
    return 0


# ---------------------------------------------------------------------------
# 驱动插件真实分支
# ---------------------------------------------------------------------------


class _StubCard:
    """可被 card_to_text / card_to_summary 消费的最小卡片替身。"""

    title: ClassVar[str] = "测试卡片"
    subtitle: ClassVar[str] = "副标题"
    badge: ClassVar[str] = ""
    footnote: ClassVar[str] = ""

    class _Section:
        title: ClassVar[str] = "测试分区"
        footer: ClassVar[str] = ""
        kind: ClassVar[str] = "rows"
        data: ClassVar[dict] = {}

        class _Line:
            segments: ClassVar[list] = [("第一行内容", None)]

        lines: ClassVar[list] = [_Line(), _Line(), _Line()]

    sections: ClassVar[list] = [_Section(), _Section(), _Section()]


def _drive_plain_plus_image(main, event: FakeEvent, card, broken: bool = False):
    """让插件的 _reply_official 与 OneBot 分支真的跑一遍。"""
    import asyncio

    class _Result:
        """模拟 commands 返回的 CommandResult。"""

        text = "这是一行说明文本"

    result_stub = _Result()
    result_stub.card = card

    import importlib
    import tempfile

    plugin_mod = importlib.import_module(f"{PLUGIN_NAME}.main")
    original_render = plugin_mod.render_card

    def _fake_render(_card, fail=broken):
        """render_card 的替身：成功时返回一个占位 PNG 路径。"""
        if fail:
            raise RuntimeError("模拟渲染失败")
        path = Path(tempfile.gettempdir()) / "mh_material_cards" / "sim_placeholder.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(b"\x89PNG\r\n\x1a\n")
        return "image", str(path)

    async def _fake_html(_markup, _ctx):
        raise RuntimeError("测试台不启用 html 渲染")

    class _Plugin:
        html_render = staticmethod(_fake_html)
        _as_image_component = plugin_mod.MHMaterialPlugin._as_image_component
        _reply_official = plugin_mod.MHMaterialPlugin._reply_official
        _emit_card = plugin_mod.MHMaterialPlugin._emit_card

    instance = _Plugin()

    async def _collect():
        out = []
        if plugin_mod.is_official_platform(event):
            async for item in instance._reply_official(event, result_stub):
                out.append(item)
        else:
            # OneBot 分支与 main.mh 完全一致：event.plain_result 不碰 Markdown 语义
            out.append(event.plain_result(result_stub.text))
            async for item in instance._emit_card(event, card):
                out.append(item)
        return out

    plugin_mod.render_card = _fake_render
    try:
        return asyncio.run(_collect())
    finally:
        plugin_mod.render_card = original_render


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="平台适配测试台")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印详细信息")
    args = parser.parse_args(argv)
    return run(args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
