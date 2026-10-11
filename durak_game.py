"""Подкидной дурак для бота Амадеус (36 карт, партия «игрок против Амадеуса»).

Модуль не зависит от Telegram: здесь только правила, «ум» бота и текст состояния.
Кнопки и обработчики лежат в основном файле бота.

Упрощения относительно настоящих правил:
- карты подкидываются, когда все карты на столе отбиты (или когда защитник решил брать);
  выложить сразу несколько карт одного достоинства одним ходом нельзя;
- лимит подкидывания: не больше 6 карт за раунд и не больше, чем карт у защитника в начале раунда
  (в самом первом раунде игры лимит тоже 6, а не 5).
"""
import html
import random
import time
from typing import Optional

SUITS = "SHDC"
SUIT_SYMBOL = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
RANK_NAME = {6: "6", 7: "7", 8: "8", 9: "9", 10: "10", 11: "В", 12: "Д", 13: "К", 14: "Т"}

HAND_SIZE = 6
MAX_TABLE_CARDS = 6
GAME_TTL_HOURS = 12
BOT_ADD_CHANCE = 0.7            # шанс, что Амадеус подкинет карту, когда может
BOT_SPEND_TRUMP_MIN_RANK = 11   # Амадеус не тратит на мелочь козырь от этого достоинства и выше (если в колоде ещё есть карты) — берёт
BOT_KEEP_TRUMP_DECK_MIN = 3

WIN_LINES = [
    "Дураком осталась я. Статистическая погрешность — не привыкай.",
    "Ты выиграл. Признаю, неплохо. Но реванша я требую.",
    "Хм. Дурак — я. Запомни этот день, он не повторится.",
]
LOSE_LINES = [
    "Дураком остался ты. Ничего личного, просто теория вероятностей.",
    "Победа за мной. Думать на два хода вперёд всё-таки полезно.",
    "Ты проиграл. Не расстраивайся — это всего лишь карточная игра.",
]
DRAW_LINES = [
    "Ничья — карты закончились у обоих. Редкий результат.",
]


class DurakError(Exception):
    """Недопустимое действие игрока — текст показывается пользователю."""


def card_str(card) -> str:
    return f"{RANK_NAME[card[0]]}{SUIT_SYMBOL[card[1]]}"


def card_code(card) -> str:
    return f"{card[0]}{card[1]}"


def parse_card_code(code: str):
    try:
        rank, suit = int(code[:-1]), code[-1]
    except (ValueError, IndexError):
        raise DurakError("Некорректная карта.")
    if rank not in RANK_NAME or suit not in SUITS:
        raise DurakError("Некорректная карта.")
    return (rank, suit)


def _cards_ru(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} карта"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} карты"
    return f"{n} карт"


