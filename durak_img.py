"""Отрисовка стола «Дурака» картинкой для Telegram.

Зависимости: pip install Pillow
Карты рисуются прямо в коде (масти — из простых фигур, значки 6-10 — как на настоящих картах),
поэтому картинка выглядит одинаково на любом сервере. Нужен только какой-нибудь TTF-шрифт
с кириллицей (DejaVu / Liberation / Arial — подхватится сам); если шрифтов нет вообще,
вместо В/Д/К/Т рисуются J/Q/K/A, а подписи — латиницей.

Модуль ничего не знает про правила игры: ему отдают готовые данные (см. render_table_png).
Блокирующая функция — из async-кода вызывать через asyncio.to_thread.
"""
import functools
import io
from typing import Iterable, Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ==================== НАСТРОЙКИ ====================
W = 960                   # ширина картинки, px (высота зависит от содержимого)
SS = 3                    # во сколько раз рисуем крупнее для сглаживания
CW, CH = 110, 154         # размер карты, px
RADIUS = 12               # скругление углов карты
JPEG_QUALITY = 93

FELT_CENTER = (30, 112, 78)
FELT_EDGE = (12, 56, 40)
CARD_BG = (253, 252, 248)
CARD_EDGE = (176, 176, 170)
RED = (196, 30, 48)
BLACK = (26, 26, 32)
GOLD = (255, 205, 72)
TEXT_LIGHT = (222, 240, 228)
TEXT_DIM = (150, 192, 168)
BACK_BASE = (38, 56, 124)
BACK_LINE = (78, 100, 172)
ROLE_ATTACK = (214, 118, 38)
ROLE_DEFEND = (66, 128, 212)
ROLE_OUT = (110, 120, 116)

_RANK_CYR = {6: "6", 7: "7", 8: "8", 9: "9", 10: "10", 11: "В", 12: "Д", 13: "К", 14: "Т"}
_RANK_LAT = {6: "6", 7: "7", 8: "8", 9: "9", 10: "10", 11: "J", 12: "Q", 13: "K", 14: "A"}

# ==================== ШРИФТЫ ====================
_BOLD = ("DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf", "LiberationSans-Bold.ttf",
         "FreeSansBold.ttf", "NotoSans-Bold.ttf")
_REGULAR = ("DejaVuSans.ttf", "arial.ttf", "Arial.ttf", "LiberationSans-Regular.ttf",
            "FreeSans.ttf", "NotoSans-Regular.ttf")
_ttf_found = {"ok": False}


@functools.lru_cache(maxsize=64)
def _font(size: int, bold: bool = True):
    size = max(6, int(size))
    for name in (_BOLD + _REGULAR) if bold else (_REGULAR + _BOLD):
        try:
            f = ImageFont.truetype(name, size)
            _ttf_found["ok"] = True
            return f
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def _has_cyrillic() -> bool:
    _font(12)  # прогрев: выясняем, нашёлся ли хоть один TTF
    return _ttf_found["ok"]


def _t(ru: str, en: str) -> str:
    return ru if _has_cyrillic() else en


def _rank_label(rank: int) -> str:
    return (_RANK_CYR if _has_cyrillic() else _RANK_LAT)[rank]


def _text(d, xy, s, font, fill, anchor="mm"):
    try:
        d.text(xy, s, font=font, fill=fill, anchor=anchor)
    except (ValueError, TypeError):  # битмапный шрифт без поддержки anchor
        d.text(xy, s, font=font, fill=fill)


def _fit(d, s: str, font, max_w: float) -> str:
    if d.textlength(s, font=font) <= max_w:
        return s
    while len(s) > 1 and d.textlength(s + "…", font=font) > max_w:
        s = s[:-1]
    return s + "…"


# ==================== МАСТИ ====================
# Фигуры в условных координатах -1..1 (y вниз): ("c", cx, cy, r) — круг, ("p", [точки]) — многоугольник.
_SUIT_SHAPES = {
    "H": [("c", -0.5, -0.4, 0.52), ("c", 0.5, -0.4, 0.52), ("p", [(-0.98, -0.3), (0, 1.0), (0.98, -0.3)])],
    "D": [("p", [(0, -1.0), (0.68, 0), (0, 1.0), (-0.68, 0)])],
    "S": [("c", -0.5, 0.22, 0.52), ("c", 0.5, 0.22, 0.52),
          ("p", [(0, -1.0), (0.98, 0.14), (-0.98, 0.14)]),
          ("p", [(0, 0.25), (-0.4, 1.0), (0.4, 1.0)])],
    "C": [("c", 0, -0.5, 0.44), ("c", -0.54, 0.18, 0.44), ("c", 0.54, 0.18, 0.44), ("c", 0, -0.05, 0.3),
          ("p", [(0, 0.1), (-0.34, 1.0), (0.34, 1.0)])],
}


