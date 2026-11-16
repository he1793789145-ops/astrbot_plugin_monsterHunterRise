"""怪物猎人素材查询插件（AstrBot · OneBot v11 / QQ 官方机器人）。

指令：
    /mh 素材 <素材名>    查素材获取方式
    /mh 怪物 <怪物名>    查怪物掉落表
    /mh 任务 <任务名>    查任务星级/地图/奖励
    /mh help             帮助

数据来自 tools/extract.py 生成的快照，路径由 data.py 解析。

平台适配（1.6.0 起）：
    OneBot v11（aiocqhttp）
        文本与图片分两条消息发出，保持原有行为不变。
    QQ 官方机器人（qq_official / qq_official_webhook）
        文本与图片合并成一条消息，减少被动回复次数（同一 msg_id 群聊上限 5 次）；
        纯文本强制关闭 Markdown；图片发不出去时降级为摘要文本。
"""

from __future__ import annotations

import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star

from . import commands as cmd
from .data import SnapshotError, load_snapshot
from .render import Card, render_card

PLUGIN_NAME = "mh_material"

# 唤醒前缀，用于从消息里剥掉。AstrBot 会把 wake_prefix 从 message_str 前面去掉，
# 但不同配置下可能残留，这里两种都兼容。
WAKE_PREFIX = "/"
COMMAND_TOKEN = "mh"

# QQ 官方机器人平台名（AstrBot 的 platform_meta.name）。
# 群聊被动回复同一 msg_id 最多 5 次，因此这类平台把文本与图片合并成一条消息。
OFFICIAL_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})

# 图片发不出去时的摘要上限（官方平台文本上限无公开数字，宁可少不可整条被拒）。
SUMMARY_MAX_SECTIONS = 2
SUMMARY_MAX_LINES = 6
SUMMARY_MAX_CHARS = 800

# 全角 ASCII → 半角 ASCII，用于指令词比较（1:1 等长，可按长度切片）。
_FULLWIDTH_MAP = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}