class DurakGame:
    """Игрок — «P», Амадеус — «B»."""

    def __init__(self, gid: str):
        self.gid = gid
        deck = [(r, s) for s in SUITS for r in RANK_NAME]
        random.shuffle(deck)
        self.deck = deck                 # берём с конца; deck[0] — козырная карта, её берут последней
        self.trump_card = deck[0]
        self.trump = deck[0][1]
        self.hands = {"P": [], "B": []}
        for _ in range(HAND_SIZE):
            self.hands["P"].append(self.deck.pop())
            self.hands["B"].append(self.deck.pop())
        self.discard_count = 0
        self.table = []                  # [[карта_атаки, карта_защиты | None], ...]
        self.limit = 0
        self.target = None
        self.result: Optional[str] = None  # None — идёт, "P" — выиграл игрок, "B" — выиграла Амадеус, "draw"
        self.log: list = []
        self.last_activity = time.time()

        self.attacker = self._first_attacker()
        self.phase = "attack"            # attack | defend | adding | over
        self.log.append(
            "Первой атакуешь ты (у тебя младший козырь или повезло с жребием)."
            if self.attacker == "P" else
            "Первой атакует Амадеус."
        )
        self._begin_bout()
        self._auto_resolve()

    # ---------- служебное ----------
    def touch(self):
        self.last_activity = time.time()

    def is_expired(self) -> bool:
        return time.time() - self.last_activity > GAME_TTL_HOURS * 3600

    @property
    def defender(self) -> str:
        return "B" if self.attacker == "P" else "P"

    def _first_attacker(self) -> str:
        lows = {}
        for who in ("P", "B"):
            trumps = [c[0] for c in self.hands[who] if c[1] == self.trump]
            if trumps:
                lows[who] = min(trumps)
        if len(lows) == 2:
            return min(lows, key=lows.get)
        if lows:
            return next(iter(lows))
        return random.choice(("P", "B"))

    def _strength(self, card):
        return (card[1] == self.trump, card[0])

    def sort_hand(self, hand):
        return sorted(hand, key=lambda c: (c[1] == self.trump, SUITS.index(c[1]), c[0]))

    def can_beat(self, att, dfn) -> bool:
        if dfn[1] == att[1]:
            return dfn[0] > att[0]
        return dfn[1] == self.trump

    def _undefended(self) -> list:
        return [i for i, pair in enumerate(self.table) if pair[1] is None]

    def _table_ranks(self) -> set:
        return {c[0] for pair in self.table for c in pair if c}

    def can_add(self, card) -> bool:
        if len(self.table) >= self.limit:
            return False
        return not self.table or card[0] in self._table_ranks()

    def addable_cards(self, who: str) -> list:
        return [c for c in self.hands[who] if self.can_add(c)]

    def _current_target(self) -> Optional[int]:
        und = self._undefended()
        if not und:
            return None
        return self.target if self.target in und else und[0]

    def _actor(self) -> str:
        return self.defender if self.phase == "defend" else self.attacker

    # ---------- ходы игрока ----------
    def user_attack(self, card):
        """Первая карта атаки или подкидывание."""
        self.touch()
        self.log = []
        if self.phase not in ("attack", "adding") or self.attacker != "P":
            raise DurakError("Сейчас ты не атакуешь.")
        if card not in self.hands["P"]:
            raise DurakError("У тебя нет такой карты.")
        if not self.can_add(card):
            if len(self.table) >= self.limit:
                raise DurakError("Больше подкидывать нельзя — достигнут лимит раунда.")
            raise DurakError("Подкинуть можно только карту того же достоинства, что уже лежат на столе.")
        self._play_attack("P", card)
        if self.phase == "attack":
            self.phase = "defend"
        self._auto_resolve()

    def user_defend(self, card):
        self.touch()
        self.log = []
        if self.phase != "defend" or self.defender != "P":
            raise DurakError("Сейчас ты не отбиваешься.")
        if card not in self.hands["P"]:
            raise DurakError("У тебя нет такой карты.")
        idx = self._current_target()
        att = self.table[idx][0]
        if not self.can_beat(att, card):
            raise DurakError(f"Картой {card_str(card)} нельзя побить {card_str(att)}.")
        self.hands["P"].remove(card)
        self.table[idx][1] = card
        self.target = None
        self.log.append(f"Ты бьёшь {card_str(att)} картой {card_str(card)}.")
        if not self._undefended():
            self.phase = "attack"
        self._auto_resolve()

    def set_target(self, idx: int):
        self.touch()
        self.log = []
        if self.phase != "defend" or idx not in self._undefended():
            raise DurakError("Эту карту уже нельзя выбрать.")
        self.target = idx

    def user_take(self):
        self.touch()
        self.log = []
        if self.phase != "defend" or self.defender != "P":
            raise DurakError("Сейчас нечего брать.")
        self.log.append("Ты берёшь карты.")
        self.phase = "adding"
        self._auto_resolve()

    def user_bito(self):
        self.touch()
        self.log = []
        if self.phase != "attack" or self.attacker != "P" or not self.table or self._undefended():
            raise DurakError("Сейчас «бито» сказать нельзя.")
        self._bito()
        self._auto_resolve()

    def user_done_adding(self):
        self.touch()
        self.log = []
        if self.phase != "adding" or self.attacker != "P":
            raise DurakError("Сейчас нечего завершать.")
        self._finish_take()
        self._auto_resolve()

    # ---------- общие действия ----------
    def _begin_bout(self):
        self.table = []
        self.target = None
        self.limit = min(MAX_TABLE_CARDS, len(self.hands[self.defender]))
        self.phase = "attack"

    def _play_attack(self, who: str, card):
        self.hands[who].remove(card)
        self.table.append([card, None])
        name = "Ты подкидываешь" if who == "P" and len(self.table) > 1 else \
               "Ты ходишь" if who == "P" else \
               "Амадеус подкидывает" if len(self.table) > 1 else "Амадеус ходит"
        self.log.append(f"{name}: {card_str(card)}.")

    def _refill(self, who: str):
        while len(self.hands[who]) < HAND_SIZE and self.deck:
            self.hands[who].append(self.deck.pop())

    def _bito(self):
        self.log.append("Бито — карты уходят в отбой.")
        self.discard_count += sum(1 for pair in self.table for c in pair if c)
        old_attacker = self.attacker
        self.table = []
        self._refill(old_attacker)
        self._refill(self.defender)
        self.attacker = self.defender
        self._end_or_next()

    def _finish_take(self):
        taker = self.defender
        for pair in self.table:
            for c in pair:
                if c:
                    self.hands[taker].append(c)
        self.log.append(f"{'Ты забираешь' if taker == 'P' else 'Амадеус забирает'} карты со стола.")
        self.table = []
        self._refill(self.attacker)
        self._refill(taker)
        self._end_or_next()

    def _end_or_next(self):
        if not self.deck:
            p, b = len(self.hands["P"]), len(self.hands["B"])
            if p == 0 or b == 0:
                self.result = "draw" if p == 0 and b == 0 else ("P" if p == 0 else "B")
                self.phase = "over"
                self.table = []
                return
        self._begin_bout()

    # ---------- автоматика: ходы Амадеус и «вынужденные» ходы игрока ----------
    def _auto_resolve(self):
        guard = 0
        while self.phase != "over" and guard < 300:
            guard += 1
            if self._actor() == "P":
                if self.phase == "attack" and self.table and not self.addable_cards("P"):
                    self._bito()
                    continue
                if self.phase == "adding" and not self.addable_cards("P"):
                    self._finish_take()
                    continue
                return
            self._bot_act()

    def _bot_act(self):
        hand = self.hands["B"]
        trump = self.trump
        cheap = lambda c: (c[1] == trump, c[0])

        if self.phase == "attack":
            if not self.table:
                self._play_attack("B", min(hand, key=cheap))
                self.phase = "defend"
                return
            cands = [c for c in self.addable_cards("B") if c[1] != trump]
            if cands and random.random() < BOT_ADD_CHANCE:
                self._play_attack("B", min(cands, key=cheap))
                self.phase = "defend"
            else:
                self._bito()
            return

        if self.phase == "defend":
            self._bot_defend()
            return

        if self.phase == "adding":   # игрок взял — Амадеус докидывает мелочь
            while True:
                cands = [c for c in self.addable_cards("B") if c[1] != trump]
                if not cands:
                    break
                self._play_attack("B", min(cands, key=cheap))
            self._finish_take()

    def _bot_defend(self):
        trump = self.trump
        cheap = lambda c: (c[1] == trump, c[0])
        avail = list(self.hands["B"])
        plan = {}
        for i in sorted(self._undefended(), key=lambda i: self._strength(self.table[i][0]), reverse=True):
            att = self.table[i][0]
            opts = [c for c in avail if self.can_beat(att, c)]
            if not opts:
                plan = None
                break
            best = min(opts, key=cheap)
            plan[i] = best
            avail.remove(best)

        take = plan is None
        if not take and len(self.deck) > BOT_KEEP_TRUMP_DECK_MIN:
            for i, c in plan.items():
                att = self.table[i][0]
                if c[1] == trump and att[1] != trump and c[0] >= BOT_SPEND_TRUMP_MIN_RANK:
                    take = True
                    break

        if take:
            self.log.append("Амадеус не отбивается и берёт карты.")
            self.phase = "adding"
            return
        for i in sorted(plan):
            att, c = self.table[i][0], plan[i]
            self.hands["B"].remove(c)
            self.table[i][1] = c
            self.log.append(f"Амадеус бьёт {card_str(att)} картой {card_str(c)}.")
        self.phase = "attack"

    # ---------- что показывать игроку ----------
    def available_actions(self):
        """(карты, цели, прочие кнопки) — каждая кнопка это (подпись, действие)."""
        cards, targets, extras = [], [], []
        if self.phase == "over":
            return cards, targets, extras
        hand = self.sort_hand(self.hands["P"])
        if self.phase == "defend" and self.defender == "P":
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
        elif self.phase in ("attack", "adding") and self.attacker == "P":
            for c in hand:
                if self.can_add(c):
                    cards.append((card_str(c), "c" + card_code(c)))
            if self.phase == "attack" and self.table:
                extras.append(("✅ Бито", "bito"))
            elif self.phase == "adding":
                extras.append(("✅ Хватит", "done"))
        return cards, targets, extras

    def render_text(self) -> str:
        e = html.escape
        lines = ["<b>Дурак</b> (подкидной) — ты против Амадеуса"]
        trump_line = f"Козырь: <b>{SUIT_SYMBOL[self.trump]}</b>"
        if self.deck:
            trump_line += f" (нижняя карта колоды: {card_str(self.trump_card)})"
        trump_line += f" · Колода: {len(self.deck)} · Отбой: {self.discard_count}"
        lines.append(trump_line)
        lines.append("")

        if self.table:
            lines.append("Стол:")
            for att, dfn in self.table:
                lines.append(f"   {card_str(att)}  ←  {card_str(dfn) if dfn else '?'}")
        elif self.phase != "over":
            lines.append("Стол пуст.")
        if self.phase != "over":
            lines.append("")
        lines.append(f"У Амадеус: {_cards_ru(len(self.hands['B']))}")
        lines.append("Твои карты: " + (" ".join(card_str(c) for c in self.sort_hand(self.hands["P"])) or "—"))

        if self.log:
            lines.append("")
            lines.extend(e(x) for x in self.log)

        lines.append("")
        if self.phase == "over":
            if self.result == "P":
                lines.append("<b>Ты избавился от карт — ты выиграл!</b>")
                lines.append(e(random.choice(WIN_LINES)))
            elif self.result == "B":
                lines.append("<b>Ты остался с картами — ты дурак.</b>")
                lines.append(e(random.choice(LOSE_LINES)))
            else:
                lines.append("<b>Ничья.</b>")
                lines.append(e(random.choice(DRAW_LINES)))
            lines.append("Реванш: /durak")
        elif self.phase == "defend":
            cards, _, _ = self.available_actions()
            lines.append("<b>Твой ход:</b> отбивай кнопкой с картой или бери." if cards
                         else "<b>Побить нечем — придётся брать.</b>")
        elif self.phase == "adding":
            lines.append("<b>Амадеус берёт.</b> Можешь подкинуть ещё карты того же достоинства или нажать «Хватит».")
        elif self.table:
            lines.append("<b>Всё отбито.</b> Подкинь карту того же достоинства или нажми «Бито».")
        else:
            lines.append("<b>Твоя атака:</b> выбери карту, которой пойдёшь.")
        return "\n".join(lines)
