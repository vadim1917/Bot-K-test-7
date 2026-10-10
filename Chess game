"""Шахматы для бота Амадеус.

Зависимость: pip install chess

Идея: ходы считает не нейронка, а простой встроенный движок (negamax + материал).
Нейронке в промпте отдаётся 1-3 кандидатных хода, она выбирает один из них
и комментирует партию в характере Курису. Если нейронка недоступна или выдала
мусор — ходит лучший кандидат движка.
"""
import random
import re
import time
from collections import deque
from typing import Optional

import chess

# ==================== НАСТРОЙКИ ====================
CHESS_SEARCH_DEPTH = 2        # глубина поиска в полуходах (2 = свой ход + ответ; 3 сильнее, но заметно медленнее)
CHESS_MAX_CANDIDATES = 3      # сколько ходов максимум отдаём нейронке
CHESS_CANDIDATE_MARGIN = 120  # кандидат не должен быть слабее лучшего больше чем на столько (сотые доли пешки)
CHESS_NOISE = 12              # небольшой шум оценки, чтобы партии не повторялись
CHESS_GAME_TTL_HOURS = 12     # брошенная партия стирается через столько часов

MATE_SCORE = 100000
INF = 10 ** 9

_PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}


def _build_center_bonus() -> dict:
    bonus = {}
    for sq in chess.SQUARES:
        f = chess.square_file(sq)
        r = chess.square_rank(sq)
        dist = max(abs(f - 3.5), abs(r - 3.5))
        bonus[sq] = int((3.5 - dist) * 6)
    return bonus


_CENTER_BONUS = _build_center_bonus()


# ==================== ДВИЖОК ====================
def _static_eval(board: chess.Board) -> int:
    """Оценка с точки зрения того, чей ход."""
    me = board.turn
    score = 0
    for sq, piece in board.piece_map().items():
        v = _PIECE_VALUES[piece.piece_type]
        if piece.piece_type in (chess.PAWN, chess.KNIGHT, chess.BISHOP):
            v += _CENTER_BONUS[sq]
        score += v if piece.color == me else -v
    return score


def _move_order_key(board: chess.Board, move: chess.Move) -> int:
    k = 0
    if board.is_capture(move):
        victim = board.piece_at(move.to_square)
        attacker = board.piece_at(move.from_square)
        victim_value = _PIECE_VALUES[victim.piece_type] if victim else 100  # взятие на проходе
        attacker_value = _PIECE_VALUES[attacker.piece_type] if attacker else 0
        k += 10 * victim_value - attacker_value
    if move.promotion:
        k += 800
    return -k


def _ordered_moves(board: chess.Board) -> list:
    return sorted(board.legal_moves, key=lambda m: _move_order_key(board, m))


def _quiesce(board: chess.Board, alpha: int, beta: int, qdepth: int) -> int:
    stand = _static_eval(board)
    if qdepth <= 0 or stand >= beta:
        return stand
    alpha = max(alpha, stand)
    captures = [m for m in board.legal_moves if board.is_capture(m)]
    captures.sort(key=lambda m: _move_order_key(board, m))
    for m in captures:
        board.push(m)
        score = -_quiesce(board, -beta, -alpha, qdepth - 1)
        board.pop()
        if score >= beta:
            return score
        if score > alpha:
            alpha = score
    return alpha


def _negamax(board: chess.Board, depth: int, alpha: int, beta: int) -> int:
    if depth <= 0:
        return _quiesce(board, alpha, beta, 3)
    moves = _ordered_moves(board)
    if not moves:
        return -MATE_SCORE if board.is_check() else 0
    best = -INF
    for m in moves:
        board.push(m)
        score = -_negamax(board, depth - 1, -beta, -alpha)
        board.pop()
        if score > best:
            best = score
        if best > alpha:
            alpha = best
        if alpha >= beta:
            break
    return best


def choose_candidates(board: chess.Board, max_candidates: int = CHESS_MAX_CANDIDATES) -> list:
    """Возвращает 1..max_candidates ходов, лучший первым. Блокирующая функция —
    из асинхронного кода вызывать через asyncio.to_thread."""
    b = board.copy(stack=False)
    scored = []
    for m in _ordered_moves(b):
        b.push(m)
        s = -_negamax(b, CHESS_SEARCH_DEPTH - 1, -INF, INF)
        b.pop()
        scored.append((s + random.randint(-CHESS_NOISE, CHESS_NOISE), m))
    if not scored:
        return []
    scored.sort(key=lambda x: -x[0])
    top = scored[0][0]
    if top >= MATE_SCORE - 1000:  # есть мат — берём только его
        return [scored[0][1]]
    return [m for s, m in scored[:max_candidates] if s >= top - CHESS_CANDIDATE_MARGIN]