def _suit_color(suit: str):
    return RED if suit in "HD" else BLACK


def _draw_suit(d, suit: str, cx: float, cy: float, r: float, fill, flip: bool = False):
    sg = -1 if flip else 1
    for sh in _SUIT_SHAPES[suit]:
        if sh[0] == "c":
            _, x, y, rad = sh
            px, py, pr = cx + x * r, cy + sg * y * r, rad * r
            d.ellipse((px - pr, py - pr, px + pr, py + pr), fill=fill)
        else:
            d.polygon([(cx + x * r, cy + sg * y * r) for x, y in sh[1]], fill=fill)


# ==================== ТАЙЛЫ КАРТ ====================
# Значки 6-10: (x, y) в долях рабочей области, нижняя половина рисуется «вверх ногами», как на настоящих картах.
_THIRD = 1 / 3
_PIPS = {
    6: [(0, 0), (1, 0), (0, .5), (1, .5), (0, 1), (1, 1)],
    7: [(0, 0), (1, 0), (0, .5), (1, .5), (0, 1), (1, 1), (.5, .25)],
    8: [(0, 0), (1, 0), (0, .5), (1, .5), (0, 1), (1, 1), (.5, .25), (.5, .75)],
    9: [(0, 0), (1, 0), (0, _THIRD), (1, _THIRD), (0, 2 * _THIRD), (1, 2 * _THIRD), (0, 1), (1, 1), (.5, .5)],
    10: [(0, 0), (1, 0), (0, _THIRD), (1, _THIRD), (0, 2 * _THIRD), (1, 2 * _THIRD), (0, 1), (1, 1),
         (.5, 1 / 6), (.5, 5 / 6)],
}


@functools.lru_cache(maxsize=48)
def _face_tile(card, S: int):
    rank, suit = card
    w, h = CW * S, CH * S
    tile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=RADIUS * S, fill=CARD_BG, outline=CARD_EDGE,
                        width=max(1, round(1.4 * S)))
    color = _suit_color(suit)
    label = _rank_label(rank)

    # индекс в углу (и такой же, перевёрнутый, в противоположном)
    cw_, ch_ = 26 * S, 54 * S
    corner = Image.new("RGBA", (cw_, ch_), (0, 0, 0, 0))
    cd = ImageDraw.Draw(corner)
    _text(cd, (cw_ / 2, 15 * S), label, _font((19 if len(label) == 1 else 15) * S), color)
    _draw_suit(cd, suit, cw_ / 2, 37 * S, 7 * S, color)
    tile.alpha_composite(corner, (5 * S, 5 * S))
    tile.alpha_composite(corner.rotate(180), (w - 5 * S - cw_, h - 5 * S - ch_))

    cx = CW / 2
    if rank == 14:  # туз — один большой значок
        _draw_suit(d, suit, cx * S, CH / 2 * S, 27 * S, color)
    elif rank in _PIPS:
        x0, x1, y0, y1 = 41, 69, 27, 127
        r = (8 if rank >= 9 else 9) * S
        for nx, ny in _PIPS[rank]:
            _draw_suit(d, suit, (x0 + nx * (x1 - x0)) * S, (y0 + ny * (y1 - y0)) * S, r, color, flip=ny > .5)
    else:  # валет, дама, король — рамка, буква и два значка
        tint = (252, 238, 236) if suit in "HD" else (238, 238, 244)
        d.rounded_rectangle((31 * S, 22 * S, 79 * S, 132 * S), radius=6 * S, fill=tint, outline=color,
                            width=max(1, round(2 * S)))
        _text(d, (cx * S, 77 * S), label, _font(46 * S), color)
        _draw_suit(d, suit, cx * S, 45 * S, 11 * S, color)
        _draw_suit(d, suit, cx * S, 109 * S, 11 * S, color, flip=True)
    return tile


