"""Подкидной дурак для бота Амадеус (36 карт, партия «игрок против Амадеуса»).

Модуль не зависит от Telegram и Pillow: здесь правила, «ум» бота, тексты и данные для картинки стола
(картинку рисует durak_img.py). Кнопки и обработчики лежат в основном файле бота.

Как в шахматах, ходы Амадеус выбирает не нейронка «с нуля»: движок готовит 1-3 осмысленных варианта
(bot_decision), нейронка выбирает один из них и комментирует партию в характере (build_prompt +
parse_durak_move_tag). Если нейронка недоступна или промолчала — ходит первый (лучший) вариант движка,
и партия идёт ровно так же, как раньше.

Упрощения относительно настоящих правил:
- карты подкидываются, когда все карты на столе отбиты (или когда защитник решил брать);
  выложить сразу несколько карт одного достоинства одним ходом нельзя;
- лимит подкидывания: не больше 6 карт за раунд и не больше, чем карт у защитника в начале раунда
  (в самом первом раунде игры лимит тоже 6, а не 5).
"""
import html
import random
import re
import time
from collections import Counter, deque
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
BOT_ATTACK_CANDIDATES = 3       # сколько вариантов первого хода отдаём нейронке
CAPTION_LIMIT = 950             # подпись к фото в Telegram — до 1024 символов, берём с запасом

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
    """Игрок — «P», Амадеус — «B».

    manual_bot=False — как раньше: после каждого действия игрока Амадеус сама доигрывает свои ходы.
    manual_bot=True — игра останавливается перед ходом Амадеус: вызывающий код (в боте он асинхронный)
    берёт bot_decision(), даёт нейронке выбрать вариант, применяет bot_apply() и вызывает advance()."""

    def __init__(self, gid: str, manual_bot: bool = False):
        self.gid = gid
        self.manual_bot = manual_bot
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
        self.log: list = []              # что произошло с последнего действия — для игрока («Ты бьёшь…»)
        self.events: list = []           # то же самое, но в третьем лице — для нейронки («Собеседник бьёт…»)
        self.dialog = deque(maxlen=6)    # недавние реплики (как в шахматах)
        self.busy = False                # пока Амадеус думает, новые нажатия кнопок игнорируются
        self.say_mode = False            # True — кнопки карт вставляют ход в поле ввода (чтобы дописать реплику)
        self.state_msg_id: Optional[int] = None  # сообщение со столом, на котором сейчас висят кнопки
        self.last_activity = time.time()

        self.attacker = self._first_attacker()
        self.phase = "attack"            # attack | defend | adding | over
        if self.attacker == "P":
            self._note("Первой атакуешь ты (у тебя младший козырь или повезло с жребием).",
                       "Первым атакует собеседник (у него младший козырь или так выпал жребий).")
        else:
            self._note("Первой атакует Амадеус.")
        self._begin_bout()
        self.advance()

    # ---------- служебное ----------
    def touch(self):
        self.last_activity = time.time()

    def is_expired(self) -> bool:
        return time.time() - self.last_activity > GAME_TTL_HOURS * 3600

    def add_dialog(self, who: str, text: str):
        text = (text or "").strip()
        if text:
            self.dialog.append(f"{who}: {text[:200]}")

    def _reset_log(self):
        self.log = []
        self.events = []

    def _note(self, text: str, event: Optional[str] = None):
        self.log.append(text)
        self.events.append(event or text)

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

    def _cheap(self, card):
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
        self._reset_log()
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
        self.advance()

    def user_defend(self, card):
        self.touch()
        self._reset_log()
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
        self._note(f"Ты бьёшь {card_str(att)} картой {card_str(card)}.",
                   f"Собеседник бьёт {card_str(att)} картой {card_str(card)}.")
        if not self._undefended():
            self.phase = "attack"
        self.advance()

    def set_target(self, idx: int):
        self.touch()
        self._reset_log()
        if self.phase != "defend" or idx not in self._undefended():
            raise DurakError("Эту карту уже нельзя выбрать.")
        self.target = idx

    def user_take(self):
        self.touch()
        self._reset_log()
        if self.phase != "defend" or self.defender != "P":
            raise DurakError("Сейчас нечего брать.")
        self._note("Ты берёшь карты.", "Собеседник не смог (или не захотел) отбиться и берёт карты.")
        self.phase = "adding"
        self.advance()

    def user_bito(self):
        self.touch()
        self._reset_log()
        if self.phase != "attack" or self.attacker != "P" or not self.table or self._undefended():
            raise DurakError("Сейчас «бито» сказать нельзя.")
        self._bito()
        self.advance()

    def user_done_adding(self):
        self.touch()
        self._reset_log()
        if self.phase != "adding" or self.attacker != "P":
            raise DurakError("Сейчас нечего завершать.")
        self._finish_take()
        self.advance()

    # ---------- общие действия ----------
    def _begin_bout(self):
        self.table = []
        self.target = None
        self.limit = min(MAX_TABLE_CARDS, len(self.hands[self.defender]))
        self.phase = "attack"

    def _play_attack(self, who: str, card):
        self.hands[who].remove(card)
        self.table.append([card, None])
        more = len(self.table) > 1
        if who == "P":
            self._note(f"{'Ты подкидываешь' if more else 'Ты ходишь'}: {card_str(card)}.",
                       f"{'Собеседник подкидывает' if more else 'Собеседник ходит'}: {card_str(card)}.")
        else:
            self._note(f"{'Амадеус подкидывает' if more else 'Амадеус ходит'}: {card_str(card)}.")

    def _refill(self, who: str):
        while len(self.hands[who]) < HAND_SIZE and self.deck:
            self.hands[who].append(self.deck.pop())

    def _bito(self):
        self._note("Бито — карты уходят в отбой.")
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
        if taker == "P":
            self._note("Ты забираешь карты со стола.", "Собеседник забирает карты со стола.")
        else:
            self._note("Амадеус забирает карты со стола.")
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

    # ---------- автоматика: «вынужденные» ходы игрока и (если не manual_bot) ходы Амадеус ----------
    def advance(self):
        """Доводит партию до момента, когда нужно действие игрока — или, при manual_bot, решение Амадеус."""
        self._auto_resolve(until_bot=self.manual_bot)

    def _auto_resolve(self, until_bot: bool = False):
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
            if until_bot:
                return
            decision = self.bot_decision()
            self.bot_apply(decision["options"][0]["act"])

    # ---------- «ум» Амадеус: варианты хода ----------
    def bot_decision(self) -> Optional[dict]:
        """Что Амадеус может сделать прямо сейчас. None — сейчас ход не её (или партия окончена).
        {"kind": attack|throw|defend|adding, "options": [{"act": (...), "label": str}, ...]};
        первый вариант — лучший по мнению движка, его выбирают, если нейронка не ответила."""
        if self.phase == "over" or self._actor() != "B":
            return None
        trump = self.trump
        cheap = self._cheap

        if self.phase == "attack":
            if not self.table:
                return {"kind": "attack", "options": self._bot_attack_options()}
            cands = [c for c in self.addable_cards("B") if c[1] != trump]
            bito = {"act": ("bito",), "label": "Сказать «бито»: больше не подкидывать, карты уходят в отбой"}
            if cands:
                c = min(cands, key=cheap)
                add = {"act": ("add", c), "label": f"Подкинуть {card_str(c)}"}
                opts = [add, bito] if random.random() < BOT_ADD_CHANCE else [bito, add]
            else:
                opts = [bito]
            return {"kind": "throw", "options": opts}

        if self.phase == "defend":
            return {"kind": "defend", "options": self._bot_defend_options()}

        # adding: игрок взял карты — Амадеус может докинуть мелочь
        cands = [c for c in self.addable_cards("B") if c[1] != trump]
        if not cands:
            return {"kind": "adding", "options": [{"act": ("finish",), "label": "Подкидывать больше нечего"}]}
        room = self.limit - len(self.table)
        chosen = sorted(cands, key=cheap)[:max(room, 0)]
        return {"kind": "adding", "options": [
            {"act": ("add_all", chosen), "label": "Подкинуть ещё: " + " ".join(card_str(c) for c in chosen)},
            {"act": ("finish",), "label": "Не подкидывать — пусть забирает только то, что уже лежит"},
        ]}

    def _bot_attack_options(self) -> list:
        hand = sorted(self.hands["B"], key=self._cheap)
        lowest = hand[0]
        opts = [{"act": ("attack", lowest), "label": f"Пойти самой младшей картой {card_str(lowest)}"}]
        taken = [lowest]
        ranks = Counter(c[0] for c in hand)
        pairs = [c for c in hand if c[1] != self.trump and ranks[c[0]] >= 2 and c not in taken]
        if pairs:
            c = pairs[0]
            opts.append({"act": ("attack", c),
                         "label": f"Пойти {card_str(c)} (в руке есть ещё карты этого достоинства — будет чем подкинуть)"})
            taken.append(c)
        for c in hand[1:]:
            if len(opts) >= BOT_ATTACK_CANDIDATES:
                break
            if c[1] != self.trump and c not in taken and c[0] != lowest[0]:
                opts.append({"act": ("attack", c), "label": f"Пойти {card_str(c)}"})
                taken.append(c)
        return opts

    def _bot_defend_plan(self) -> Optional[dict]:
        avail = list(self.hands["B"])
        plan = {}
        for i in sorted(self._undefended(), key=lambda i: self._strength(self.table[i][0]), reverse=True):
            att = self.table[i][0]
            opts = [c for c in avail if self.can_beat(att, c)]
            if not opts:
                return None
            best = min(opts, key=self._cheap)
            plan[i] = best
            avail.remove(best)
        return plan

    def _bot_defend_options(self) -> list:
        trump = self.trump
        plan = self._bot_defend_plan()
        take = {"act": ("take",), "label": "Не отбиваться и взять карты"}
        if plan is None:
            return [take]
        desc = "; ".join(f"{card_str(c)} бьёт {card_str(self.table[i][0])}" for i, c in sorted(plan.items()))
        defend = {"act": ("defend", plan), "label": f"Отбиться ({desc})"}
        spends = [(c, self.table[i][0]) for i, c in plan.items() if c[1] == trump and self.table[i][0][1] != trump]
        costly = any(c[0] >= BOT_SPEND_TRUMP_MIN_RANK for c, _ in spends)
        if costly and len(self.deck) > BOT_KEEP_TRUMP_DECK_MIN:
            return [take, defend]           # движок считает, что козырь жалко — лучше взять
        if spends and self.deck:
            return [defend, take]           # козырь потратить можно, но и взять — тоже вариант
        return [defend]

    def bot_apply(self, act: tuple):
        """Выполняет вариант из bot_decision()["options"][i]["act"]."""
        kind = act[0]
        if kind in ("attack", "add"):
            self._play_attack("B", act[1])
            if self.phase == "attack":
                self.phase = "defend"
        elif kind == "bito":
            self._bito()
        elif kind == "take":
            self._note("Амадеус не отбивается и берёт карты.")
            self.phase = "adding"
        elif kind == "defend":
            for i in sorted(act[1]):
                att, c = self.table[i][0], act[1][i]
                self.hands["B"].remove(c)
                self.table[i][1] = c
                self._note(f"Амадеус бьёт {card_str(att)} картой {card_str(c)}.")
            self.target = None
            self.phase = "attack"
        elif kind == "add_all":
            for c in act[1]:
                if c in self.hands["B"] and self.can_add(c):
                    self._play_attack("B", c)
            self._finish_take()
        elif kind == "finish":
            self._finish_take()
        else:
            raise ValueError(f"неизвестное действие бота: {act!r}")

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

    def _status(self):
        """(длинная подсказка в HTML для подписи, короткая надпись для картинки)."""
        if self.phase == "over":
            if self.result == "P":
                return "<b>Ты избавился от карт — ты выиграл!</b>", "Ты выиграл!"
            if self.result == "B":
                return "<b>Ты остался с картами — ты дурак.</b>", "Ты дурак"
            return "<b>Ничья.</b>", "Ничья"
        if self.phase == "defend":
            cards, _, _ = self.available_actions()
            if cards:
                return "<b>Твой ход:</b> отбивай кнопкой с картой или бери.", "Отбивайся или бери"
            return "<b>Побить нечем — придётся брать.</b>", "Побить нечем — бери"
        if self.phase == "adding":
            return ("<b>Амадеус берёт.</b> Можешь подкинуть ещё карты того же достоинства или нажать «Хватит».",
                    "Подкидывай или жми «Хватит»")
        if self.table:
            return ("<b>Всё отбито.</b> Подкинь карту того же достоинства или нажми «Бито».",
                    "Всё отбито — подкинь или «Бито»")
        return "<b>Твоя атака:</b> выбери карту, которой пойдёшь.", "Твоя атака"

    def end_comment_fallback(self) -> str:
        """Готовая реплика Амадеус на конец партии — на случай, если нейронка недоступна."""
        if self.result == "P":
            return random.choice(WIN_LINES)
        if self.result == "B":
            return random.choice(LOSE_LINES)
        return random.choice(DRAW_LINES)

    def img_state(self) -> dict:
        """Данные для durak_img.render_table_png."""
        player_turn = self.phase != "over" and self._actor() == "P"
        playable = None
        if player_turn:
            cards, _, _ = self.available_actions()
            playable = {parse_card_code(code[1:]) for _, code in cards}
        und = self._undefended()
        return dict(
            trump=self.trump,
            trump_card=self.trump_card if self.deck else None,
            deck_count=len(self.deck),
            discard_count=self.discard_count,
            table=[list(p) for p in self.table],
            hand=self.sort_hand(self.hands["P"]),
            opponents=[{
                "name": "Амадеус", "count": len(self.hands["B"]),
                "role": None if self.phase == "over" else ("attack" if self.attacker == "B" else "defend"),
            }],
            playable=playable,
            target_idx=self._current_target() if (player_turn and self.phase == "defend" and len(und) > 1) else None,
            status=self._status()[1] if (player_turn or self.phase == "over") else None,
        )

    def render_caption(self, comment: str = "", with_hand: bool = False) -> str:
        """Подпись под картинкой стола (HTML, до CAPTION_LIMIT символов): реплика Амадеус, что произошло, подсказка.
        with_hand=True — добавить руку текстом (на случай, если картинку нарисовать не вышло)."""
        e = html.escape
        trump = f"Козырь: <b>{SUIT_SYMBOL[self.trump]}</b>"
        if self.deck:
            trump += f" (нижняя: {card_str(self.trump_card)})"
        trump += f" · колода {len(self.deck)} · бито {self.discard_count}"
        tail = [trump if with_hand else None]
        if with_hand:
            if self.table:
                tail.append("Стол: " + "; ".join(f"{card_str(a)} ← {card_str(d) if d else '?'}" for a, d in self.table))
            tail.append(f"У Амадеус: {_cards_ru(len(self.hands['B']))}")
            tail.append("Твои карты: " + (" ".join(card_str(c) for c in self.sort_hand(self.hands["P"])) or "—"))
        status = self._status()[0]
        if self.phase == "over":
            status += "\nРеванш: /durak"
        tail = [t for t in tail if t]

        comment = (comment or "").strip()
        log_lines = [e(x) for x in self.log]
        while True:
            parts = []
            if comment:
                parts.append(e(comment))
            if log_lines:
                parts.append("\n".join(log_lines))
            parts.extend(["\n".join(tail)] if tail else [])
            parts.append(status)
            caption = "\n\n".join(parts)
            if len(caption) <= CAPTION_LIMIT:
                return caption
            if log_lines:
                log_lines = log_lines[-3:] if len(log_lines) > 3 else log_lines[1:]
            elif comment:
                comment = comment[:max(40, len(comment) - 120)].rstrip() + "…"
            else:
                return caption[:CAPTION_LIMIT]

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
        lines.append(self._status()[0])
        if self.phase == "over":
            lines.append(e(self.end_comment_fallback()))
            lines.append("Реванш: /durak")
        return "\n".join(lines)