# 消息开头可能残留的 @ 片段：Discord/Satori 风格 <@123>、OneBot 风格 @昵称。
_LEADING_AT_RE = re.compile(r"^(?:<@[!&]?\d+>|<@!?[^>]*>|@[^\s@]+)[\s\u3000]*")


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

        if is_official_platform(event):
            async for item in self._reply_official(event, result):
                yield item
            return

        # ---- OneBot v11：保持原有行为，文本与图片各发一条 ----
        # 文本与卡片可以同时存在：认不出输入的实体时，先给一行说明，
        # 再附一张帮助图片（手机上图片比纯文本清晰得多）。
        if result.text:
            yield event.plain_result(result.text)
            if result.card is None:
                return

        if result.card is None:
            yield event.plain_result("没有查到结果。")
            return

        async for item in self._emit_card(event, result.card):
            yield item

    # ---- QQ 官方机器人 --------------------------------------------------

    async def _reply_official(self, event: AstrMessageEvent, result):
        """官方平台：文本与图片合并成一条消息，失败时降级为摘要文本。"""
        if result.card is None:
            yield _plain_result(event, result.text or "没有查到结果。")
            return

        image = await self._as_image_component(event, result.card)
        if image is None:
            # 图片发不出去：文本与摘要合并成一条，仍然只占一次回复
            summary = card_to_summary(result.card)
            if result.text:
                summary = f"{result.text}\n\n{summary}"
            yield _plain_result(event, summary)
            return

        chain = []
        if result.text:
            chain.append(Plain(text=result.text))
        chain.append(image)
        yield _chain_result(event, chain)

    async def _as_image_component(self, event: AstrMessageEvent, card: Card):
        """渲染卡片并转成消息链图片组件；任何失败都返回 None，由调用方降级。"""
        try:
            kind, payload = render_card(card)
            if kind == "html":
                # 走 AstrBot 官方文转图服务；未配置 t2i 时这里会抛错，由调用方降级
                payload = await self.html_render(payload, {})
                kind = "image"
            if kind != "image":
                return None
            return _image_component(payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[mh_material] 卡片渲染失败，降级摘要：%s", exc)
            return None

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


def card_id(card: Card) -> str:
    return f"mh_{abs(hash((card.title, card.subtitle, len(card.sections))))}"


def _platform_name(event: AstrMessageEvent) -> str:
    """取平台名；老版本 AstrBot 没有该方法时返回空串。"""
    try:
        name = event.get_platform_name()
    except Exception:  # noqa: BLE001 - 平台元信息缺失不该影响查询
        return ""
    return str(name or "").strip().lower()


def _platform_name_to_official(name: str) -> bool:
    """平台名是否为 QQ 官方机器人（含 Webhook 变体）。"""
    return str(name or "").strip().lower() in OFFICIAL_PLATFORMS


def is_official_platform(event: AstrMessageEvent) -> bool:
    """是否为 QQ 官方机器人（含 Webhook 变体）。"""
    return _platform_name_to_official(_platform_name(event))


def _plain_result(event: AstrMessageEvent, text: str):
    """纯文本结果；官方平台强制关闭 Markdown（直接发 msg_type=0）。"""
    result = event.plain_result(text)
    use_markdown = getattr(result, "use_markdown", None)
    if callable(use_markdown):
        use_markdown(False)
    return result


def _chain_result(event: AstrMessageEvent, chain: list):
    """文本 + 图片合并成一条消息，同样关闭 Markdown。"""
    result = event.chain_result(chain)
    use_markdown = getattr(result, "use_markdown", None)
    if callable(use_markdown):
        use_markdown(False)
    return result


def _image_component(source: str):
    """把渲染产物（本地路径或 http URL）转成消息链里的图片组件。"""
    if source.startswith(("http://", "https://")):
        return Image.fromURL(source)
    return Image.fromFileSystem(source)


def extract_argument(message: str) -> str:
    """从消息文本里取出 ``/mh`` 之后的参数部分。

    AstrBot 通常已经把 wake_prefix 去掉了，但不同配置下可能残留，
    所以这里对「有前缀 / 无前缀」两种形态都做兼容；同时清理开头的 @ 片段、
    全角空格与全角斜杠，避免不同适配器给出的文本形态差异导致指令失效。

    >>> extract_argument("/mh 素材 妖辉石")
    '素材 妖辉石'
    >>> extract_argument("mh 怪物 爵银龙")
    '怪物 爵银龙'
    >>> extract_argument("@小机器人 /mh 肉质 爵银龙")
    '肉质 爵银龙'
    >>> extract_argument("/mh")
    ''
    """
    text = normalize_message(message)
    if not text:
        return ""

    # 去掉可能残留的 @ 片段与唤醒前缀（顺序可能互换，循环几次都剥干净）
    while True:
        stripped = _LEADING_AT_RE.sub("", text)
        while stripped.startswith(WAKE_PREFIX):
            stripped = stripped[len(WAKE_PREFIX) :].lstrip()
        if stripped == text:
            break
        text = stripped
    if not text:
        return ""

    token = COMMAND_TOKEN
    # 比较时把全角 Ａ-Ｚ 等折成半角（1:1 等长，切片仍安全）
    shadow = text.translate(_FULLWIDTH_MAP).lower()
    if shadow == token:
        return ""
    if shadow.startswith(token + " "):
        return text[len(token) :].strip()
    # 不是本指令（理论上不会走到，防御性返回原文，交由 dispatch 报未知子命令）
    return text


def normalize_message(message: str) -> str:
    """统一空白与斜杠形态：全角空格→半角、全角斜杠→斜杠、连续空白压成一个。"""
    text = message or ""
    text = text.replace("\u3000", " ").replace("\u00a0", " ")
    text = text.replace("／", "/")
    return re.sub(r"\s+", " ", text).strip()


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


def _table_summary_lines(section, limit: int) -> list[str]:
    """把 ``kind="table"`` 的分区摊成文本行。

    表格类分区的内容放在 ``section.data``（headers/rows），``section.lines``
    是空的，直接按行拼接会只剩一个标题。

    手机端字体不是等宽的，用空格对齐的表格会散架，所以这里改成
    「每个部位一行、数字用｜分隔」的紧凑写法。
    """
    data = section.data or {}
    rows = data.get("rows") or []
    headers = data.get("headers") or []
    if not rows or not headers:
        return []

    out = []
    for row in rows[:limit]:
        if not row:
            continue
        name = str(row[0])
        # row[0] 是部位名，headers[0] 对的是 row[1]，所以表头要整体错开一格
        cells = [
            f"{headers[index - 1]}{row[index]}"
            for index in range(1, min(len(headers) + 1, len(row)))
        ]
        # 物理三列一组、属性五列一组，中间加竖线便于扫读
        head = " ".join(cells[:3])
        if len(cells) > 3:
            tail = " ".join(cells[3:8]) if len(cells) > 4 else ""
            extra = " ".join(cells[8:])
            body = (
                head + ("｜" + tail if tail else "") + ("｜" + extra if extra else "")
            )
        else:
            body = head
        out.append(f"   {name} {body}")
    return out


def card_to_summary(card: Card) -> str:
    """图片发不出去时的摘要：标题 + 至多 2 个分区、每区至多 6 行、总长受限。"""
    lines = [card.title]
    if card.subtitle:
        lines.append(card.subtitle)

    for section in card.sections[:SUMMARY_MAX_SECTIONS]:
        lines.append(f"■ {section.title}")
        if section.kind == "table":
            table_lines = _table_summary_lines(section, SUMMARY_MAX_LINES)
            lines.extend(table_lines)
            hidden = len(section.data.get("rows") or []) - SUMMARY_MAX_LINES
            if hidden > 0:
                lines.append(f"   …还有 {hidden} 个部位")
        else:
            for line in section.lines[:SUMMARY_MAX_LINES]:
                text = "".join(segment for segment, _ in line.segments)
                lines.append(f"   {text}")
            hidden = len(section.lines) - SUMMARY_MAX_LINES
            if hidden > 0:
                lines.append(f"   …还有 {hidden} 行")
        if section.footer:
            lines.append(f"   （{section.footer}）")

    hidden_sections = len(card.sections) - SUMMARY_MAX_SECTIONS
    if hidden_sections > 0:
        # 不要引导用户去 /mh help —— 那里是用法说明，不是本次结果。
        lines.append(f"…还有 {hidden_sections} 个分区未显示")

    text = "\n".join(lines)
    if len(text) > SUMMARY_MAX_CHARS - 20:
        text = text[: SUMMARY_MAX_CHARS - 20].rstrip() + "\n…（内容过长已截断）"
    return text + "\n（图片发送失败，以上为摘要）"