@functools.lru_cache(maxsize=4)
def _back_tile(S: int):
    w, h = CW * S, CH * S
    tile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=RADIUS * S, fill=(244, 244, 240), outline=CARD_EDGE,
                        width=max(1, round(1.4 * S)))
    inset = 7 * S
    iw, ih = w - 2 * inset, h - 2 * inset
    inner = Image.new("RGBA", (iw, ih), BACK_BASE)
    idr = ImageDraw.Draw(inner)
    step = 15 * S
    for k in range(-ih, iw + ih, step):  # ромбическая сетка
        idr.line((k, 0, k + ih, ih), fill=BACK_LINE, width=max(1, S))
        idr.line((k, ih, k + ih, 0), fill=BACK_LINE, width=max(1, S))
    _draw_suit(idr, "D", iw / 2, ih / 2, 20 * S, (244, 244, 240))
    _draw_suit(idr, "D", iw / 2, ih / 2, 14 * S, BACK_BASE)
    mask = Image.new("L", (iw, ih), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, iw - 1, ih - 1), radius=7 * S, fill=255)
    tile.paste(inner, (inset, inset), mask)
    return tile


@functools.lru_cache(maxsize=16)
def _scaled(kind: str, S: int, scale_pct: int):
    """Уменьшенная копия рубашки (kind='back') — для чужих карт."""
    base = _back_tile(S)
    k = scale_pct / 100
    return base.resize((round(base.width * k), round(base.height * k)), Image.LANCZOS)


@functools.lru_cache(maxsize=16)
def _shadow_tile(w: int, h: int, S: int):
    """Мягкая тень под картой размером w×h (в px картинки)."""
    pad = 14
    img = Image.new("L", ((w + 2 * pad) * S, (h + 2 * pad) * S), 0)
    ImageDraw.Draw(img).rounded_rectangle((pad * S, (pad + 3) * S, (pad + w) * S, (pad + h + 3) * S),
                                          radius=RADIUS * S, fill=255)
    img = img.filter(ImageFilter.GaussianBlur(5 * S))
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.putalpha(img.point(lambda v: int(v * 0.55)))
    return out


@functools.lru_cache(maxsize=4)
def _dim_tile(S: int):
    w, h = CW * S, CH * S
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, w - 1, h - 1), radius=RADIUS * S, fill=(10, 20, 16, 120))
    return img