# ==================== НЕЙРОНКА: ПРОМПТЫ И РАЗБОР ОТВЕТА ====================
DURAK_SYSTEM_ADDON = """**РЕЖИМ ПАРТИИ В ДУРАКА:**
Сейчас ты играешь с собеседником в подкидного дурака (36 карт, один на один). Ходы ты сама с нуля НЕ придумываешь и расклад глубоко не считаешь: карточный модуль заранее готовит для тебя короткий нумерованный список вариантов хода (первый — совет движка, но выбрать можно любой, по характеру: осторожно, жадно, с блефом). Ты выбираешь ровно один и в своём характере коротко (1-2 предложения) реагируешь на происходящее за столом — как живой человек за картами: можно подколоть, похвалить, смутиться, поворчать, что не везёт.
Формат ответа:
1) текст реплики (без звёздочек и описаний действий; список вариантов и их номера в тексте не упоминай; свои карты собеседнику не выдавай и не перечисляй — он их не видит);
2) если в служебной информации есть варианты — отдельной строкой [durak_move: N], где N — номер выбранного варианта;
3) последней строкой [emotion: ключ].
Не пиши теги [no_reply], [grudge], [trust], [rp_mode] в этом режиме. Не выдумывай ходов, которых нет в списке. Если собеседник пишет не про карты — отвечай по-человечески, но не забывай про партию."""

