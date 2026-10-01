# -*- coding: utf-8 -*-
"""怪物猎人素材查询插件（AstrBot · OneBot v11）。

指令：
    /mh 素材 <素材名>    查素材获取方式
    /mh 怪物 <怪物名>    查怪物掉落表
    /mh 任务 <任务名>    查任务星级/地图/奖励
    /mh help             帮助

数据来自 tools/extract.py 生成的快照，路径由 data.py 解析。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from . import commands as cmd
from .data import SnapshotError, load_snapshot
from .render import Card, render_card

PLUGIN_NAME = "mh_material"

# 唤醒前缀，用于从消息里剥掉。AstrBot 会把 wake_prefix 从 message_str 前面去掉，
# 但不同配置下可能残留，这里两种都兼容。
WAKE_PREFIX = "/"
COMMAND_TOKEN = "mh"


class MHMaterialPlugin(Star):
    """怪物猎人：崛起/曙光 素材查询。"""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._snapshot = None
        self._snapshot_error: str | None = None

    # ---- 快照 -----------------------------------------------------------

    def _get_snapshot(self):
        """惰性加载快照；失败时记住错误，避免每次查询都重试并刷日志。"""
        if self._snapshot is not None:
            return self._snapshot
        if self._snapshot_error is not None:
            return None
        try:
            self._snapshot = load_snapshot()
            stats = self._snapshot.stats()
            logger.info(
                "[mh_material] 快照加载完成：%s 种素材 / %s 只怪物 / %s 个任务",
                stats["materials"],
                stats["monsters"],
                stats["quests"],
            )
        except SnapshotError as exc:
            self._snapshot_error = str(exc)
            logger.error("[mh_material] 快照加载失败：%s", exc)
        except Exception as exc:  # noqa: BLE001 - 兜底，插件不应因数据问题崩溃
            self._snapshot_error = str(exc)
            logger.exception("[mh_material] 快照加载异常")
        return self._snapshot

    # ---- 指令 -----------------------------------------------------------

    @filter.command("mh")
    async def mh(self, event: AstrMessageEvent):
        """怪物猎人素材查询。/mh help 查看全部指令。"""
        # 参数不用 @filter.command 的注解式解析（GreedyStr）。
        # 原因：AstrBot 的 CommandFilter.init_handler_md 用 inspect.signature 直接读注解，
        # 而本模块顶部的 `from __future__ import annotations` 会把注解变成字符串，
        # 导致 `注解 is GreedyStr` 判定失败、参数被当成默认值，只取到第一个词。
        # 这里改为显式解析消息文本，行为不依赖注解求值时机。
        argument = extract_argument(event.get_message_str())

        snapshot = self._get_snapshot()
        if snapshot is None:
            yield event.plain_result(
                "素材数据尚未就绪：\n"
                f"{self._snapshot_error or '未知原因'}\n\n"
                "请在插件目录执行 tools/extract.py 生成快照后重试。"
            )
            return

        try:
            result = cmd.dispatch(snapshot, argument)
        except Exception as exc:  # noqa: BLE001 - 单个查询出错不该拖垮机器人
            logger.exception("[mh_material] 查询失败")
            yield event.plain_result(f"查询出错了：{exc}")
            return

        # 纯文本结果（帮助、候选列表、错误提示）
        if result.text:
            yield event.plain_result(result.text)
            return

        if result.card is None:
            yield event.plain_result("没有查到结果。")
            return

        async for item in self._emit_card(event, result.card):
            yield item

    async def _emit_card(self, event: AstrMessageEvent, card: Card):
        """渲染并发送卡片；渲染失败时退回纯文本，保证用户总能看到内容。"""
        try:
            kind, payload = render_card(card)
            if kind == "image":
                yield event.image_result(payload)
                return
            if kind == "html":
                # 走 AstrBot 官方文转图服务；未配置 t2i 时这里会抛错，由下面兜底
                url = await self.html_render(payload, {})
                yield event.image_result(url)
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning("[mh_material] 卡片渲染失败，回退纯文本：%s", exc)

        yield event.plain_result(card_to_text(card))

    async def terminate(self) -> None:
        """插件卸载时释放快照引用。"""
        self._snapshot = None


def extract_argument(message: str) -> str:
    """从消息文本里取出 ``/mh`` 之后的参数部分。

    AstrBot 通常已经把 wake_prefix 去掉了，但不同配置下可能残留，
    所以这里对「有前缀 / 无前缀」两种形态都做兼容。

    >>> extract_argument("/mh 素材 妖辉石")
    '素材 妖辉石'
    >>> extract_argument("mh 怪物 爵银龙")
    '怪物 爵银龙'
    >>> extract_argument("/mh")
    ''
    """
    text = (message or "").strip()
    if not text:
        return ""

    # 去掉可能残留的唤醒前缀
    while text.startswith(WAKE_PREFIX):
        text = text[len(WAKE_PREFIX):].lstrip()
    if not text:
        return ""

    lowered = text.lower()
    token = COMMAND_TOKEN
    if lowered == token:
        return ""
    if lowered.startswith(token + " "):
        return text[len(token):].strip()
    # 不是本指令（理论上不会走到，防御性返回原文，交由 dispatch 报未知子命令）
    return text


def card_to_text(card: Card) -> str:
    """把卡片降级成纯文本（渲染不可用时的兜底展示）。"""
    lines = [card.title]
    if card.subtitle:
        lines.append(card.subtitle)
    lines.append("")
    for section in card.sections:
        lines.append(f"■ {section.title}")
        for line in section.lines:
            text = "".join(segment for segment, _ in line.segments)
            lines.append(f"   {text}")
        if section.footer:
            lines.append(f"   （{section.footer}）")
        lines.append("")
    if card.footnote:
        lines.append(card.footnote)
    return "\n".join(lines).strip()