# ==================== ПРИМИТИВЫ КАРТИНКИ ====================
def _felt(w: int, h: int):
    """Сукно: тёмные края, светлее к центру (маленькая картинка, растянутая сглаживанием)."""
    px = [[FELT_EDGE] * 5 for _ in range(5)]
    for y in range(1, 4):
        for x in range(1, 4):
            px[y][x] = tuple((a + b) // 2 for a, b in zip(FELT_EDGE, FELT_CENTER))
    px[2][2] = FELT_CENTER
    small = Image.new("RGB", (5, 5))
    small.putdata([c for row in px for c in row])
    return small.resize((w, h), Image.BICUBIC).convert("RGBA")


def _put(canvas, tile, x: float, y: float, S: int):
    canvas.alpha_composite(tile, (round(x * S), round(y * S)))


def _card(canvas, card, x: float, y: float, S: int, *, dim: bool = False, glow: bool = False):
    if glow:
        d = ImageDraw.Draw(canvas)
        g = 4 * S
        d.rounded_rectangle((x * S - g, y * S - g, (x + CW) * S + g, (y + CH) * S + g),
                            radius=(RADIUS + 3) * S, fill=GOLD)
    _put(canvas, _shadow_tile(CW, CH, S), x - 14, y - 14, S)
    _put(canvas, _face_tile(card, S), x, y, S)
    if dim:
        _put(canvas, _dim_tile(S), x, y, S)


def _back(canvas, x: float, y: float, S: int, scale_pct: int = 100):
    k = scale_pct / 100
    _put(canvas, _shadow_tile(round(CW * k), round(CH * k), S), x - 14, y - 14, S)
    tile = _back_tile(S) if scale_pct == 100 else _scaled("back", S, scale_pct)
    _put(canvas, tile, x, y, S)


def _overlay_box(canvas, box, fill, radius, S, outline=None, width=0):
    x0, y0, x1, y1 = (round(v * S) for v in box)
    ov = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
    ImageDraw.Draw(ov).rounded_rectangle((0, 0, x1 - x0 - 1, y1 - y0 - 1), radius=radius * S, fill=fill,
                                         outline=outline, width=round(width * S))
    canvas.alpha_composite(ov, (x0, y0))


def _pill(canvas, text: str, cx: float, cy: float, S: int, fg, bg, size: int = 15, padx: int = 12,
          pady: int = 5, outline=None):
    d = ImageDraw.Draw(canvas)
    f = _font(size * S)
    tw = d.textlength(text, font=f) / S
    w, h = tw + 2 * padx, size + 2 * pady
    d.rounded_rectangle(((cx - w / 2) * S, (cy - h / 2) * S, (cx + w / 2) * S, (cy + h / 2) * S),
                        radius=h / 2 * S, fill=bg, outline=outline, width=round(1.5 * S) if outline else 0)
    _text(d, (cx * S, cy * S + S * 0.5), text, f, fg)


# ==================== БЛОКИ СТОЛА ====================
def _draw_deck(canvas, trump: str, trump_card, deck_count: int, discard_count: int, S: int):
    d = ImageDraw.Draw(canvas)
    px, py = 100, 34  # верхний левый угол стопки колоды
    if deck_count >= 1 and trump_card:
        rot = _face_tile(tuple(trump_card), S).rotate(90, expand=True)  # козырь лежит боком под колодой
        _put(canvas, _shadow_tile(CH, CW, S), 36 - 14, py + 24 - 14, S)
        _put(canvas, rot, 36, py + 24, S)
    if deck_count >= 2:
        for k in range(min(3, deck_count - 1), 0, -1):
            _back(canvas, px + (k - 1) * 0 - k * 2.2, py - k * 2.2, S)
        _back(canvas, px, py, S)
        cx, cy = px + CW - 4, py + CH - 4
        r = 16
        d.ellipse(((cx - r) * S, (cy - r) * S, (cx + r) * S, (cy + r) * S), fill=(18, 36, 28), outline=GOLD,
                  width=round(2 * S))
        _text(d, (cx * S, cy * S + S * 0.5), str(deck_count), _font(17 * S), TEXT_LIGHT)
    elif deck_count == 0:  # колода кончилась — остаётся только знак козыря
        cx, cy = px + 40, py + 70
        r = 46
        d.ellipse(((cx - r) * S, (cy - r) * S, (cx + r) * S, (cy + r) * S), fill=(18, 44, 32), outline=GOLD,
                  width=round(2.5 * S))
        _draw_suit(d, trump, cx * S, cy * S, 24 * S, (255, 255, 255) if trump in "SC" else (255, 120, 130))
    f = _font(15 * S, bold=False)
    line = _t(f"козырь   ·  колода {deck_count}  ·  бито {discard_count}",
              f"trump   ·  deck {deck_count}  ·  discard {discard_count}")
    _text(d, (150 * S, (py + CH + 32) * S), line, f, TEXT_DIM)
    # значок козырной масти рядом со словом «козырь»
    tw = d.textlength(line, font=f) / S
    sx = 150 - tw / 2 + d.textlength(_t("козырь", "trump"), font=f) / S + 14
    _draw_suit(d, trump, sx * S, (py + CH + 32) * S, 8 * S, (255, 255, 255) if trump in "SC" else (255, 120, 130))


def _draw_opponent(canvas, cx: float, top: float, cell_w: float, opp: dict, mini_pct: int, S: int):
    d = ImageDraw.Draw(canvas)
    k = mini_pct / 100
    mw, mh = CW * k, CH * k
    n = int(opp.get("count", 0))
    out = bool(opp.get("out"))
    shown = 0 if out else min(n, 10)
    if shown:
        step = min(22 * k / 0.7, max(6, (cell_w - mw - 16) / max(1, shown - 1))) if shown > 1 else 0
        total = mw + step * (shown - 1)
        x = cx - total / 2
        for i in range(shown):
            _back(canvas, x + i * step, top, S, mini_pct)
    else:
        _text(d, (cx * S, (top + mh / 2) * S), "✔" if out else "—", _font(40 * S), TEXT_DIM)
    name = _fit(d, str(opp.get("name", "")), _font(19 * S), (cell_w - 10) * S)
    _text(d, (cx * S, (top + mh + 22) * S), name, _font(19 * S), TEXT_LIGHT)
    cards_word = _t(_cards_ru(n), f"{n} cards")
    sub = _t("вышел", "out") if out else cards_word
    _text(d, (cx * S, (top + mh + 44) * S), sub, _font(15 * S, bold=False), TEXT_DIM)
    role = opp.get("role")
    if out:
        _pill(canvas, _t("ВЫШЕЛ", "OUT"), cx, top + mh + 70, S, (235, 240, 236), ROLE_OUT, size=13, padx=10, pady=3)
    elif role == "attack":
        _pill(canvas, _t("АТАКУЕТ", "ATTACK"), cx, top + mh + 70, S, (255, 255, 255), ROLE_ATTACK, size=13, padx=10, pady=3)
    elif role == "defend":
        _pill(canvas, _t("ОТБИВАЕТСЯ", "DEFENDS"), cx, top + mh + 70, S, (255, 255, 255), ROLE_DEFEND, size=13, padx=10, pady=3)


def _cards_ru(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} карта"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} карты"
    return f"{n} карт"


def _draw_table(canvas, table, y0: float, target_idx: Optional[int], status: Optional[str], S: int):
    d = ImageDraw.Draw(canvas)
    box = (28, y0, W - 28, y0 + 252)
    _overlay_box(canvas, box, (0, 0, 0, 38), 22, S, outline=(255, 255, 255, 40), width=1.5)
    pairs = [p for p in table]
    if not pairs:
        _text(d, (W / 2 * S, (y0 + 130) * S), _t("Стол пуст", "Table is empty"), _font(24 * S, bold=False), TEXT_DIM)
    else:
        pw, gap, off_x, off_y = CW + 28, 14, 28, 34
        n = len(pairs)
        total = n * pw + (n - 1) * gap
        if total > W - 80:  # на всякий случай: больше 6 пар в раунде не бывает
            gap = max(2, (W - 80 - n * pw) / max(1, n - 1))
            total = n * pw + (n - 1) * gap
        x = (W - total) / 2
        ty = y0 + 30
        for i, (att, dfn) in enumerate(pairs):
            _card(canvas, tuple(att), x, ty, S, glow=(target_idx is not None and i == target_idx and dfn is None))
            if dfn:
                _card(canvas, tuple(dfn), x + off_x, ty + off_y, S)
            x += pw + gap
    if status:
        _pill(canvas, status, W / 2, y0 + 2, S, TEXT_LIGHT, (14, 34, 26), size=18, padx=18, pady=7, outline=GOLD)


def _draw_hand(canvas, hand: list, y1: float, playable: Optional[set], S: int):
    d = ImageDraw.Draw(canvas)
    n = len(hand)
    label = _t(f"Твои карты · {n}", f"Your cards · {n}")
    _text(d, (44 * S, (y1 + 8) * S), label, _font(17 * S), TEXT_LIGHT, anchor="lm")
    if not n:
        return
    avail = W - 80
    step = min(CW + 14, (avail - CW) / (n - 1)) if n > 1 else 0
    total = CW + step * (n - 1)
    x = (W - total) / 2
    for card in hand:
        card = tuple(card)
        can = playable is None or card in playable
        lift = 0 if (playable is None or can) and playable is None else (-18 if can else 0)
        _card(canvas, card, x, y1 + 36 + lift, S, dim=(playable is not None and not can),
              glow=(playable is not None and can))
        x += step


# ==================== ГЛАВНАЯ ФУНКЦИЯ ====================
def render_table_png(trump: str, trump_card, deck_count: int, discard_count: int, table: Iterable,
                     hand: Optional[list] = None, opponents: Optional[list] = None,
                     playable: Optional[set] = None, target_idx: Optional[int] = None,
                     status: Optional[str] = None) -> io.BytesIO:
    """Картинка стола (JPEG).

    trump — масть козыря ("S", "H", "D", "C"); trump_card — нижняя карта колоды (рисуется, пока колода не пуста);
    table — [[карта_атаки, карта_защиты | None], ...]; hand — карты игрока (None — руку не показывать);
    opponents — [{"name", "count", "role": "attack"|"defend"|None, "out": bool}, ...];
    playable — какие карты руки сейчас можно сыграть (остальные затемняются; None — не выделять);
    target_idx — какую атакующую карту сейчас бьём (подсветка, когда непобитых несколько);
    status — короткая надпись на столе («Твоя атака»). Карта — кортеж (достоинство 6..14, масть)."""
    S = SS
    y0 = 250
    height = y0 + 252 + 28 + (208 if hand is not None else 0)
    canvas = _felt(W * S, height * S)

    _draw_deck(canvas, trump, trump_card, deck_count, discard_count, S)

    opponents = list(opponents or [])
    if opponents:
        left, right = 260, W - 28
        cell = (right - left) / len(opponents)
        mini = 70 if len(opponents) <= 2 else 56
        for i, opp in enumerate(opponents):
            _draw_opponent(canvas, left + cell * (i + 0.5), 38, cell, opp, mini, S)

    _draw_table(canvas, list(table), y0, target_idx, status, S)
    if hand is not None:
        _draw_hand(canvas, list(hand), y0 + 252 + 22, set(map(tuple, playable)) if playable is not None else None, S)

    img = canvas.convert("RGB").resize((W, height), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, subsampling=0)
    buf.seek(0)
    buf.name = "durak.jpg"
    return buf