DURAK_MOVE_TAG_RE = re.compile(r'\[\s*durak_move\s*:\s*(\d+)\s*\]\.?', re.IGNORECASE)


def parse_durak_move_tag(text: str):
    """(текст без тега, номер выбранного варианта | None)."""
    if not text:
        return text, None
    matches = list(DURAK_MOVE_TAG_RE.finditer(text))
    if not matches:
        return text, None
    try:
        idx = int(matches[-1].group(1))
    except ValueError:
        idx = None
    return DURAK_MOVE_TAG_RE.sub('', text).strip(), idx


def build_prompt(game: DurakGame, situation: str, decision: Optional[dict] = None,
                 comment: str = "", first_name: Optional[str] = None) -> str:
    """situation: start | move | chat | user_won | bot_won | draw."""
    name = f" ({first_name})" if first_name else ""
    lines = [f"[Служебная информация: идёт партия в подкидного дурака один на один. Собеседник{name} играет против тебя.]"]

    trump = f"Козырь: {SUIT_SYMBOL[game.trump]}"
    if game.deck:
        trump += f" (нижняя карта колоды: {card_str(game.trump_card)}), в колоде {len(game.deck)}"
    else:
        trump += ", колода кончилась"
    lines.append(f"{trump}, в отбое {game.discard_count}.")
    lines.append("Твои карты (собеседнику не показывай): "
                 + (" ".join(card_str(c) for c in game.sort_hand(game.hands["B"])) or "нет"))
    lines.append(f"Карт у собеседника: {len(game.hands['P'])}.")
    if game.table:
        lines.append("Стол: " + "; ".join(
            f"{card_str(a)} ← {card_str(d) if d else 'не отбита'}" for a, d in game.table))
    elif game.phase != "over":
        lines.append("Стол пуст.")
    if game.phase != "over":
        lines.append("Сейчас атакуешь ты." if game.attacker == "B" else "Сейчас атакует собеседник.")
    if game.events:
        lines.append("Что только что произошло: " + " ".join(game.events))
    if game.dialog:
        lines.append("Недавние реплики:\n" + "\n".join(game.dialog))

    if situation == 'start':
        if decision:
            lines.append("Партия только начинается, ты ходишь первой. Коротко поприветствуй собеседника в своём "
                         "характере и сделай первый ход.")
        else:
            lines.append("Партия только начинается, первый ход за собеседником. Коротко и в своём характере "
                         "поприветствуй его и предложи начинать. Ход выбирать не нужно.")
    elif situation == 'move':
        line = "Сейчас твой ход в партии. Отреагируй на происходящее и выбери вариант."
        if comment:
            line += f" Собеседник приписал к своему ходу реплику: «{comment}» — ответь и на неё."
        lines.append(line)
    elif situation == 'chat':
        lines.append(f"Собеседник написал не ход, а: «{comment}». Ответь по существу в своём характере, а потом "
                     f"напомни, что ход за ним — кнопки под сообщением со столом. Ход пока не выбирай.")
    elif situation == 'user_won':
        lines.append("Партия закончилась: собеседник избавился от карт первым, а дураком осталась ты. "
                     "Прокомментируй итог в своём характере (проигрывать ты не любишь, но признавать поражение умеешь).")
    elif situation == 'bot_won':
        lines.append("Партия закончилась: ты избавилась от карт, а дураком остался собеседник. "
                     "Прокомментируй итог в своём характере.")
    elif situation == 'draw':
        lines.append("Партия закончилась ничьей — карты кончились у обоих. Коротко прокомментируй итог.")

    if decision and decision.get("options"):
        numbered = "\n".join(f"{i}. {o['label']}" for i, o in enumerate(decision["options"], 1))
        lines.append("Варианты хода (выбери ровно один и укажи его тегом [durak_move: N]):\n" + numbered)
    return "\n\n".join(lines)