def describe_move(board: chess.Board, move: chess.Move) -> str:
    """SAN + короткие пометки. Вызывать ДО того, как ход сделан на доске."""
    san = board.san(move)
    tags = []
    if board.is_castling(move):
        tags.append("рокировка")
    elif board.is_capture(move):
        tags.append("взятие")
    if move.promotion:
        tags.append("превращение пешки")
    if board.gives_check(move):
        b = board.copy(stack=False)
        b.push(move)
        tags.append("мат" if b.is_checkmate() else "шах")
    return f"{san} ({', '.join(tags)})" if tags else san


# ==================== РАЗБОР ХОДА ПОЛЬЗОВАТЕЛЯ ====================
_RU_UPPER = {"Ф": "Q", "Л": "R", "С": "B", "К": "N", "О": "O"}
_RU_LOWER = {"а": "a", "б": "b", "с": "c", "д": "d", "е": "e", "ф": "f", "г": "g", "х": "h"}
_MOVE_LIKE_RE = re.compile(r'^[a-hA-HKQRBNOkqrbnoКкФфЛлСсОоАаБбДдЕеГгХх0-8xX+#=\-:.]{2,10}$')


def _normalize_move_token(raw: str) -> str:
    s = (raw or "").strip().strip('.,!?;:()"«»\'')
    s = re.sub(r'^\d+\.+', '', s)
    s = s.replace('—', '-').replace('–', '-')
    s = s.replace('Кр', 'K').replace('кр', 'K')
    out = []
    for ch in s:
        if ch in _RU_UPPER:
            out.append(_RU_UPPER[ch])
        elif ch in _RU_LOWER:
            out.append(_RU_LOWER[ch])
        else:
            out.append(ch)
    s = ''.join(out).replace('0', 'O')
    s = re.sub(r'[+#!?]+$', '', s)
    if s.lower() in ('o-o', 'o-o-o'):
        s = s.upper()
    return s


def _parse_token(board: chess.Board, raw: str) -> Optional[chess.Move]:
    s = _normalize_move_token(raw)
    if not s:
        return None

    variants = [s]
    if s[0] in 'ABCDEFGH':
        variants.append(s.lower())
    for v in variants:
        try:
            return board.parse_san(v)
        except ValueError:
            continue

    u = s.lower().replace('-', '').replace('x', '').replace(':', '')
    if re.fullmatch(r'[a-h][1-8][a-h][1-8][qrbn]?', u):
        for cand in (u, u + 'q'):
            try:
                m = chess.Move.from_uci(cand)
            except ValueError:
                continue
            if m in board.legal_moves:
                return m
    return None


def parse_message(board: chess.Board, text: str):
    """Ищет ход в начале (или в конце) сообщения; остальное — комментарий.
    Возвращает (move | None, comment)."""
    parts = (text or "").strip().split()
    if not parts:
        return None, ""
    move = _parse_token(board, parts[0])
    if move:
        return move, " ".join(parts[1:]).strip()
    if len(parts) > 1:
        move = _parse_token(board, parts[-1])
        if move:
            return move, " ".join(parts[:-1]).strip()
    return None, (text or "").strip()


def looks_like_move_attempt(text: str) -> bool:
    """Одно короткое «слово», похожее на запись хода (но не распознанное как легальный ход)."""
    t = (text or "").strip()
    return bool(t) and ' ' not in t and bool(_MOVE_LIKE_RE.match(t))


def legal_moves_san(board: chess.Board, limit: int = 40) -> str:
    sans = sorted(board.san(m) for m in board.legal_moves)
    extra = f" … (+{len(sans) - limit})" if len(sans) > limit else ""
    return ", ".join(sans[:limit]) + extra


# ==================== ОТРИСОВКА ДОСКИ ====================
CHESS_BOARD_STYLE = "unicode"   # "unicode" — фигуры ♔♞, "ascii" — буквы KQRBNP; пустые клетки в обоих случаях — точки

# U+265F (чёрная пешка) в некоторых клиентах рисуется как цветной эмодзи — VS15 (U+FE0E) просит текстовый вид.
_UNICODE_PIECES = {
    (chess.PAWN, True): "♙", (chess.KNIGHT, True): "♘", (chess.BISHOP, True): "♗",
    (chess.ROOK, True): "♖", (chess.QUEEN, True): "♕", (chess.KING, True): "♔",
    (chess.PAWN, False): "♟\ufe0e", (chess.KNIGHT, False): "♞", (chess.BISHOP, False): "♝",
    (chess.ROOK, False): "♜", (chess.QUEEN, False): "♛", (chess.KING, False): "♚",
}


