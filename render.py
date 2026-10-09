"""卡片渲染（怪物猎人风格）。

默认走 PIL 自绘，不依赖 AstrBot 的文转图服务（这台机器上 t2i 是关闭的）。
渲染入口按「一张卡片 = 若干 section」抽象，以后加配装/技能卡片可以复用同一套原语。

## 怪猎要素

* **切角面板**：45° 切角取代圆角，怪猎 UI 的招牌几何
* **金色取景框**：面板四角 L 形括号 + 左缘页签强调条
* **公会徽记**：矢量绘制的翼 + 盾 + 中央菱形，配字距拉开的拉丁报头
* **稀有度刻度**：菱形点数按怪猎稀有度配色（1-2 灰 / 3-4 绿 / 5-6 蓝 / 7 紫 / 8 红 / 9-10 金）
* **部位图示**：爪痕（怪物）、定位菱形（地图）、旗标（任务）、傀异核（傀异调查）
* **概率量条**：概率列右对齐成列，左侧配一条按本分区最大值相对缩放的暗绿量条

## 实现要点：3 倍超采样

PIL 画多边形/细线**不做抗锯齿**，直接画会让切角与括号出现锯齿。
所以内部统一用 3 倍分辨率绘制、最后 LANCZOS 缩回，所有尺寸都以「逻辑像素」书写。

若日后在 AstrBot 面板里配好 t2i，把 RENDER_BACKEND 改成 "html" 即可走官方 html_render。
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 渲染后端：png = 本地 PIL 绘制；html = AstrBot 官方文转图服务
RENDER_BACKEND = os.environ.get("MH_MATERIAL_RENDER", "png")

# 超采样倍率
SS = 3

# --------------------------------------------------------------------------
# 配色：怪猎的深炭底 + 琥珀金 + 青蓝/苔绿
# --------------------------------------------------------------------------

COLORS = {
    "bg_deep": (11, 13, 16),
    "bg": (15, 17, 21),
    "panel": (23, 26, 31),
    "panel_alt": (27, 31, 37),
    "panel_rare": (30, 29, 26),
    "panel_rare_alt": (24, 24, 22),
    "border": (48, 54, 62),
    "border_soft": (36, 41, 48),
    "gold": (198, 160, 78),
    "gold_bright": (233, 199, 116),
    "gold_dim": (110, 90, 48),
    "title": (240, 236, 226),
    "text": (196, 200, 208),
    "muted": (128, 134, 145),
    "faint": (86, 92, 102),
    "rank": (112, 184, 212),
    "chance": (150, 204, 130),
    "event": (222, 138, 160),
    "gauge": (74, 106, 68),
    "hatch": (17, 19, 24),
}

# 怪猎稀有度的颜色梯度（1 -> 10）
RARITY_COLORS = [
    (150, 156, 166),
    (150, 156, 166),
    (140, 190, 140),
    (140, 190, 140),
    (118, 170, 220),
    (118, 170, 220),
    (186, 138, 220),
    (226, 140, 140),
    (232, 186, 96),
    (240, 210, 130),
]

# --------------------------------------------------------------------------
# 版式常量（逻辑像素）
# --------------------------------------------------------------------------

WIDTH = 880
MARGIN = 26
CHAMFER = 12
FONT_TITLE = 40
FONT_MASTHEAD = 12
FONT_SUBTITLE = 19
FONT_SECTION = 25
FONT_BODY = 21
FONT_SMALL = 18
LINE_GAP = 9
SECTION_GAP = 14
PANEL_PAD_X = 20
PANEL_PAD_TOP = 15
PANEL_PAD_BOTTOM = 15
GAUGE_MAX = 46

GLYPH_BY_TITLE = {
    "出现地图": "map",
    "可刷任务": "quest",
    "任务报酬": "quest",
    "傀异调查": "anomaly",
    "说明": "note",
    "暂无掉落记录": "note",
}

# 每只怪物最多列出几条获取方式（保留旧接口名，供 commands 使用）
ENTRIES_PER_MONSTER = 6


def _fonts_dir() -> Path:
    return Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """加载中文字体；找不到就退回 Pillow 默认位图字体（会丢中文，仅作兜底）。"""
    names = (
        ["msyhbd.ttc", "msyh.ttc", "simhei.ttf"] if bold else ["msyh.ttc", "simhei.ttf"]
    )
    for name in names:
        path = _fonts_dir() / name
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default()


def glyph_for(title: str) -> str:
    """分区标题 -> 图示。怪物名走爪痕，其余按关键词判定。"""
    if title in GLYPH_BY_TITLE:
        return GLYPH_BY_TITLE[title]
    if title.startswith("采集点"):
        return "map"
    if any(char in title for char in "怪龙兽鸟"):
        return "claw"
    return "note"


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
    glyph: str = ""


@dataclass
class Card:
    """一张待渲染的卡片。"""

    title: str
    subtitle: str = ""
    sections: list[Section] = field(default_factory=list)
    footnote: str = ""
    # 怪猎化的可选字段；缺失时不影响渲染
    rarity: int | None = None
    badge: str = ""


# --------------------------------------------------------------------------
# 绘制工具（内部一律 3 倍超采样）
# --------------------------------------------------------------------------


def chamfer_points(x0, y0, x1, y1, cut):
    """切角矩形（怪猎 UI 的招牌几何）。"""
    return [
        (x0 + cut, y0),
        (x1 - cut, y0),
        (x1, y0 + cut),
        (x1, y1 - cut),
        (x1 - cut, y1),
        (x0 + cut, y1),
        (x0, y1 - cut),
        (x0, y0 + cut),
    ]


def vgradient(size, top, bottom) -> Image.Image:
    """竖向渐变图（逐行画线，开销可忽略）。"""
    width, height = size
    strip = Image.new("RGB", (1, height))
    draw = ImageDraw.Draw(strip)
    for y in range(height):
        t = y / max(1, height - 1)
        draw.point(
            (0, y),
            fill=(
                int(top[0] + (bottom[0] - top[0]) * t),
                int(top[1] + (bottom[1] - top[1]) * t),
                int(top[2] + (bottom[2] - top[2]) * t),
            ),
        )
    return strip.resize((width, height), Image.NEAREST)


def parse_percent(text: str) -> float | None:
    """从段文本末尾取出百分比，用于画量条。取不到就不画。"""
    match = re.search(r"([\d.]+)\s*%\s*$", text or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


class Canvas:
    """带超采样的绘制包装：调用方只用逻辑坐标。"""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.image = Image.new("RGB", (width * SS, height * SS), COLORS["bg"])
        self.draw = ImageDraw.Draw(self.image)

    def textlength(self, text: str, font) -> float:
        return self.draw.textlength(text, font=font) / SS

    def text(self, xy, text, font, fill) -> None:
        self.draw.text((xy[0] * SS, xy[1] * SS), text, font=font, fill=fill)

    def tracked(self, xy, text, font, fill, tracking=0.0) -> float:
        """带字距的文本，返回结束 x（逻辑坐标）。"""
        x, y = xy
        for char in text:
            self.draw.text((x * SS, y * SS), char, font=font, fill=fill)
            x += self.textlength(char, font) + tracking
        return x

    def polygon(self, points, fill=None, outline=None, width=1) -> None:
        pts = [(x * SS, y * SS) for x, y in points]
        self.draw.polygon(pts, fill=fill, outline=outline)
        if outline and width > 1:
            self.draw.line(
                pts + [pts[0]], fill=outline, width=int(width * SS), joint="curve"
            )

    def rect(self, box, fill=None) -> None:
        x0, y0, x1, y1 = [v * SS for v in box]
        self.draw.rectangle((x0, y0, x1, y1), fill=fill)

    def line(self, points, fill, width=1) -> None:
        pts = [(x * SS, y * SS) for x, y in points]
        self.draw.line(pts, fill=fill, width=max(1, int(width * SS)), joint="curve")

    def paste_masked(self, source: Image.Image, box, mask_points) -> None:
        """把图片按多边形遮罩贴上去（用于切角面板里的渐变）。"""
        x0, y0, x1, y1 = [int(v * SS) for v in box]
        if x1 <= x0 or y1 <= y0:
            return
        mask = Image.new("L", (x1 - x0, y1 - y0), 0)
        ImageDraw.Draw(mask).polygon(
            [((x - box[0]) * SS, (y - box[1]) * SS) for x, y in mask_points], fill=255
        )
        self.image.paste(source.resize((x1 - x0, y1 - y0)), (x0, y0), mask)

    def finish(self) -> Image.Image:
        return self.image.resize((self.width, self.height), Image.LANCZOS)


def draw_hatch(canvas: Canvas, box, color, step=22, width=1) -> None:
    """极淡的斜向纹理。对比度要压到几乎看不见，否则会成规则条纹。"""
    x0, y0, x1, y1 = box
    span = (x1 - x0) + (y1 - y0)
    offset = -int(span)
    while offset < span:
        start_x = x0 + offset
        canvas.line([(start_x, y1), (start_x + (y1 - y0), y0)], fill=color, width=width)
        offset += step


def draw_crest(canvas: Canvas, cx, cy, size, gold, gold_bright) -> None:
    """公会徽记：翼 + 盾 + 中央菱形。

    小尺寸下细节必须克制——翼羽要从盾牌**外侧**起笔，
    否则会和盾牌糊成一团（曾经画成「蝴蝶」）。
    """
    half = size / 2
    for side in (-1, 1):
        for i in range(3):
            start_x = cx + side * half * 0.44
            start_y = cy - half * 0.30 + i * half * 0.30
            end_x = cx + side * (half * (0.92 - i * 0.10))
            end_y = start_y - half * 0.20
            canvas.line(
                [(start_x, start_y), (end_x, end_y)],
                gold if i else gold_bright,
                width=2,
            )

    shield = [
        (cx - half * 0.42, cy - half * 0.62),
        (cx + half * 0.42, cy - half * 0.62),
        (cx + half * 0.42, cy + half * 0.14),
        (cx, cy + half * 0.74),
        (cx - half * 0.42, cy + half * 0.14),
    ]
    canvas.polygon(shield, fill=(20, 22, 27))
    canvas.polygon(shield, outline=gold_bright, width=1)
    canvas.polygon(
        [
            (cx, cy - half * 0.32),
            (cx + half * 0.24, cy - half * 0.02),
            (cx, cy + half * 0.34),
            (cx - half * 0.24, cy - half * 0.02),
        ],
        fill=gold_bright,
    )


def draw_glyph(canvas: Canvas, kind: str, x, y, size, color, bright) -> None:
    """分区标题前的小图示。"""
    h = size
    if kind == "claw":
        for i in range(3):
            offset = x + i * (h * 0.30)
            canvas.line(
                [(offset, y + h), (offset + h * 0.22, y)],
                fill=bright if i == 1 else color,
                width=2,
            )
    elif kind == "map":
        canvas.polygon(
            [
                (x, y + h * 0.5),
                (x + h * 0.5, y),
                (x + h, y + h * 0.5),
                (x + h * 0.5, y + h),
            ],
            outline=bright,
            width=1,
        )
        canvas.polygon(
            [
                (x + h * 0.32, y + h * 0.5),
                (x + h * 0.5, y + h * 0.32),
                (x + h * 0.68, y + h * 0.5),
                (x + h * 0.5, y + h * 0.68),
            ],
            fill=bright,
        )
    elif kind == "quest":
        canvas.line([(x + h * 0.16, y), (x + h * 0.16, y + h)], fill=color, width=2)
        canvas.polygon(
            [
                (x + h * 0.16, y),
                (x + h * 0.94, y + h * 0.22),
                (x + h * 0.16, y + h * 0.46),
            ],
            fill=bright,
        )
    elif kind == "anomaly":
        # 傀异核：外菱形环 + 内实心（比螺旋在小尺寸下清楚得多）
        canvas.polygon(
            [
                (x + h * 0.5, y + h * 0.02),
                (x + h * 0.98, y + h * 0.5),
                (x + h * 0.5, y + h * 0.98),
                (x + h * 0.02, y + h * 0.5),
            ],
            outline=bright,
            width=2,
        )
        canvas.polygon(
            [
                (x + h * 0.5, y + h * 0.30),
                (x + h * 0.72, y + h * 0.5),
                (x + h * 0.5, y + h * 0.70),
                (x + h * 0.28, y + h * 0.5),
            ],
            fill=bright,
        )
    else:
        canvas.polygon(
            [
                (x + h * 0.5, y),
                (x + h, y + h * 0.5),
                (x + h * 0.5, y + h),
                (x, y + h * 0.5),
            ],
            outline=color,
            width=1,
        )


def draw_rarity(canvas: Canvas, x, y, rarity: int, size=9, gap=4) -> float:
    """稀有度刻度：怪猎式的菱形点数。返回结束 x。"""
    color = RARITY_COLORS[min(max(rarity, 1), 10) - 1]
    half = size / 2
    for i in range(min(rarity, 10)):
        cx = x + i * (size + gap) + half
        cy = y + half
        canvas.polygon(
            [(cx, cy - half), (cx + half, cy), (cx, cy + half), (cx - half, cy)],
            fill=color,
        )
    return x + min(rarity, 10) * (size + gap)


class CardRenderer:
    """把 Card 画成 PNG。"""

    def __init__(self, width: int = WIDTH) -> None:
        self.width = width
        self.f_title = load_font(FONT_TITLE * SS, bold=True)
        self.f_masthead = load_font(FONT_MASTHEAD * SS, bold=True)
        self.f_subtitle = load_font(FONT_SUBTITLE * SS)
        self.f_section = load_font(FONT_SECTION * SS, bold=True)
        self.f_body = load_font(FONT_BODY * SS)
        self.f_small = load_font(FONT_SMALL * SS)
        self.f_badge = load_font(13 * SS, bold=True)
        self.f_num = load_font(20 * SS, bold=True)

    # ---- 样式映射 -------------------------------------------------------

    def _segment_font(self, style: str):
        return {
            "title": self.f_section,
            "muted": self.f_small,
        }.get(style, self.f_body)

    def _segment_color(self, style: str):
        return {
            "title": COLORS["title"],
            "muted": COLORS["muted"],
            "rank": COLORS["rank"],
            "chance": COLORS["chance"],
            "event": COLORS["event"],
            "accent": COLORS["gold_bright"],
        }.get(style, COLORS["text"])

    def line_height(self, line: Line) -> int:
        sizes = [self._segment_font(style).size // SS + 4 for _, style in line.segments]
        return max(sizes) if sizes else FONT_BODY

    # ---- 测量 -----------------------------------------------------------

    def _panel_height(self, section: Section) -> int:
        height = PANEL_PAD_TOP + FONT_SECTION + 10
        for line in section.lines:
            height += self.line_height(line) + LINE_GAP
        if section.footer:
            height += FONT_SMALL + 6
        return height + PANEL_PAD_BOTTOM

    def measure(self, card: Card) -> int:
        y = 22 + 96
        if card.rarity or card.subtitle:
            y += 30
        y += 16
        for section in card.sections:
            y += self._panel_height(section) + SECTION_GAP
        if card.footnote:
            y += 16 + FONT_SMALL + 8
        return y + MARGIN

    # ---- 渲染 -----------------------------------------------------------

    def render(self, card: Card) -> str:
        height = self.measure(card)
        canvas = Canvas(self.width, height)
        self._draw_background(canvas, height)
        cursor = self._draw_header(canvas, card, 22)
        for section in card.sections:
            cursor = self._draw_panel(canvas, section, cursor)
        if card.footnote:
            self._draw_footnote(canvas, card.footnote, cursor + 6)

        image = canvas.finish()
        out_dir = Path(tempfile.gettempdir()) / "mh_material_cards"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = (
            out_dir
            / f"mh_{abs(hash((card.title, card.subtitle, len(card.sections))))}.png"
        )
        image.save(out_path, "PNG")
        return str(out_path)

    # ---- 各部分 ---------------------------------------------------------

    def _draw_background(self, canvas: Canvas, height: int) -> None:
        canvas.image.paste(
            vgradient((self.width * SS, height * SS), COLORS["bg"], COLORS["bg_deep"])
        )
        draw_hatch(
            canvas, (MARGIN, 118, self.width - MARGIN, height - 36), COLORS["hatch"]
        )

    def _draw_header(self, canvas: Canvas, card: Card, cursor: int) -> int:
        crest_size = 34
        draw_crest(
            canvas,
            MARGIN + crest_size / 2,
            cursor + crest_size / 2,
            crest_size,
            COLORS["gold"],
            COLORS["gold_bright"],
        )
        canvas.tracked(
            (MARGIN + crest_size + 14, cursor + 11),
            "MONSTER HUNTER RISE",
            self.f_masthead,
            COLORS["gold"],
            tracking=3.2,
        )
        if card.badge:
            text_w = canvas.textlength(card.badge, self.f_badge)
            pad = 10
            x1 = self.width - MARGIN
            x0 = x1 - text_w - pad * 2
            y0, y1 = cursor + 6, cursor + 30
            canvas.polygon(
                chamfer_points(x0, y0, x1, y1, 6),
                fill=(24, 27, 32),
                outline=COLORS["gold_dim"],
                width=1,
            )
            canvas.text(
                (x0 + pad, y0 + 5), card.badge, self.f_badge, COLORS["gold_bright"]
            )

        cursor += 40
        canvas.text((MARGIN, cursor), card.title, self.f_title, COLORS["title"])
        cursor += FONT_TITLE + 12

        if card.rarity or card.subtitle:
            x = MARGIN
            if card.rarity:
                end = draw_rarity(canvas, x, cursor + 3, card.rarity)
                color = RARITY_COLORS[min(max(card.rarity, 1), 10) - 1]
                # 数字与最后一枚菱形留出明确间距，否则「◆◆◆9」会读成一体
                x = end + 14
                canvas.text((x, cursor + 1), str(card.rarity), self.f_num, color)
                x += canvas.textlength(str(card.rarity), self.f_num) + 18
            if card.subtitle:
                canvas.text(
                    (x, cursor + 2), card.subtitle, self.f_subtitle, COLORS["muted"]
                )
            cursor += 30

        cursor += 10
        mid = self.width / 2
        canvas.line([(MARGIN, cursor), (mid - 12, cursor)], COLORS["gold_dim"], width=1)
        canvas.line(
            [(mid + 12, cursor), (self.width - MARGIN, cursor)],
            COLORS["gold_dim"],
            width=1,
        )
        canvas.polygon(
            [
                (mid, cursor - 4),
                (mid + 4, cursor),
                (mid, cursor + 4),
                (mid - 4, cursor),
            ],
            fill=COLORS["gold"],
        )
        return cursor + 22

    def _fit_text(self, canvas: Canvas, text: str, font, max_width: float) -> str:
        """按宽度截断文本，超长时以 … 结尾（用于标题/页脚这类单段文本）。"""
        if not text or canvas.textlength(text, font) <= max_width:
            return text
        ellipsis = "…"
        available = max_width - canvas.textlength(ellipsis, font)
        out = ""
        for char in text:
            char_w = canvas.textlength(char, font)
            if char_w > available:
                break
            out += char
            available -= char_w
        return (out + ellipsis) if out else ellipsis

    def _draw_panel(self, canvas: Canvas, section: Section, top: int) -> int:
        height = self._panel_height(section)
        x0, x1 = MARGIN, self.width - MARGIN
        box = (x0, top, x1, top + height)
        points = chamfer_points(x0, top, x1, top + height, CHAMFER)

        if section.highlight:
            base, alt = COLORS["panel_rare"], COLORS["panel_rare_alt"]
        else:
            base, alt = COLORS["panel"], COLORS["panel_alt"]
        canvas.paste_masked(
            vgradient((self.width * SS, height * SS), base, alt), box, points
        )
        canvas.polygon(points, outline=COLORS["border"], width=1)

        accent = COLORS["gold"] if section.highlight else COLORS["border"]
        canvas.line(
            [(x0 + 1, top + CHAMFER), (x0 + 1, top + height - CHAMFER)],
            accent,
            width=2 if section.highlight else 1,
        )
        self._corners(canvas, box)

        inner_left = x0 + PANEL_PAD_X
        inner_right = x1 - PANEL_PAD_X
        max_width = inner_right - inner_left

        cursor = top + PANEL_PAD_TOP
        glyph = section.glyph or glyph_for(section.title)
        draw_glyph(
            canvas,
            glyph,
            x0 + PANEL_PAD_X,
            cursor + 4,
            14,
            COLORS["gold_dim"],
            COLORS["gold"],
        )
        title_color = COLORS["gold_bright"] if section.highlight else COLORS["title"]
        canvas.text(
            (x0 + PANEL_PAD_X + 24, cursor),
            self._fit_text(canvas, section.title, self.f_section, max_width - 24),
            self.f_section,
            title_color,
        )
        cursor += FONT_SECTION + 10

        # 量条按「本分区最大值」相对缩放：掉落概率普遍在 5%~85%，
        # 用 0~100% 绝对刻度会把高位压成一排等长的条，看不出差别。
        section_max = 0.0
        for line in section.lines:
            for text, style in line.segments:
                if style == "chance":
                    value = parse_percent(text)
                    if value is not None:
                        section_max = max(section_max, value)

        for line in section.lines:
            laid, gauge = self._layout_line(canvas, line, max_width, section_max)
            row_height = self.line_height(line)
            if gauge:
                gx, gw = gauge
                mid_y = cursor + row_height / 2 - 1
                canvas.rect(
                    (inner_left + gx, mid_y, inner_left + gx + gw, mid_y + 3),
                    fill=COLORS["gauge"],
                )
            for offset, text, style in laid:
                canvas.text(
                    (inner_left + offset, cursor),
                    text,
                    self._segment_font(style),
                    self._segment_color(style),
                )
            cursor += row_height + LINE_GAP

        if section.footer:
            canvas.text(
                (inner_left, cursor + 2),
                self._fit_text(canvas, section.footer, self.f_small, max_width),
                self.f_small,
                COLORS["faint"],
            )
        return top + height + SECTION_GAP

    def _corners(self, canvas: Canvas, box) -> None:
        """四角 L 形取景框。"""
        x0, y0, x1, y1 = box
        inset, length = 6, 13
        for cx, cy, dx, dy in (
            (x0 + inset, y0 + inset, 1, 1),
            (x1 - inset, y0 + inset, -1, 1),
            (x0 + inset, y1 - inset, 1, -1),
            (x1 - inset, y1 - inset, -1, -1),
        ):
            canvas.line([(cx, cy), (cx + dx * length, cy)], COLORS["gold_dim"], width=1)
            canvas.line([(cx, cy), (cx, cy + dy * length)], COLORS["gold_dim"], width=1)

    def _layout_line(
        self, canvas: Canvas, line: Line, max_width: float, section_max: float = 0.0
    ):
        """排一行，返回 (段落列表, 概率量条)。

        - **空文本段先剔除**：否则末尾的空段会让「概率在末尾」的判定失败。
        - 末尾是唯一的 chance 段时贴右对齐，并在左侧画量条。
        - 末尾概率段自己就占半行以上时不右对齐：否则 base = max_width - tail
          会变成负数，文字被画到面板左侧之外。
        """
        segments = [(text, style) for text, style in line.segments if text]
        if not segments:
            return [], None

        styles = [style for _, style in segments]
        chance_indexes = [i for i, style in enumerate(styles) if style == "chance"]
        right_align = bool(chance_indexes) and chance_indexes[-1] == len(styles) - 1
        measured = [
            canvas.textlength(text, self._segment_font(style))
            for text, style in segments
        ]
        if right_align and measured[-1] > max_width * 0.5:
            right_align = False

        gauge = None
        gauge_reserve = 0.0
        if right_align:
            percent = parse_percent(segments[-1][0])
            if percent is not None:
                scale = section_max if section_max > 0 else 100.0
                ratio = min(max(percent / scale, 0.0), 1.0)
                length = max(6.0, GAUGE_MAX * ratio)
                gauge = (max_width - measured[-1] - 10 - length, length)
                gauge_reserve = GAUGE_MAX + 10

        tail = measured[-1] if right_align else 0.0
        budget = max(max_width - tail - (10 if right_align else 0) - gauge_reserve, 40)

        result = []
        used = 0.0
        for index, ((text, style), width) in enumerate(zip(segments, measured)):
            if right_align and index == len(segments) - 1:
                break
            if used + width <= budget:
                result.append((used, text, style))
                used += width
                continue
            font = self._segment_font(style)
            available = budget - used - canvas.textlength("…", font)
            piece = ""
            for char in text:
                char_w = canvas.textlength(char, font)
                if char_w > available:
                    break
                piece += char
                available -= char_w
            if piece:
                result.append((used, piece + "…", style))
            break

        if right_align:
            result.append((max_width - measured[-1], segments[-1][0], segments[-1][1]))
        return result, gauge

    def _draw_footnote(self, canvas: Canvas, text: str, y: int) -> None:
        canvas.line(
            [(MARGIN, y), (self.width - MARGIN, y)], COLORS["border_soft"], width=1
        )
        canvas.text(
            (MARGIN, y + 8),
            self._fit_text(canvas, text, self.f_small, self.width - MARGIN * 2),
            self.f_small,
            COLORS["faint"],
        )


# --------------------------------------------------------------------------
# HTML 后端（可选，需要 AstrBot 配好 t2i）
# --------------------------------------------------------------------------


def _css_rgb(rgb) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def render_html(card: Card) -> str | None:
    """把 Card 转成 HTML，交给 AstrBot 的 html_render。

    未配置 t2i 时返回 None，调用方应回退到文本。
    """
    import html as html_mod

    rows = []
    for section in card.sections:
        lines = []
        for line in section.lines:
            parts = [
                f'<span class="{style}">{html_mod.escape(text)}</span>'
                for text, style in line.segments
            ]
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
    badge = (
        f"<div class='badge'>{html_mod.escape(card.badge)}</div>" if card.badge else ""
    )
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body {{ background:{_css_rgb(COLORS["bg"])}; color:{_css_rgb(COLORS["text"])};
       font-family:"Microsoft YaHei",sans-serif; width:{WIDTH}px; margin:0; padding:{MARGIN}px; }}
h1 {{ color:{_css_rgb(COLORS["title"])}; font-size:{FONT_TITLE}px; margin:0 0 6px;
      letter-spacing:1px; }}
.masthead {{ color:{_css_rgb(COLORS["gold"])}; font-size:12px; letter-spacing:3px;
             font-weight:bold; }}
.subtitle {{ color:{_css_rgb(COLORS["muted"])}; font-size:{FONT_SUBTITLE}px; margin:4px 0 14px; }}
.badge {{ float:right; color:{_css_rgb(COLORS["gold_bright"])};
          border:1px solid {_css_rgb(COLORS["gold_dim"])}; border-radius:2px;
          padding:3px 10px; font-size:13px; }}
.rule {{ border-top:1px solid {_css_rgb(COLORS["gold_dim"])}; margin:10px 0 18px; }}
.section {{ background:{_css_rgb(COLORS["panel"])}; border:1px solid {_css_rgb(COLORS["border"])};
            padding:14px 18px; margin-bottom:{SECTION_GAP}px; border-left:2px solid
            {_css_rgb(COLORS["border"])}; }}
.section.highlight {{ background:{_css_rgb(COLORS["panel_rare"])};
                      border-left:2px solid {_css_rgb(COLORS["gold"])}; }}
.section.highlight .stitle {{ color:{_css_rgb(COLORS["gold_bright"])}; }}
.stitle {{ color:{_css_rgb(COLORS["title"])}; font-size:{FONT_SECTION}px; font-weight:bold;
          margin-bottom:8px; }}
.line {{ font-size:{FONT_BODY}px; line-height:1.5; }}
.rank {{ color:{_css_rgb(COLORS["rank"])}; }} .chance {{ color:{_css_rgb(COLORS["chance"])}; }}
.muted {{ color:{_css_rgb(COLORS["muted"])}; font-size:{FONT_SMALL}px; }}
.event {{ color:{_css_rgb(COLORS["event"])}; }}
.accent {{ color:{_css_rgb(COLORS["gold_bright"])}; }}
.footer {{ color:{_css_rgb(COLORS["faint"])}; font-size:{FONT_SMALL}px; margin-top:6px; }}
.footnote {{ color:{_css_rgb(COLORS["faint"])}; font-size:{FONT_SMALL}px; margin-top:14px;
             border-top:1px solid {_css_rgb(COLORS["border_soft"])}; padding-top:8px; }}
</style></head><body>
<div class="masthead">MONSTER HUNTER RISE</div>{badge}
<h1>{html_mod.escape(card.title)}</h1>{subtitle}
<div class="rule"></div>
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