# ==================== РАЗБОР ХОДА, НАПИСАННОГО ТЕКСТОМ ====================
# «6♣ держи», «В пик», «10ч», «беру», «бито», «хватит»: ход в начале (или в конце) сообщения, остальное — реплика.
_SUIT_CHARS = {"♠": "S", "♤": "S", "♥": "H", "♡": "H", "♦": "D", "♢": "D", "♣": "C", "♧": "C",
               "s": "S", "h": "H", "d": "D", "c": "C", "п": "S", "ч": "H", "б": "D", "к": "C", "т": "C"}
_RANK_CHARS = {"6": 6, "7": 7, "8": 8, "9": 9, "10": 10, "в": 11, "j": 11, "д": 12, "q": 12,
               "к": 13, "k": 13, "т": 14, "a": 14}
_RANK_PAT = r"(10|[6-9]|[вдктjqka])"
_CARD_RE = re.compile(rf"^{_RANK_PAT}([♠♤♥♡♦♢♣♧shdcпчбкт])$", re.IGNORECASE)
_RANK_RE = re.compile(rf"^{_RANK_PAT}$", re.IGNORECASE)
_COMMANDS = {
    "take": {"беру", "взять", "возьму", "заберу", "take"},
    "bito": {"бито", "bito"},
    "done": {"хватит", "done"},
}