def render_board(board: chess.Board, user_color: bool, style: Optional[str] = None) -> str:
    style = style or CHESS_BOARD_STYLE
    ranks = range(7, -1, -1) if user_color == chess.WHITE else range(8)
    files = list(range(8)) if user_color == chess.WHITE else list(range(7, -1, -1))
    lines = []
    for r in ranks:
        row = []
        for f in files:
            p = board.piece_at(chess.square(f, r))
            if p is None:
                row.append("·")
            elif style == "unicode":
                row.append(_UNICODE_PIECES[(p.piece_type, p.color)])
            else:
                row.append(p.symbol())
        lines.append(f"{r + 1}  " + " ".join(row))
    lines.append("   " + " ".join("abcdefgh"[f] for f in files))
    return "\n".join(lines)


# ==================== СОСТОЯНИЕ ПАРТИИ ====================
class ChessGame:
    def __init__(self, user_color: bool):
        self.board = chess.Board()
        self.user_color = user_color
        self.bot_color = not user_color
        self.dialog = deque(maxlen=6)
        self.busy = False
        self.last_activity = time.time()

    def touch(self):
        self.last_activity = time.time()

    def is_expired(self) -> bool:
        return time.time() - self.last_activity > CHESS_GAME_TTL_HOURS * 3600

    def add_dialog(self, who: str, text: str):
        text = (text or "").strip()
        if text:
            self.dialog.append(f"{who}: {text[:200]}")


_TERMINATION_RU = {
    chess.Termination.CHECKMATE: "мат",
    chess.Termination.STALEMATE: "пат",
    chess.Termination.INSUFFICIENT_MATERIAL: "недостаточно материала для мата",
    chess.Termination.SEVENTYFIVE_MOVES: "правило 75 ходов",
    chess.Termination.FIVEFOLD_REPETITION: "пятикратное повторение позиции",
    chess.Termination.FIFTY_MOVES: "правило 50 ходов",
    chess.Termination.THREEFOLD_REPETITION: "троекратное повторение позиции",
}


def result_info(game: ChessGame):
    """None, если партия продолжается; иначе (kind, строка), kind: user_won | bot_won | draw."""
    outcome = game.board.outcome(claim_draw=True)
    if not outcome:
        return None
    reason = _TERMINATION_RU.get(outcome.termination, "конец партии")
    if outcome.winner is None:
        return "draw", f"Партия окончена: {reason}. Ничья."
    if outcome.winner == game.user_color:
        return "user_won", f"Партия окончена: {reason}. Победа за тобой."
    return "bot_won", f"Партия окончена: {reason}. Победила я."


# ==================== ПРОМПТЫ ====================
CHESS_SYSTEM_ADDON = """**РЕЖИМ ШАХМАТНОЙ ПАРТИИ:**
Сейчас ты играешь в шахматы с собеседником. Ходы ты сама с нуля НЕ придумываешь и позицию глубоко не анализируешь: шахматный модуль заранее готовит для тебя короткий нумерованный список кандидатных ходов. Ты выбираешь ровно один из них (по характеру: осторожно, агрессивно, красиво) и в своём характере коротко (1-3 предложения) реагируешь на ход и реплику собеседника — как живой человек за доской, можно подколоть, похвалить, смутиться.
Формат ответа:
1) текст реплики (без звёздочек и описаний действий; сам список кандидатов и их номера в тексте не упоминай);
2) если в служебной информации есть кандидаты — отдельной строкой [chess_move: N], где N — номер выбранного хода;
3) последней строкой [emotion: ключ].
Не пиши теги [no_reply], [grudge], [trust], [rp_mode] в этом режиме. Не выдумывай ходы, которых нет в списке. Если собеседник пишет не про шахматы — отвечай по-человечески, но не забывай про партию."""

CHESS_MOVE_TAG_RE = re.compile(r'\[\s*chess_move\s*:\s*(\d+)\s*\]\.?', re.IGNORECASE)


def parse_chess_move_tag(text: str):
    if not text:
        return text, None
    matches = list(CHESS_MOVE_TAG_RE.finditer(text))
    if not matches:
        return text, None
    match = matches[-1]
    try:
        idx = int(match.group(1))
    except ValueError:
        idx = None
    clean = CHESS_MOVE_TAG_RE.sub('', text).strip()
    return clean, idx


def _color_ru(color: bool) -> str:
    return "белыми" if color == chess.WHITE else "чёрными"


