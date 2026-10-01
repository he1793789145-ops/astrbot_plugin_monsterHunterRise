"""卡片渲染。

默认走 PIL 自绘，不依赖 AstrBot 的文转图服务（这台机器上 t2i 是关闭的）。
渲染入口按「一张卡片 = 若干 section」抽象，以后加配装/技能卡片可以复用同一套原语。

若日后在 AstrBot 面板里配好 t2i，把 RENDER_BACKEND 改成 "html" 即可走官方 html_render。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 渲染后端：png = 本地 PIL 绘制；html = AstrBot 官方文转图服务
RENDER_BACKEND = os.environ.get("MH_MATERIAL_RENDER", "png")

# 稀有素材的阈值（稀有度 >= 该值加高亮）
RARE_RARITY = 7

# 每只怪物最多列出几条获取方式
ENTRIES_PER_MONSTER = 6

# --------------------------------------------------------------------------
# 配色（深色底，接近游戏内图鉴的观感）
# --------------------------------------------------------------------------

COLORS = {
    "bg": (24, 26, 30),
    "panel": (33, 36, 42),
    "panel_alt": (38, 42, 49),
    "border": (60, 66, 76),
    "title": (238, 240, 244),
    "text": (200, 205, 214),
    "muted": (140, 147, 158),
    "accent": (232, 176, 74),  # 金色：稀有素材
    "accent_dim": (150, 116, 56),
    "rank": (110, 190, 230),  # 蓝色：难度
    "chance": (140, 210, 140),  # 绿色：概率
    "event": (230, 140, 170),  # 粉色：活动任务
}


def _font_candidates(bold: bool = False) -> list[str]:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    if bold:
        names = ["msyhbd.ttc", "msyh.ttc", "simhei.ttf"]
    else:
        names = ["msyh.ttc", "simhei.ttf", "simsun.ttc"]
    return [str(Path(windir) / "Fonts" / n) for n in names]


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """加载中文字体；找不到就退回 Pillow 默认位图字体（会丢中文，仅作兜底）。"""
    for path in _font_candidates(bold):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


# --------------------------------------------------------------------------
# 卡片的数据描述（渲染器只认这个结构，不认业务对象）
# --------------------------------------------------------------------------


@dataclass
class Line:
    """卡片里的一行：由若干段不同样式的文本组成。"""

    segments: list[tuple[str, str]] = field(default_factory=list)

    @staticmethod
    def of(*segments: tuple[str, str]) -> Line:
        return Line(segments=list(segments))


@dataclass
class Section:
    """卡片里的一个分区（例如一只怪物、一个任务）。"""

    title: str
    lines: list[Line] = field(default_factory=list)
    highlight: bool = False
    footer: str = ""


@dataclass
class Card:
    """一张待渲染的卡片。"""

    title: str
    subtitle: str = ""
    sections: list[Section] = field(default_factory=list)
    footnote: str = ""


# --------------------------------------------------------------------------
# 版式常量
# --------------------------------------------------------------------------

PADDING = 28
WIDTH = 820
LINE_GAP = 8
SECTION_GAP = 18
FONT_TITLE = 40
FONT_SUBTITLE = 22
FONT_SECTION = 26
FONT_BODY = 22
FONT_SMALL = 19


class CardRenderer:
    """把 Card 画成 PNG。"""

    def __init__(self, width: int = WIDTH) -> None:
        self.width = width
        self.f_title = load_font(FONT_TITLE, bold=True)
        self.f_subtitle = load_font(FONT_SUBTITLE)
        self.f_section = load_font(FONT_SECTION, bold=True)
        self.f_body = load_font(FONT_BODY)
        self.f_small = load_font(FONT_SMALL)

    # ---- 测量 -----------------------------------------------------------

    def _segment_font(self, style: str):
        return {
            "title": self.f_section,
            "body": self.f_body,
            "muted": self.f_small,
            "rank": self.f_body,
            "chance": self.f_body,
            "accent": self.f_body,
            "event": self.f_body,
        }.get(style, self.f_body)

    def _segment_color(self, style: str):
        return {
            "title": COLORS["title"],
            "body": COLORS["text"],
            "muted": COLORS["muted"],
            "rank": COLORS["rank"],
            "chance": COLORS["chance"],
            "accent": COLORS["accent"],
            "event": COLORS["event"],
        }.get(style, COLORS["text"])

    def _line_height(self, line: Line) -> int:
        heights = [self._segment_font(s).size + 4 for _, s in line.segments]
        return max(heights) if heights else FONT_BODY

    # ---- 绘制 -----------------------------------------------------------

    def render(self, card: Card) -> str:
        """渲染成 PNG，返回文件路径。"""
        # 先算高度，再一次性建画布，避免二次遍历
        y = PADDING
        y += FONT_TITLE + 10
        if card.subtitle:
            y += FONT_SUBTITLE + 14
        y += 12
        for section in card.sections:
            y += FONT_SECTION + 12
            for _ in section.lines:
                y += LINE_GAP
            for line in section.lines:
                y += self._line_height(line)
            if section.footer:
                y += self._line_height(Line.of((section.footer, "muted")))
            y += SECTION_GAP
        if card.footnote:
            y += FONT_SMALL + 16
        height = y + PADDING

        image = Image.new("RGB", (self.width, height), COLORS["bg"])
        draw = ImageDraw.Draw(image)

        cursor = PADDING
        # 标题
        draw.text(
            (PADDING, cursor), card.title, font=self.f_title, fill=COLORS["title"]
        )
        cursor += FONT_TITLE + 10
        if card.subtitle:
            draw.text(
                (PADDING, cursor),
                card.subtitle,
                font=self.f_subtitle,
                fill=COLORS["muted"],
            )
            cursor += FONT_SUBTITLE + 14

        cursor += 12
        for index, section in enumerate(card.sections):
            cursor = self._draw_section(draw, section, cursor, index)

        if card.footnote:
            draw.text(
                (PADDING, cursor + 4),
                card.footnote,
                font=self.f_small,
                fill=COLORS["muted"],
            )

        out_dir = Path(tempfile.gettempdir()) / "mh_material_cards"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = (
            out_dir
            / f"card_{abs(hash((card.title, card.subtitle, len(card.sections))))}.png"
        )
        image.save(out_path, "PNG")
        return str(out_path)

    def _draw_section(
        self, draw: ImageDraw.ImageDraw, section: Section, top: int, index: int
    ) -> int:
        # 分区背景
        block_top = top - 8
        block_height = FONT_SECTION + 12
        for line in section.lines:
            block_height += self._line_height(line) + LINE_GAP
        if section.footer:
            block_height += (
                self._line_height(Line.of((section.footer, "muted"))) + LINE_GAP
            )
        block_height += 10

        bg = COLORS["panel_alt"] if index % 2 else COLORS["panel"]
        draw.rounded_rectangle(
            (
                PADDING - 12,
                block_top,
                self.width - PADDING + 12,
                block_top + block_height,
            ),
            radius=10,
            fill=bg,
            outline=COLORS["border"],
            width=1,
        )

        cursor = top
        title_color = COLORS["accent"] if section.highlight else COLORS["title"]
        draw.text(
            (PADDING, cursor), section.title, font=self.f_section, fill=title_color
        )
        cursor += FONT_SECTION + 12

        for line in section.lines:
            x = PADDING
            for text, style in line.segments:
                font = self._segment_font(style)
                color = self._segment_color(style)
                draw.text((x, cursor), text, font=font, fill=color)
                x += int(draw.textlength(text, font=font))
            cursor += self._line_height(line) + LINE_GAP

        if section.footer:
            draw.text(
                (PADDING, cursor),
                section.footer,
                font=self.f_small,
                fill=COLORS["muted"],
            )
            cursor += self._line_height(Line.of((section.footer, "muted"))) + LINE_GAP

        return cursor + SECTION_GAP


# --------------------------------------------------------------------------
# HTML 后端（可选，需要 AstrBot 配好 t2i）
# --------------------------------------------------------------------------


def render_html(card: Card) -> str | None:
    """把 Card 转成 HTML，交给 AstrBot 的 html_render。

    未配置 t2i 时返回 None，调用方应回退到文本。
    """
    import html as html_mod

    rows = []
    for section in card.sections:
        lines = []
        for line in section.lines:
            parts = []
            for text, style in line.segments:
                parts.append(f'<span class="{style}">{html_mod.escape(text)}</span>')
            lines.append("<div class='line'>" + "".join(parts) + "</div>")
        footer = (
            f"<div class='footer'>{html_mod.escape(section.footer)}</div>"
            if section.footer
            else ""
        )
        cls = "section highlight" if section.highlight else "section"
        rows.append(
            f"<div class='{cls}'><div class='stitle'>{html_mod.escape(section.title)}</div>"
            + "".join(lines)
            + footer
            + "</div>"
        )

    subtitle = (
        f"<div class='subtitle'>{html_mod.escape(card.subtitle)}</div>"
        if card.subtitle
        else ""
    )
    footnote = (
        f"<div class='footnote'>{html_mod.escape(card.footnote)}</div>"
        if card.footnote
        else ""
    )
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body {{ background:#181a1e; color:#c8cdd6; font-family:"Microsoft YaHei",sans-serif;
       width:820px; margin:0; padding:28px; }}
h1 {{ color:#eef0f4; font-size:40px; margin:0 0 6px; }}
.subtitle {{ color:#8c939e; font-size:22px; margin-bottom:14px; }}
.section {{ background:#21242a; border:1px solid #3c424c; border-radius:10px;
            padding:14px 18px; margin-bottom:16px; }}
.section.highlight .stitle {{ color:#e8b04a; }}
.stitle {{ color:#eef0f4; font-size:26px; font-weight:bold; margin-bottom:8px; }}
.line {{ font-size:22px; line-height:1.45; }}
.rank {{ color:#6ebee6; }} .chance {{ color:#8cd28c; }}
.muted {{ color:#8c939e; font-size:19px; }} .event {{ color:#e68caa; }}
.footer {{ color:#8c939e; font-size:19px; margin-top:6px; }}
.footnote {{ color:#8c939e; font-size:19px; margin-top:10px; }}
</style></head><body><h1>{html_mod.escape(card.title)}</h1>{subtitle}
{"".join(rows)}{footnote}</body></html>"""


def get_renderer() -> CardRenderer:
    return CardRenderer()


def render_card(card: Card):
    """按当前后端渲染；返回 (类型, 内容)。

    ('image', path) 或 ('html', html_text)。调用方负责在 html 不可用时回退文本。
    """
    if RENDER_BACKEND == "html":
        markup = render_html(card)
        if markup:
            return "html", markup
    return "image", get_renderer().render(card)