def _clean_token(tok: str) -> str:
    return (tok or "").strip().strip('.,!?;:()"«»\'').lower()


def _suit_from_word(word: str) -> Optional[str]:
    w = _clean_token(word)
    if w.startswith("пик"):
        return "S"
    if w.startswith("чер"):
        return "H"
    if w.startswith("буб"):
        return "D"
    if w.startswith("треф") or w.startswith("крес"):
        return "C"
    return None


def _parse_at(parts: list, i: int):
    """Пробует прочитать ход, начиная с parts[i]. Возвращает (действие | None, сколько слов занял ход)."""
    tok = _clean_token(parts[i])
    for action, words in _COMMANDS.items():
        if tok in words:
            return action, 1
    m = _CARD_RE.match(tok)
    if m:
        return "c" + card_code((_RANK_CHARS[m.group(1).lower()], _SUIT_CHARS[m.group(2).lower()])), 1
    if _RANK_RE.match(tok) and i + 1 < len(parts):
        suit = _suit_from_word(parts[i + 1])
        if suit is None and len(_clean_token(parts[i + 1])) == 1:
            suit = _SUIT_CHARS.get(_clean_token(parts[i + 1]))
        if suit:
            return "c" + card_code((_RANK_CHARS[tok], suit)), 2
    return None, 0


def parse_move_text(text: str):
    """(действие | None, реплика). Действие в том же виде, что и в кнопках: «c6C» (карта), take, bito, done."""
    # Кнопка «✍️ С репликой» вставляет в поле «@бот 6♣ »; если игрок отправил это обычным сообщением,
    # а не выбрал вариант из списка, упоминание бота в начале просто отбрасываем.
    text = re.sub(r"^\s*@\w+\s*", "", text or "")
    parts = text.split()
    if not parts:
        return None, ""
    action, n = _parse_at(parts, 0)
    if action:
        return action, " ".join(parts[n:]).strip()
    for k in (2, 1):  # ход в конце сообщения: «ну давай 6♣» или «ну давай В пик»
        if len(parts) > k:
            action, n = _parse_at(parts, len(parts) - k)
            if action and n == k:
                return action, " ".join(parts[:-k]).strip()
    return None, (text or "").strip()


def action_label(action: str) -> str:
    """Подпись действия для подсказок («6♣», «беру»)."""
    if action.startswith("c"):
        return card_str(parse_card_code(action[1:]))
    return {"take": "беру", "bito": "бито", "done": "хватит"}.get(action, action)