def _material_balance_ru(board: chess.Board, bot_color: bool) -> str:
    diff = 0
    for piece in board.piece_map().values():
        v = _PIECE_VALUES[piece.piece_type]
        diff += v if piece.color == bot_color else -v
    pawns = round(diff / 100)
    if pawns == 0:
        return "равный"
    return f"у тебя +{pawns}" if pawns > 0 else f"у собеседника +{-pawns}"


def build_prompt(game: ChessGame, situation: str, user_move_san: Optional[str] = None,
                 comment: str = "", candidates: Optional[list] = None,
                 first_name: Optional[str] = None) -> str:
    b = game.board
    name = f" ({first_name})" if first_name else ""
    lines = [
        f"[Служебная информация: идёт шахматная партия. Собеседник{name} играет "
        f"{_color_ru(game.user_color)}, ты — {_color_ru(game.bot_color)}.]"
    ]
    if b.move_stack:
        history = chess.Board().variation_san(b.move_stack)
        if len(history) > 500:
            history = "…" + history[-500:]
        lines.append(f"Ходы партии: {history}")
        lines.append(f"Материальный баланс: {_material_balance_ru(b, game.bot_color)} (в пешках).")
    if game.dialog:
        lines.append("Недавние реплики:\n" + "\n".join(game.dialog))

    if situation == 'start':
        if game.bot_color == chess.WHITE:
            lines.append(
                "Партия только начинается, ты играешь белыми и ходишь первой. Коротко поприветствуй "
                "собеседника в своём характере и сделай первый ход."
            )
        else:
            lines.append(
                "Партия только начинается, первый ход за собеседником (он белыми). Коротко и в своём "
                "характере поприветствуй его и предложи начинать. Ход выбирать не нужно."
            )
    elif situation == 'move':
        line = f"Собеседник только что сделал ход {user_move_san}."
        if comment:
            line += f" Его реплика: «{comment}»."
        if b.is_check():
            line += " Этим ходом он поставил тебе шах."
        lines.append(line)
    elif situation == 'chat':
        lines.append(
            f"Собеседник написал не ход, а: «{comment}». Ответь по существу в своём характере, а потом "
            f"напомни, что ход нужно записать (например e4 или Nf3). Ход пока не выбирай."
        )
    elif situation == 'user_won':
        lines.append(
            f"Собеседник только что сделал ход {user_move_san}, и партия закончилась его победой. "
            f"Прокомментируй итог в своём характере (проигрывать ты не любишь, но признавать поражение умеешь)."
        )
    elif situation == 'draw':
        lines.append(
            f"Собеседник только что сделал ход {user_move_san}, и партия закончилась ничьей. "
            f"Коротко прокомментируй итог."
        )

    if candidates:
        numbered = "\n".join(f"{i}. {describe_move(b, m)}" for i, m in enumerate(candidates, 1))
        lines.append(
            "Кандидатные ходы (выбери ровно один и укажи его тегом [chess_move: N]):\n" + numbered
        )
    return "\n\n".join(lines)


# ==================== PvP: ДВА УЧАСТНИКА В ГРУППЕ ====================
PVP_GAME_TTL_HOURS = 24      # брошенная партия в группе стирается через столько часов
PVP_CHALLENGE_TTL_MINUTES = 10


class PvpGame:
    """Партия двух живых участников в групповом чате (нейронка не участвует)."""

    def __init__(self, chat_id: int, white_id: int, white_name: str, black_id: int, black_name: str):
        self.chat_id = chat_id
        self.board = chess.Board()
        self.white_id, self.white_name = white_id, white_name
        self.black_id, self.black_name = black_id, black_name
        self.draw_offer_by: Optional[int] = None
        self.last_activity = time.time()

    def touch(self):
        self.last_activity = time.time()

    def is_expired(self) -> bool:
        return time.time() - self.last_activity > PVP_GAME_TTL_HOURS * 3600

    def color_of(self, user_id: int) -> Optional[bool]:
        if user_id == self.white_id:
            return chess.WHITE
        if user_id == self.black_id:
            return chess.BLACK
        return None

    def player(self, color: bool):
        """(id, имя) игрока за данный цвет."""
        return (self.white_id, self.white_name) if color == chess.WHITE else (self.black_id, self.black_name)

    def opponent_of(self, user_id: int):
        color = self.color_of(user_id)
        return None if color is None else self.player(not color)


def pvp_result(game: PvpGame):
    """None, если партия идёт; иначе строка с итогом (без имён — их подставляет вызывающий код)
    в виде (winner_color | None, причина)."""
    outcome = game.board.outcome(claim_draw=True)
    if not outcome:
        return None
    return outcome.winner, _TERMINATION_RU.get(outcome.termination, "конец партии")
