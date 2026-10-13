"""Подкидной дурак для группы: 2-4 живых игрока (без бота).

Модуль не зависит от Telegram: здесь правила и тексты. Карты каждого игрока приватные, поэтому
в основном файле бота рука и кнопки хода показываются игроку в личных сообщениях, а в группе
лежит только общее состояние стола.

Упрощения относительно настоящих правил (те же, что в партии с Амадеусом):
- подкидывать можно, когда всё на столе отбито (или защитник решил брать), а не пока он думает;
- первую карту кладёт только атакующий, остальные подкидывают после неё;
- за раунд на стол ложится не больше 6 карт и не больше, чем карт у защитника в начале раунда.
"""
import html
import random
import time
from typing import Optional

from durak_game import (
    SUITS, SUIT_SYMBOL, RANK_NAME, HAND_SIZE, MAX_TABLE_CARDS, DurakError,
    card_str, card_code, parse_card_code, _cards_ru,
)

MIN_PLAYERS = 2
MAX_PLAYERS = 4
LOBBY_TTL_MINUTES = 15          # лобби, из которого так и не стартовали, стирается
GAME_IDLE_TTL_MINUTES = 60      # партия без единого хода стирается


class DurakTable:
    def __init__(self, gid: str, chat_id: int, host_id: int, host_name: str):
        self.gid = gid
        self.chat_id = chat_id
        self.host_id = host_id
        self.state = "lobby"            # lobby | play | over
        self.players = [host_id]        # порядок мест за столом (после старта перемешивается)
        self.names = {host_id: host_name}
        self.message_id: Optional[int] = None   # общее сообщение в группе
        self.dm_ids: dict = {}                  # user_id -> id личного сообщения с рукой
        self.dm_kinds: dict = {}                # user_id -> "photo" | "text": чем сейчас является личное сообщение
        self.public_kind: str = "text"          # общее сообщение в группе: лобби — текст, во время партии — фото стола
        self.created = time.time()
        self.last_activity = time.time()
        self.log: list = []
        self.stopped_by: Optional[str] = None

        # заполняются в start()
        self.deck: list = []
        self.trump_card = None
        self.trump = None
        self.hands: dict = {}
        self.discard_count = 0
        self.table: list = []
        self.out: set = set()
        self.finish_order: list = []
        self.attacker = None
        self.defender = None
        self.phase = "lobby"            # attack | defend | throw | adding | over
        self.limit = 0
        self.target = None
        self.passed: set = set()
        self.fool = None

    # ---------- служебное ----------
    def touch(self):
        self.last_activity = time.time()

    def is_expired(self) -> bool:
        if self.state == "lobby":
            return time.time() - self.created > LOBBY_TTL_MINUTES * 60
        return time.time() - self.last_activity > GAME_IDLE_TTL_MINUTES * 60

    def name(self, uid) -> str:
        return self.names.get(uid, "игрок")

    # ---------- лобби ----------
    def add_player(self, uid: int, name: str):
        if self.state != "lobby":
            raise DurakError("Партия уже началась.")
        if uid in self.players:
            raise DurakError("Ты уже в списке игроков.")
        if len(self.players) >= MAX_PLAYERS:
            raise DurakError(f"Мест нет — максимум {MAX_PLAYERS} игрока.")
        self.players.append(uid)
        self.names[uid] = name
        self.touch()

    def remove_player(self, uid: int):
        if self.state != "lobby" or uid not in self.players:
            raise DurakError("Тебя нет в списке игроков.")
        if uid == self.host_id:
            raise DurakError("Организатор не может выйти — только отменить лобби.")
        self.players.remove(uid)
        self.names.pop(uid, None)
        self.touch()

    def start(self):
        if self.state != "lobby":
            raise DurakError("Партия уже началась.")
        if len(self.players) < MIN_PLAYERS:
            raise DurakError(f"Нужно минимум {MIN_PLAYERS} игрока.")
        random.shuffle(self.players)
        deck = [(r, s) for s in SUITS for r in RANK_NAME]
        random.shuffle(deck)
        self.deck = deck                 # берём с конца; deck[0] — козырная карта, её берут последней
        self.trump_card = deck[0]
        self.trump = deck[0][1]
        self.hands = {p: [] for p in self.players}
        for _ in range(HAND_SIZE):
            for p in self.players:
                self.hands[p].append(self.deck.pop())
        self.discard_count = 0
        self.out = set()
        self.finish_order = []
        self.state = "play"
        self.touch()

        lows = {}
        for p in self.players:
            trumps = [c[0] for c in self.hands[p] if c[1] == self.trump]
            if trumps:
                lows[p] = min(trumps)
        self.attacker = min(lows, key=lows.get) if lows else random.choice(self.players)
        self.defender = self._next_active_after(self.attacker)
        self.log = [
            f"Первым атакует {self.name(self.attacker)}"
            + (" — у этого игрока младший козырь." if lows else " — так выпал жребий.")
        ]
        self._begin_bout()

    # ---------- вспомогательные ----------
    def active(self) -> list:
        return [p for p in self.players if p not in self.out]

    def _idx(self, uid) -> int:
        return self.players.index(uid)

    def _cyc_from(self, uid) -> list:
        i = self._idx(uid)
        return self.players[i:] + self.players[:i]

    def _next_active_after(self, uid):
        for p in self._cyc_from(uid)[1:]:
            if p not in self.out:
                return p
        return uid

    def _seat_after(self, uid):
        return self.players[(self._idx(uid) + 1) % len(self.players)]

    def _first_active_from(self, uid):
        for p in self._cyc_from(uid):
            if p not in self.out:
                return p
        return None

    def can_beat(self, att, dfn) -> bool:
        if dfn[1] == att[1]:
            return dfn[0] > att[0]
        return dfn[1] == self.trump

    def sort_hand(self, hand):
        return sorted(hand, key=lambda c: (c[1] == self.trump, SUITS.index(c[1]), c[0]))

    def _undefended(self) -> list:
        return [i for i, pair in enumerate(self.table) if pair[1] is None]

    def _table_ranks(self) -> set:
        return {c[0] for pair in self.table for c in pair if c}

    def can_add(self, card) -> bool:
        if len(self.table) >= self.limit:
            return False
        return not self.table or card[0] in self._table_ranks()

    def addable(self, uid) -> list:
        return [c for c in self.hands.get(uid, []) if self.can_add(c)]

    def _current_target(self) -> Optional[int]:
        und = self._undefended()
        if not und:
            return None
        return self.target if self.target in und else und[0]

    def pending_throwers(self) -> list:
        """Кто ещё может подкинуть и не спасовал."""
        if self.phase not in ("throw", "adding"):
            return []
        return [p for p in self.active()
                if p != self.defender and p not in self.passed and self.addable(p)]

    # ---------- ходы игроков ----------
    def attack(self, uid: int, card):
        """Первая карта раунда (атакующий) или подкидывание (любой, кроме защитника)."""
        self.log = []
        self._require_play()
        if self.phase == "attack":
            if uid != self.attacker:
                raise DurakError(f"Первую карту кладёт {self.name(self.attacker)}.")
        elif self.phase in ("throw", "adding"):
            if uid == self.defender:
                raise DurakError("Защитник не подкидывает.")
            if uid in self.out:
                raise DurakError("Ты уже вышел из игры.")
        else:
            raise DurakError("Сейчас подкидывать нельзя.")
        if card not in self.hands[uid]:
            raise DurakError("У тебя нет такой карты.")
        if not self.can_add(card):
            if len(self.table) >= self.limit:
                raise DurakError("Больше подкидывать нельзя — достигнут лимит раунда.")
            raise DurakError("Подкинуть можно только карту того же достоинства, что уже лежат на столе.")

        self.hands[uid].remove(card)
        self.table.append([card, None])
        verb = "подкидывает" if len(self.table) > 1 else "ходит"
        self.log.append(f"{self.name(uid)} {verb}: {card_str(card)}.")
        self.touch()
        if self.phase in ("attack", "throw"):
            self.phase = "defend"
        self.passed.clear()
        self._check_resolve()

    def defend(self, uid: int, card):
        self.log = []
        self._require_play()
        if self.phase != "defend" or uid != self.defender:
            raise DurakError("Сейчас ты не отбиваешься.")
        if card not in self.hands[uid]:
            raise DurakError("У тебя нет такой карты.")
        idx = self._current_target()
        att = self.table[idx][0]
        if not self.can_beat(att, card):
            raise DurakError(f"Картой {card_str(card)} нельзя побить {card_str(att)}.")
        self.hands[uid].remove(card)
        self.table[idx][1] = card
        self.target = None
        self.log.append(f"{self.name(uid)} бьёт {card_str(att)} картой {card_str(card)}.")
        self.touch()
        if not self._undefended():
            self.phase = "throw"
            self.passed.clear()
        self._check_resolve()

    def set_target(self, uid: int, idx: int):
        self.log = []
        self._require_play()
        if self.phase != "defend" or uid != self.defender or idx not in self._undefended():
            raise DurakError("Эту карту уже нельзя выбрать.")
        self.target = idx
        self.touch()

    def take(self, uid: int):
        self.log = []
        self._require_play()
        if self.phase != "defend" or uid != self.defender:
            raise DurakError("Сейчас ты не отбиваешься.")
        self.log.append(f"{self.name(uid)} берёт карты.")
        self.phase = "adding"
        self.passed.clear()
        self.touch()
        self._check_resolve()

    def pass_(self, uid: int):
        self.log = []
        self._require_play()
        if self.phase not in ("throw", "adding") or uid == self.defender or uid in self.out:
            raise DurakError("Сейчас пасовать не нужно.")
        self.passed.add(uid)
        self.touch()
        self._check_resolve()

    def _require_play(self):
        if self.state != "play":
            raise DurakError("Партия уже закончена.")

    # ---------- завершение раунда ----------
    def _check_resolve(self):
        if self.phase in ("throw", "adding") and not self.pending_throwers():
            if self.phase == "throw":
                self._bito()
            else:
                self._finish_take()

    def _begin_bout(self):
        self.table = []
        self.target = None
        self.passed = set()
        self.limit = min(MAX_TABLE_CARDS, len(self.hands[self.defender]))
        self.phase = "attack"

    def _refill_all(self):
        order = [p for p in self._cyc_from(self.attacker) if p not in self.out and p != self.defender]
        order.append(self.defender)
        for p in order:
            while len(self.hands[p]) < HAND_SIZE and self.deck:
                self.hands[p].append(self.deck.pop())

    def _bito(self):
        self.log.append("Бито — карты уходят в отбой.")
        self.discard_count += sum(1 for pair in self.table for c in pair if c)
        self.table = []
        self._refill_all()
        self._next_round(self.defender)

    def _finish_take(self):
        taker = self.defender
        for pair in self.table:
            for c in pair:
                if c:
                    self.hands[taker].append(c)
        self.log.append(f"{self.name(taker)} забирает карты со стола.")
        self.table = []
        self._refill_all()
        self._next_round(self._seat_after(taker))

    def _next_round(self, start):
        if not self.deck:
            for p in self.players:
                if p not in self.out and not self.hands[p]:
                    self.out.add(p)
                    self.finish_order.append(p)
                    self.log.append(f"{self.name(p)}: карт больше нет — выходит из игры.")
        act = self.active()
        if len(act) <= 1:
            self.state = "over"
            self.phase = "over"
            self.table = []
            self.fool = act[0] if act else None
            return
        self.attacker = self._first_active_from(start)
        self.defender = self._next_active_after(self.attacker)
        self._begin_bout()
        self.log.append(f"Атакует {self.name(self.attacker)}, защищается {self.name(self.defender)}.")

    # ---------- что показывать ----------
    def available_actions(self, uid: int):
        """(карты, цели, прочие кнопки) для личного сообщения игрока — подписи и действия."""
        cards, targets, extras = [], [], []
        if self.state != "play" or uid in self.out or uid not in self.hands:
            return cards, targets, extras
        hand = self.sort_hand(self.hands[uid])
        if self.phase == "defend" and uid == self.defender:
            idx = self._current_target()
            att = self.table[idx][0]
            for c in hand:
                if self.can_beat(att, c):
                    cards.append((card_str(c), "c" + card_code(c)))
            und = self._undefended()
            if len(und) > 1:
                for i in und:
                    label = ("🎯 " if i == idx else "") + card_str(self.table[i][0])
                    targets.append((label, f"g{i}"))
            extras.append(("🤲 Беру", "take"))
        elif self.phase == "attack" and uid == self.attacker:
            for c in hand:
                if self.can_add(c):
                    cards.append((card_str(c), "c" + card_code(c)))
        elif self.phase in ("throw", "adding") and uid != self.defender and uid not in self.passed:
            for c in hand:
                if self.can_add(c):
                    cards.append((card_str(c), "c" + card_code(c)))
            if cards:
                extras.append(("✅ Пас", "pass"))
        return cards, targets, extras

    def hand_text(self, uid: int) -> str:
        """Текст для всплывающего окна кнопки «Мои карты» (до 200 символов)."""
        if uid not in self.hands:
            return "Ты не участвуешь в этой партии."
        if uid in self.out:
            return "Ты уже избавился от карт — ты вышел из игры."
        cards = " ".join(card_str(c) for c in self.sort_hand(self.hands[uid])) or "—"
        return f"Козырь {SUIT_SYMBOL[self.trump]}\nТвои карты: {cards}"[:200]

    def _status_lines(self) -> list:
        e = html.escape
        lines = []
        if self.phase == "attack":
            lines.append(f"Ходит <b>{e(self.name(self.attacker))}</b> — кладёт первую карту.")
        elif self.phase == "defend":
            lines.append(f"<b>{e(self.name(self.defender))}</b> отбивается.")
        elif self.phase in ("throw", "adding"):
            who = ", ".join(e(self.name(p)) for p in self.pending_throwers())
            head = "Всё отбито." if self.phase == "throw" else f"{e(self.name(self.defender))} берёт."
            lines.append(f"{head} Подкидывают или пасуют: <b>{who}</b>.")
        return lines

    def _table_lines(self) -> list:
        if not self.table:
            return ["Стол пуст."] if self.phase != "over" else []
        out = ["Стол:"]
        for att, dfn in self.table:
            out.append(f"   {card_str(att)}  ←  {card_str(dfn) if dfn else '?'}")
        return out

    def _trump_line(self) -> str:
        line = f"Козырь: <b>{SUIT_SYMBOL[self.trump]}</b>"
        if self.deck:
            line += f" (нижняя карта колоды: {card_str(self.trump_card)})"
        return line + f" · Колода: {len(self.deck)} · Отбой: {self.discard_count}"

    def _result_lines(self) -> list:
        e = html.escape
        lines = []
        if self.stopped_by:
            lines.append(f"<b>Партия остановлена: {e(self.stopped_by)}.</b>")
            return lines
        if self.finish_order:
            lines.append("Вышли из игры: " + ", ".join(
                f"{i}. {e(self.name(p))}" for i, p in enumerate(self.finish_order, 1)))
        if self.fool is None:
            lines.append("<b>Ничья — карты закончились у всех.</b>")
        else:
            lines.append(f"<b>Дурак — {e(self.name(self.fool))}!</b>")
        return lines

    def render_lobby(self) -> str:
        e = html.escape
        lines = [
            "<b>Дурак</b> (подкидной) — набор игроков",
            f"Играют от {MIN_PLAYERS} до {MAX_PLAYERS} человек. Организатор: <b>{e(self.name(self.host_id))}</b>.",
            "",
            "Игроки:",
        ]
        for i, p in enumerate(self.players, 1):
            lines.append(f"  {i}. {e(self.name(p))}")
        lines.append("")
        lines.append("Карты у каждого свои, поэтому бот присылает руку и кнопки хода <b>в личные сообщения</b>. "
                     "Если ещё не писал боту в личку — сначала отправь ему /start.")
        lines.append(f"Организатор жмёт «Начать», когда соберётся хотя бы {MIN_PLAYERS} игрока.")
        return "\n".join(lines)

    def render_public(self) -> str:
        e = html.escape
        if self.stopped_by and self.trump is None:
            return f"<b>Лобби закрыто: {e(self.stopped_by)}.</b>"
        if self.state == "lobby":
            return self.render_lobby()
        lines = [f"<b>Дурак</b> (подкидной) — игроков: {len(self.players)}", self._trump_line(), ""]
        lines.append("За столом:")
        for p in self.players:
            n = e(self.name(p))
            if p in self.out:
                lines.append(f"  ✔ {n} — вышел")
                continue
            tag = " ⚔️ атакует" if p == self.attacker and self.state == "play" else \
                  " 🛡 защищается" if p == self.defender and self.state == "play" else ""
            lines.append(f"  • {n} — {_cards_ru(len(self.hands[p]))}{tag}")
        lines.append("")
        lines.extend(self._table_lines())
        if self.log:
            lines.append("")
            lines.extend(e(x) for x in self.log)
        lines.append("")
        if self.state == "over":
            lines.extend(self._result_lines())
            lines.append("Новая партия: /durak_group")
        else:
            lines.extend(self._status_lines())
            lines.append("Свои карты и кнопки хода — в личных сообщениях от бота.")
        return "\n".join(lines)

    def render_private(self, uid: int) -> str:
        e = html.escape
        lines = ["<b>Дурак</b> (подкидной) — твоя рука", self._trump_line(), ""]
        lines.extend(self._table_lines())
        lines.append("")
        lines.append("Твои карты: " + (" ".join(card_str(c) for c in self.sort_hand(self.hands.get(uid, []))) or "—"))
        if self.log:
            lines.append("")
            lines.extend(e(x) for x in self.log)
        lines.append("")
        if self.state == "over":
            lines.extend(self._result_lines())
            return "\n".join(lines)
        if uid in self.out:
            lines.append("Ты вышел из игры — наблюдай за остальными.")
            return "\n".join(lines)

        cards, _, extras = self.available_actions(uid)
        if self.phase == "defend" and uid == self.defender:
            lines.append("<b>Твой ход:</b> отбивай картой или бери." if cards
                         else "<b>Побить нечем — придётся брать.</b>")
        elif self.phase == "attack" and uid == self.attacker:
            lines.append("<b>Твоя атака:</b> выбери карту, которой пойдёшь.")
        elif self.phase in ("throw", "adding") and uid != self.defender:
            if uid in self.passed:
                lines.append("Ты спасовал. Ждём остальных.")
            elif cards:
                lines.append("<b>Можешь подкинуть</b> карту того же достоинства, что на столе, или спасовать.")
            else:
                lines.append("Подкинуть нечего. Ждём остальных.")
        else:
            lines.extend(self._status_lines())
        return "\n".join(lines)
    # ---------- картинка стола и подписи (саму картинку рисует durak_img.py) ----------
    def _role(self, p) -> Optional[str]:
        if self.state != "play" or p in self.out:
            return None
        return "attack" if p == self.attacker else "defend" if p == self.defender else None

    def _short(self, name: str, limit: int = 14) -> str:
        return name if len(name) <= limit else name[:limit - 1] + "…"

    def _public_short_status(self) -> Optional[str]:
        if self.stopped_by:
            return "Партия остановлена"
        if self.state == "over":
            return "Ничья" if self.fool is None else f"Дурак — {self._short(self.name(self.fool))}"
        if self.phase == "attack":
            return f"Ходит {self._short(self.name(self.attacker))}"
        if self.phase == "defend":
            return f"{self._short(self.name(self.defender))} отбивается"
        if self.phase == "throw":
            return "Всё отбито — подкидывают или пасуют"
        if self.phase == "adding":
            return f"{self._short(self.name(self.defender))} берёт — подкидывают"
        return None

    def _private_status(self, uid: int):
        """(строки подсказки в HTML для подписи, короткая надпись для картинки)."""
        if self.state == "over":
            return self._result_lines(), self._public_short_status()
        if uid in self.out:
            return ["Ты вышел из игры — наблюдай за остальными."], "Ты вышел из игры"
        cards, _, _ = self.available_actions(uid)
        if self.phase == "defend" and uid == self.defender:
            if cards:
                return ["<b>Твой ход:</b> отбивай картой или бери."], "Отбивайся или бери"
            return ["<b>Побить нечем — придётся брать.</b>"], "Побить нечем — бери"
        if self.phase == "attack" and uid == self.attacker:
            return ["<b>Твоя атака:</b> выбери карту, которой пойдёшь."], "Твоя атака"
        if self.phase in ("throw", "adding") and uid != self.defender:
            if uid in self.passed:
                return ["Ты спасовал. Ждём остальных."], "Ты спасовал"
            if cards:
                return (["<b>Можешь подкинуть</b> карту того же достоинства, что на столе, или спасовать."],
                        "Подкидывай или пасуй")
            return ["Подкинуть нечего. Ждём остальных."], "Подкинуть нечего"
        return self._status_lines(), self._public_short_status()

    def img_state(self, uid: Optional[int] = None) -> dict:
        """Данные для durak_img.render_table_png. uid=None — общая картинка для группы (без руки);
        uid=игрок — личная: его рука (с подсветкой допустимых карт) и соперники."""
        private = uid is not None and uid in self.hands
        opponents = [
            {"name": self.name(p), "count": len(self.hands.get(p, [])), "out": p in self.out, "role": self._role(p)}
            for p in self.players if not (private and p == uid)
        ]
        playable = None
        target_idx = None
        if private and self.state == "play" and uid not in self.out:
            cards, targets, extras = self.available_actions(uid)
            if cards or extras:
                playable = {parse_card_code(code[1:]) for _, code in cards}
            if targets and self.phase == "defend" and uid == self.defender:
                target_idx = self._current_target()
        status = self._private_status(uid)[1] if private else self._public_short_status()
        return dict(
            trump=self.trump,
            trump_card=self.trump_card if self.deck else None,
            deck_count=len(self.deck),
            discard_count=self.discard_count,
            table=[list(p) for p in self.table],
            hand=self.sort_hand(self.hands[uid]) if private else None,
            opponents=opponents,
            playable=playable,
            target_idx=target_idx,
            status=status,
        )

    def _caption(self, head: str, lines: list, tail: list) -> str:
        e = html.escape
        log = [e(x) for x in self.log]
        while True:
            parts = [head]
            if log:
                parts.append("\n".join(log))
            parts.extend(tail)
            caption = "\n\n".join(parts)
            if len(caption) <= 950:
                return caption
            if not log:
                return caption[:950]
            log = log[-3:] if len(log) > 3 else log[1:]

    def caption_public(self) -> str:
        """Подпись под общей картинкой в группе."""
        e = html.escape
        if self.stopped_by and self.trump is None:
            return f"<b>Лобби закрыто: {e(self.stopped_by)}.</b>"
        head = f"<b>Дурак</b> (подкидной) · игроков: {len(self.players)} · козырь <b>{SUIT_SYMBOL[self.trump]}</b>"
        if self.state == "over":
            return self._caption(head, [], ["\n".join(self._result_lines()), "Новая партия: /durak_group"])
        return self._caption(head, [], ["\n".join(self._status_lines()),
                                        "Свои карты и кнопки хода — в личных сообщениях от бота."])

    def caption_private(self, uid: int) -> str:
        """Подпись под личной картинкой игрока (рука и кнопки хода — под ней)."""
        head = f"<b>Дурак</b> (подкидной) — твоя рука · козырь <b>{SUIT_SYMBOL[self.trump]}</b>"
        lines = self._private_status(uid)[0]
        return self._caption(head, [], ["\n".join(lines)])
