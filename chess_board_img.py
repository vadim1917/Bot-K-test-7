"""Отрисовка шахматной доски картинкой (PNG) для Telegram.

Зависимости: pip install Pillow chess
Шрифты с шахматными символами не нужны: фигуры рисуются прямо в коде из простых форм,
поэтому картинка выглядит одинаково на любом сервере и в любом клиенте Telegram.
"""
import io
from typing import Optional

import chess
from PIL import Image, ImageDraw, ImageFont

# ==================== НАСТРОЙКИ ====================
CELL = 84                 # размер клетки в итоговой картинке, px
MARGIN = 30               # поля под координаты, px
SS = 3                    # во сколько раз рисуем крупнее для сглаживания
LIGHT_SQ = (240, 217, 181)
DARK_SQ = (181, 136, 99)
LAST_LIGHT = (205, 210, 106)
LAST_DARK = (170, 162, 58)
CHECK_COLOR = (224, 70, 70)
FRAME_BG = (38, 36, 33)
LABEL_COLOR = (205, 200, 190)

WHITE_FILL, WHITE_LINE, WHITE_DETAIL = (252, 250, 245), (30, 30, 30), (30, 30, 30)
BLACK_FILL, BLACK_LINE, BLACK_DETAIL = (58, 58, 62), (8, 8, 8), (225, 220, 205)

# ==================== ФИГУРЫ (координаты 0..100) ====================
# Формы: ("p", [точки]) — многоугольник, ("e", (x0, y0, x1, y1)) — эллипс/круг, ("r", (x0, y0, x1, y1)) — прямоугольник.
# Детали ("l", (x0, y0, x1, y1), ширина) / ("c", (cx, cy, r)) рисуются поверх цветом, контрастным заливке.
_BASE = [("r", (24, 77, 76, 87)), ("p", [(30, 70), (70, 70), (73, 78), (27, 78)])]


def _circle(cx, cy, r):
    return ("e", (cx - r, cy - r, cx + r, cy + r))


_PIECES = {
    chess.PAWN: dict(
        shapes=[_circle(50, 33, 12), ("p", [(40, 44), (60, 44), (67, 72), (33, 72)]), ("r", (35, 43, 65, 50))] + _BASE,
        details=[],
    ),
    chess.ROOK: dict(
        shapes=[
            ("r", (28, 16, 40, 34)), ("r", (44, 16, 56, 34)), ("r", (60, 16, 72, 34)),
            ("r", (28, 30, 72, 38)),
            ("p", [(34, 36), (66, 36), (70, 72), (30, 72)]),
            ("r", (26, 64, 74, 72)),
        ] + _BASE,
        details=[],
    ),
    chess.KNIGHT: dict(
        shapes=[
            ("p", [(27, 74), (33, 56), (45, 44), (31, 47), (22, 45), (23, 36), (35, 22),
                   (39, 12), (46, 20), (53, 14), (57, 22), (70, 36), (77, 56), (75, 74)]),
        ] + _BASE,
        details=[("c", (40, 30, 2.8)), ("c", (26, 40, 1.8))],
    ),
    chess.BISHOP: dict(
        shapes=[
            _circle(50, 15, 5.5),
            ("e", (37, 20, 63, 56)),
            ("p", [(42, 52), (58, 52), (65, 72), (35, 72)]),
            ("r", (33, 52, 67, 59)),
        ] + _BASE,
        details=[("l", (47, 27, 56, 39), 3.2)],
    ),
    chess.QUEEN: dict(
        shapes=[
            ("p", [(30, 56), (26, 28), (33, 46), (38, 22), (44, 42), (50, 18), (56, 42),
                   (62, 22), (67, 46), (74, 28), (70, 56)]),
            _circle(26, 25, 4.5), _circle(38, 19, 4.5), _circle(50, 15, 4.5),
            _circle(62, 19, 4.5), _circle(74, 25, 4.5),
            ("p", [(36, 52), (64, 52), (69, 72), (31, 72)]),
            ("r", (28, 52, 72, 60)),
        ] + _BASE,
        details=[],
    ),
    chess.KING: dict(
        shapes=[
            ("r", (46, 8, 54, 32)), ("r", (38, 15, 62, 23)),
            ("e", (33, 30, 67, 62)),
            ("p", [(36, 54), (64, 54), (69, 72), (31, 72)]),
            ("r", (30, 56, 70, 63)),
        ] + _BASE,
        details=[],
    ),
}


def _draw_shape(d: ImageDraw.ImageDraw, shape, ox: float, oy: float, k: float, fill, outline=None, width=0):
    kind, data = shape
    if kind == "p":
        pts = [(ox + x * k, oy + y * k) for x, y in data]
        d.polygon(pts, fill=fill, outline=outline, width=width) if width else d.polygon(pts, fill=fill)
    else:
        x0, y0, x1, y1 = data
        box = (ox + x0 * k, oy + y0 * k, ox + x1 * k, oy + y1 * k)
        if kind == "e":
            d.ellipse(box, fill=fill, outline=outline, width=width) if width else d.ellipse(box, fill=fill)
        else:
            d.rectangle(box, fill=fill, outline=outline, width=width) if width else d.rectangle(box, fill=fill)


def _draw_piece(d: ImageDraw.ImageDraw, piece: chess.Piece, x: float, y: float, size: float):
    spec = _PIECES[piece.piece_type]
    k = size / 100.0
    if piece.color == chess.WHITE:
        fill, line, detail = WHITE_FILL, WHITE_LINE, WHITE_DETAIL
    else:
        fill, line, detail = BLACK_FILL, BLACK_LINE, BLACK_DETAIL
    ow = max(2, round(2.6 * k))
    # 1) контур: каждая форма заливается цветом контура и обводится им же — получается чистый силуэт
    for shape in spec["shapes"]:
        _draw_shape(d, shape, x, y, k, fill=line, outline=line, width=ow)
    # 2) заливка поверх, без обводки
    for shape in spec["shapes"]:
        _draw_shape(d, shape, x, y, k, fill=fill)
    # 3) мелкие детали (глаз коня, прорезь слона)
    for det in spec["details"]:
        if det[0] == "l":
            x0, y0, x1, y1 = det[1]
            d.line((x + x0 * k, y + y0 * k, x + x1 * k, y + y1 * k), fill=detail, width=max(1, round(det[2] * k)))
        else:
            cx, cy, r = det[1]
            d.ellipse((x + (cx - r) * k, y + (cy - r) * k, x + (cx + r) * k, y + (cy + r) * k), fill=detail)


def _load_font(size: int):
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "Arial.ttf", "arial.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)  # Pillow >= 10.1: масштабируемый встроенный шрифт
    except TypeError:
        return ImageFont.load_default()


def _blend(a, b, t: float):
    return tuple(round(a[i] * (1 - t) + b[i] * t) for i in range(3))


def render_board_png(board: chess.Board, orient: bool = chess.WHITE, last_move: Optional[chess.Move] = None) -> io.BytesIO:
    """PNG-картинка доски. orient — чьими фигурами вниз (chess.WHITE / chess.BLACK).
    Подсвечивает последний ход и короля под шахом. Блокирующая — из async вызывать через asyncio.to_thread."""
    if last_move is None and board.move_stack:
        last_move = board.peek()
    highlight = {last_move.from_square, last_move.to_square} if last_move else set()
    check_sq = board.king(board.turn) if board.is_check() else None

    cell, margin = CELL * SS, MARGIN * SS
    side = margin * 2 + cell * 8
    img = Image.new("RGB", (side, side), FRAME_BG)
    d = ImageDraw.Draw(img)

    files = list(range(8)) if orient == chess.WHITE else list(range(7, -1, -1))
    ranks = list(range(7, -1, -1)) if orient == chess.WHITE else list(range(8))

    for row, r in enumerate(ranks):
        for col, f in enumerate(files):
            sq = chess.square(f, r)
            light = (f + r) % 2 == 1
            color = LIGHT_SQ if light else DARK_SQ
            if sq in highlight:
                color = LAST_LIGHT if light else LAST_DARK
            if sq == check_sq:
                color = _blend(color, CHECK_COLOR, 0.7)
            x, y = margin + col * cell, margin + row * cell
            d.rectangle((x, y, x + cell, y + cell), fill=color)
            piece = board.piece_at(sq)
            if piece:
                pad = cell * 0.06
                _draw_piece(d, piece, x + pad, y + pad, cell - 2 * pad)

    font = _load_font(int(MARGIN * SS * 0.55))
    for col, f in enumerate(files):
        cx = margin + col * cell + cell / 2
        ch = "abcdefgh"[f]
        for cy in (margin / 2, side - margin / 2):
            d.text((cx, cy), ch, fill=LABEL_COLOR, font=font, anchor="mm")
    for row, r in enumerate(ranks):
        cy = margin + row * cell + cell / 2
        for cx in (margin / 2, side - margin / 2):
            d.text((cx, cy), str(r + 1), fill=LABEL_COLOR, font=font, anchor="mm")

    final = side // SS
    img = img.resize((final, final), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    buf.name = "board.png"
    return buf
