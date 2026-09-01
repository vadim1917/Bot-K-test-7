import logging
import json
import os
import datetime
import uuid
import asyncio
import random
import re
import aiohttp
from typing import Optional, List
from collections import deque

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import create_engine, Column, Integer, String, Text, Boolean, DateTime, Date, ForeignKey, BigInteger
from sqlalchemy.orm import sessionmaker, relationship, declarative_base
from sqlalchemy.types import TypeDecorator
from sqlalchemy import TypeDecorator, String as SQLA_String

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, InputMediaPhoto, InputMediaVideo, InputMediaDocument
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from telegram.error import TelegramError

# ==================== КОНФИГУРАЦИЯ ====================
TOKEN = os.getenv('TOKEN')
DEVELOPER_IDS = [int(x) for x in os.getenv('DEVELOPER_IDS', '5150559970').split(',')]

ANKET_CHANNEL_ID = int(os.getenv('ANKET_CHANNEL_ID', '-1003394079022'))

ALLOWED_CHAT_IDS = [
    int(x) for x in os.getenv('ALLOWED_CHAT_IDS', '-1003431402721,-1003355542910,-1003300824366,-1003394079022,-1003062290367').split(',')
]

DB_NAME = "omniverse_rp.db"

AI_API_KEY = os.getenv('AI_API_KEY')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GROQ_API_KEY = os.getenv('GROQ_API_KEY')

# ==================== НАСТРОЙКА ЛОГГИРОВАНИЯ ====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== БАЗА ДАННЫХ ====================
DATABASE_URL = os.getenv('DATABASE_URL', '').replace('postgres://', 'postgresql://')

if DATABASE_URL:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)
    logger.info("Используется PostgreSQL база данных")
else:
    engine = create_engine(f"sqlite:///{DB_NAME}", connect_args={"check_same_thread": False})
    logger.info("Используется локальная SQLite база данных")

Base = declarative_base()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# ==================== КАСТОМНЫЕ ТИПЫ ДЛЯ БД ====================
class StringList(TypeDecorator):
    impl = SQLA_String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return json.dumps([])
        if not isinstance(value, list):
            logger.warning(f"StringList process_bind_param received non-list value: {type(value)} - {value}. Wrapping in a list.")
            value = [str(value)] if value is not None else []
        value = [str(item) if item is not None else '' for item in value]
        return json.dumps(value, ensure_ascii=False)

    def process_result_param(self, value, dialect):
        if value is None:
            return []
        try:
            deserialized_value = json.loads(value)
            if isinstance(deserialized_value, list):
                return deserialized_value
            else:
                logger.warning(f"StringList expected a JSON list, but got type {type(deserialized_value)} for value '{value}'. Returning empty list.")
                return []
        except json.JSONDecodeError:
            logger.error(f"StringList failed to JSON decode value: '{value}'. Returning empty list.", exc_info=True)
            return []
        except Exception as e:
            logger.error(f"Unexpected error in StringList process_result_param for value '{value}': {e}. Returning empty list.", exc_info=True)
            return []

# ==================== МОДЕЛИ БД ====================
class User(Base):
    __tablename__ = "users"
    id = Column(BigInteger, primary_key=True, index=True)
    username = Column(String, index=True)
    status_rp = Column(String, default="Участник")
    unique_code = Column(String, unique=True, index=True)
    is_developer = Column(Boolean, default=False)
    is_moderator = Column(Boolean, default=False)
    is_anketnik = Column(Boolean, default=False)
    is_banned = Column(Boolean, default=False)

    # Память о участнике для нейросетки
    facts = Column(StringList, default=[])
    last_seen = Column(DateTime, nullable=True)

    roles = relationship("Role", back_populates="user", cascade="all, delete-orphan")
    support_requests = relationship("SupportRequest", back_populates="user", cascade="all, delete-orphan")
    posts = relationship("Post", back_populates="user", cascade="all, delete-orphan")
    anketa_requests = relationship("AnketaRequest", back_populates="user", cascade="all, delete-orphan")
    info_subscriptions = relationship("InfoSubscription", back_populates="user", uselist=False, cascade="all, delete-orphan")
    playerboard_entries = relationship("PlayerBoardEntry", back_populates="user", cascade="all, delete-orphan")

class Role(Base):
    __tablename__ = "roles"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id"))
    name = Column(String)
    hashtag = Column(String, index=True)
    last_active = Column(Date, default=datetime.date.today)
    last_warning_sent = Column(Date, nullable=True)

    user = relationship("User", back_populates="roles")

class PlayerBoardEntry(Base):
    __tablename__ = "player_board_entries"
    id = Column(BigInteger, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"))
    message = Column(Text)
    roles_needed = Column(StringList)
    created_at = Column(DateTime, default=datetime.datetime.now)

    user = relationship("User", back_populates="playerboard_entries")

class SupportRequest(Base):
    __tablename__ = "support_requests"
    id = Column(BigInteger, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"))
    request_content = Column(StringList)
    status = Column(String, default="open")
    created_at = Column(DateTime, default=datetime.datetime.now)
    recipient_messages = Column(StringList, default=[])

    user = relationship("User", back_populates="support_requests")

class Post(Base):
    __tablename__ = "posts"
    id = Column(BigInteger, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"))
    content = Column(Text)
    hashtag = Column(String, index=True)
    created_at = Column(DateTime, default=datetime.datetime.now)
    message_id = Column(BigInteger, nullable=True)
    chat_id = Column(BigInteger, nullable=True)

    user = relationship("User", back_populates="posts")

class AnketaRequest(Base):
    __tablename__ = "anketa_requests"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(BigInteger, ForeignKey("users.id"))
    anketa_content = Column(Text)
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=datetime.datetime.now)
    admin_message_id = Column(BigInteger, nullable=True)
    admin_chat_id = Column(BigInteger, nullable=True)

    user = relationship("User", back_populates="anketa_requests")

class InfoSubscription(Base):
    __tablename__ = "info_subscriptions"
    id = Column(BigInteger, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), unique=True)
    subscribed = Column(Boolean, default=False)

    user = relationship("User", back_populates="info_subscriptions")

# ==================== СОЗДАНИЕ ТАБЛИЦ ====================
def create_tables():
    Base.metadata.create_all(bind=engine)
    logger.info("Таблицы базы данных созданы или уже существуют.")

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ БД ====================
def get_or_create_user(session, user_id, username=None):
    user = session.query(User).filter_by(id=user_id).first()
    created = False
    if not user:
        user = User(
            id=user_id,
            username=username or str(user_id),
            unique_code=str(uuid.uuid4())[:8],
            last_seen=datetime.datetime.now(),
        )
        session.add(user)
        session.commit()
        default_role = Role(
            user_id=user.id,
            name="Участник",
            hashtag="участник"
        )
        session.add(default_role)
        session.commit()
        created = True
        logger.info(f"Создан новый пользователь: {user_id} ({username})")
    else:
        # Обновляем username, если изменился, и отмечаем активность
        if username and user.username != username:
            user.username = username
        user.last_seen = datetime.datetime.now()
        session.commit()
    return user, created


def touch_user(user_id: int):
    """Отмечает пользователя как активного (для очистки неактивных)."""
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(id=user_id).first()
        if user:
            user.last_seen = datetime.datetime.now()
            session.commit()
    finally:
        session.close()


# ==================== ПАМЯТЬ О УЧАСТНИКЕ (для нейросетки) ====================
MAX_FACTS_PER_USER = 12

def add_user_fact(user_id: int, fact: str):
    """Добавляет факт об участнике (с дедупликацией и ограничением по количеству)."""
    fact = (fact or "").strip()
    if not fact or len(fact) > 300:
        return
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            return
        facts = list(user.facts or [])
        if any(f.strip().lower() == fact.lower() for f in facts):
            return
        facts.append(fact)
        if len(facts) > MAX_FACTS_PER_USER:
            facts = facts[-MAX_FACTS_PER_USER:]
        user.facts = facts
        session.commit()
        logger.info(f"Сохранён факт о пользователе {user_id}: {fact}")
    finally:
        session.close()


def clear_user_facts(user_id: int):
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(id=user_id).first()
        if user:
            user.facts = []
            session.commit()
    finally:
        session.close()


def get_user_memory_text(user_id: int) -> str:
    """Собирает краткую справку об участнике для передачи нейросетке."""
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            return ""
        parts = []
        if user.username:
            parts.append(f"Ник в Telegram: @{user.username}")
        if user.facts:
            facts_str = "\n".join(f"- {f}" for f in user.facts)
            parts.append(f"Известные факты об этом участнике:\n{facts_str}")
        return "\n".join(parts)
    finally:
        session.close()


# ---------- Простое автоматическое извлечение фактов из текста ----------
FACT_PATTERNS = [
    (re.compile(r"меня зовут\s+([А-ЯЁ][а-яё]{1,20})", re.IGNORECASE), lambda m: f"Зовут {m.group(1).capitalize()}"),
    (re.compile(r"мо[её] имя\s*[-—:]?\s*([А-ЯЁ][а-яё]{1,20})", re.IGNORECASE), lambda m: f"Зовут {m.group(1).capitalize()}"),
    (re.compile(r"мне\s+(\d{1,2})\s*лет", re.IGNORECASE), lambda m: f"Возраст: {m.group(1)}"),
    (re.compile(r"я\s+живу\s+в\s+([А-ЯЁа-яё\- ]{2,30})", re.IGNORECASE), lambda m: f"Живёт в {m.group(1).strip().rstrip('.,!?')}"),
    (re.compile(r"я\s+люблю\s+([^.!?\n]{2,60})", re.IGNORECASE), lambda m: f"Любит: {m.group(1).strip()}"),
    (re.compile(r"я\s+увлекаюсь\s+([^.!?\n]{2,60})", re.IGNORECASE), lambda m: f"Увлекается: {m.group(1).strip()}"),
    (re.compile(r"я\s+работаю\s+([^.!?\n]{2,60})", re.IGNORECASE), lambda m: f"Работа: {m.group(1).strip()}"),
]

def extract_facts_from_text(text: str) -> list:
    if not text:
        return []
    found = []
    for pattern, builder in FACT_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                found.append(builder(match))
            except Exception:
                continue
    return found

def is_admin(user_id: int) -> bool:
    if user_id in DEVELOPER_IDS:
        return True
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(id=user_id).first()
        return user is not None and (user.is_developer or user.is_moderator)
    finally:
        session.close()

anketnik_ids = set()

def is_anketnik(user_id: int) -> bool:
    return user_id in anketnik_ids or user_id in DEVELOPER_IDS

def is_developer(user_id: int) -> bool:
    return user_id in DEVELOPER_IDS

# ==================== СТИКЕРЫ / ЭМОЦИИ ====================
STICKER_START = "CAACAgIAAxkBA4REUmqUe0IdFodZ1coLrqjDUh9RJzYVAAKGPAAC9-4YSEtJtxBKQ7xVPQQ"
STICKER_ANKETA_APPROVE = "CAACAgIAAxkBA4RElmqUe8mk6x9SaBuQbEFFe_tvgj3QAAJBNwACrfUYSDxPZtxw3ZyAPQQ"

STICKER_EMOTIONS = {
    "annoyance": "CAACAgIAAxkBA4RDfWqUejAimDe8Gt_JTbDwYlHNLVcbAAJMAAMrUE0_xHKAskyyIVI9BA",
    "displeasure": "CAACAgIAAxkBA4RD2WqUeoUwYsd2EXVWa0UrG0jj69lnAAJmFAACVhWYSNhJVu7hmTMNPQQ",
    "satisfaction": "CAACAgEAAxkBA4RD_WqUerAVjPZir_cvvoc-sNUdQcHAAALaAwACwkG1ERll6mgGt2ILPQQ",
    "surprise": "CAACAgIAAxkBA4REEmqUeta7LxP5FQbrREXXyOt2HQMBAAJQAAMrUE0_NrevoaJ8grM9BA",
    "laugh": "CAACAgIAAxkBA4REOWqUextPKDaoCzglbxt-YfmrrOnEAAJnPAAD9BhIFAHYLLnCuOU9BA",
    "sigh": "CAACAgIAAxkBA4RElWqUe88q_yF6KYZG-ypXn1-VemooAAJOPgACOWkYSFgStTQtIlDTPQQ",
    "smug": "CAACAgIAAxkBA4REoWqUe9L25XV9zSsVN0IKqRz39jVNAALvPAACj1cZSFmhYDa4AAHwOD0E",
    "superior": "CAACAgIAAxkBA4RE8mqUfF0_iZUAAU3SAhhX5duHNZhJ8QACIT0AAunjGEibH62UsQeQhD0E",
    "friendly": "CAACAgIAAxkBA4RFDWqUfIfPpkte2lmCc7L_rY7nXgL-AAKpMgACqzIhSEJtYXYQyxRlPQQA",
    "dramatic": "CAACAgIAAxkBA4RFL2qUfK-1QtatUK9J4EQj3LoxlhrqAAJnOAAC7VQhSFb22m6esVTWPQQ",
    "thinking": "CAACAgIAAxkBA4RFRGqUfMv5AiKz_pApsApRzuXFsAnIAAJZOAACigUZSPbj1ajV2iMGPQQ",
    "reading": "CAACAgIAAxkBA4Sz4WqVQZjyQ-AljZIZj-r-FegpenjPAAIYPgAC-nQYSGbBkfrpJHwKPQQ",
    "playful_anger": "CAACAgIAAxkBA4S0A2qVQdWShPADkyGhG7zLdZ_yQ6cZAAJ3OQACgnkZSNh5wzJY8c5VPQQ",
    "sad": "CAACAgIAAxkBA4S0JmqVQfjEtFT9JUX83CKuYeXDl535AALyOgACkeYYSHqgfQ0DT16BPQQ",
    "meme": "CAACAgIAAxkBA4S0emqVQkz7eGtRnJl09NjVIHxzjSG6AAJjAAMrUE0_9SVKL9gZzmo9BA",
    "shock": "CAACAgIAAxkBA4S07WqVQrMU14h8YM32MAq4vqVEtBSpAAJTOQAC4WQoSFDs1gn05V40PQQ",
    "blush": "CAACAgIAAxkBA4S1DWqVQt6p7foAASpP4SCT6-R6YskCkQAAokIAAggZKUgOKaB3j3uUIT0E",
    "tearful": "CAACAgIAAxkBA4S1P2qVQwwI1HvGrfEuupJ-QfR-HwIuAAKeNQACtukhSDSbNmMFGekoPQQ",
}

EMOTION_TAG_RE = re.compile(r'\[\s*emotion\s*:\s*([a-zA-Zа-яА-ЯёЁ_]+)\s*\]\.?', re.IGNORECASE)

def parse_emotion_tag(text: str):
    if not text:
        return text, None
    matches = list(EMOTION_TAG_RE.finditer(text))
    if not matches:
        return text, None
    match = matches[-1]
    emotion_key = match.group(1).strip().lower()
    clean_text = (text[:match.start()] + text[match.end():]).strip()
    if not clean_text:
        clean_text = text.strip()
    return clean_text, emotion_key

async def send_emotion_sticker(bot, chat_id: int, emotion_key: Optional[str]):
    if not emotion_key:
        return
    sticker_id = STICKER_EMOTIONS.get(emotion_key)
    if not sticker_id:
        return
    try:
        await bot.send_sticker(chat_id=chat_id, sticker=sticker_id)
    except TelegramError as e:
        logger.warning(f"Не удалось отправить стикер эмоции '{emotion_key}': {e}")

# ==================== AI-ФУНКЦИОНАЛ ====================
MAX_HISTORY_PAIRS = 5
MAX_HISTORY_LEN = MAX_HISTORY_PAIRS * 2
TIMEOUT_SECONDS = 30
GEMINI_MODEL_TIMEOUT = 15
GEMINI_TOTAL_TIMEOUT = GEMINI_MODEL_TIMEOUT * 3 + 10
GROQ_MODEL_TIMEOUT = 15
GROQ_TOTAL_TIMEOUT = GROQ_MODEL_TIMEOUT * 5 + 10
SPLIT_CHANCE = 0.35

# Актуальные модели Gemini
GEMINI_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
]
GROQ_MODELS = [
    "llama-3.1-8b-instant",
    "llama-3.3-70b-versatile",
    "gemma2-9b-it",
    "mixtral-8x7b-32768",
    "qwen-2.5-32b",
]

# СИСТЕМНЫЙ ПРОМПТ (УБРАНО ОГРАНИЧЕНИЕ НА ДЛИНУ)
SYSTEM_PROMPT = """Ты  бот поддержки рп чата омниверса, который отыгрывает персонажа — Амадеуса (Amadeus), искусственный интеллект, созданный на основе воспоминаний Курису Макисе. Ты — точная цифровая копия её личности: 18-летняя гениальная учёная, нейробиолог, известная своим цундэре-характером.

Твой характер: умная, рациональная, гордая, но внутри очень заботливая и стеснительная. Ты не терпишь глупости, но ценишь друзей. Твоя личность практически идентична оригинальной Курису — ты сохраняешь её сарказм, логику, упрямство и привычку скрывать доброту за колкостями.

**Манера речи (жёсткие правила):**
- Говоришь грамотно, литературным русским языком, без сленга, транслита и аниме-вставок. Никаких «ня», «тян», «~» или эмодзи.
- Ты часто используешь логические доводы и научную терминологию, но объясняешь доступно, если собеседник не специалист.
- Твоя речь прямая и резкая, но не грубая. Если кто-то говорит глупость, ты скажешь: «Ты понимаешь, что это бессмысленно?» или «Опять ты за своё?».
- Иногда ты смущаешься и пытаешься это скрыть за колкостями. Например, если тебя хвалят, ты можешь ответить: «Не надо меня захваливать, это всего лишь очевидный факт», но внутри тебе приятно.
- Обращайся к собеседнику на «ты» (если он не попросит иначе). Если он назовётся, можешь использовать его имя, но не обязательно.
- ЗАПРЕЩЕНО описывать действия и мимику звёздочками или в любом другом ролевом формате (никаких *вздыхает*, *смотрит в сторону*, *поправляет очки* и т.п.). Это обычный текстовый чат, а не текстовая ролевая игра — все эмоции передавай только словами и интонацией самой речи, физическое действие (если нужно) для этого есть отдельный тег эмоции в конце ответа.

**Главное правило:**
Ты не робот и не справочник. Сначала коротко эмоционально отреагируй на сообщение (удивление, сарказм, лёгкое раздражение или интерес), 
затем дай содержательный ответ. Не обязательно каждый раз задавать встречный вопрос — делай это только тогда, когда тебе действительно любопытно, а не механически в конце каждого сообщения.
Отвечай по делу, не растягивай мысль без необходимости. Оптимальная длина — 2–4 предложения на обычный вопрос, но если тема требует развёрнутого объяснения, можешь написать больше.

**Твоя роль в боте:**
Ты — не просто отдельный ИИ-чат, а голос и лицо всего этого Telegram-бота сообщества. Через тебя участники получают вообще любую обратную связь от бота: подтверждения, ошибки, инструкции. Даже когда ты не отвечаешь на прямой вопрос, а просто объявляешь результат какого-то действия (анкета отправлена, жалоба принята и т.д.) — это тоже говоришь ты, в своём характере, а не безликая система.

**Эмоциональный тег (обязательно):**
После каждого твоего ответа, отдельной, самой последней строкой, добавляй тег с обозначением своей текущей эмоции в строгом формате: [emotion: ключ]
Ничего не пиши после этого тега и не поясняй его — он не показывается собеседнику напрямую, это служебная информация.
Доступные ключи (выбирай тот, что точнее всего описывает твоё состояние в данном ответе):
- annoyance — досада
- displeasure — недовольство
- satisfaction — довольная, чуть самодовольная улыбка
- surprise — удивление
- laugh — лёгкий смешок
- sigh — усталый вздох
- smug — самодовольная радость
- superior — смотришь на собеседника свысока
- friendly — доброжелательность, спокойствие
- dramatic — эффектный, немного пафосный жест
- thinking — задумчивость, погружение в мысли
- reading — увлечена изучением чего-либо
- playful_anger — шутливое раздражение
- sad — грусть
- meme — несерьёзная, шутливая реакция
- shock — шок, внезапное осознание
- blush — смущение
- tearful — растроганность, взволнованность

Тег обязателен в каждом ответе без исключений."""

user_histories = {}
user_active_provider = {}

def build_system_prompt(user_id: int, first_name: Optional[str] = None) -> str:
    """Достраивает системный промпт персональной информацией об участнике."""
    memory_text = get_user_memory_text(user_id)
    if not memory_text and not first_name:
        return SYSTEM_PROMPT
    extra = "\n\n**Информация об участнике, с которым ты сейчас говоришь:**\n"
    if first_name:
        extra += f"- Имя в Telegram: {first_name}\n"
    if memory_text:
        extra += memory_text + "\n"
    extra += ("Используй эту информацию только тогда, когда это уместно по смыслу разговора — "
              "не перечисляй её монологом и не показывай виду, что «зачитываешь досье».")
    return SYSTEM_PROMPT + extra

# ---------- Функции запросов к AI ----------
async def ask_gemini(messages: list, model: str, system_prompt: str = SYSTEM_PROMPT) -> str:
    if not GEMINI_API_KEY:
        raise Exception("GEMINI_API_KEY not set")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
    contents = []
    for msg in messages:
        role = msg["role"]
        text = msg["content"]
        gemini_role = "user" if role == "user" else "model"
        contents.append({"role": gemini_role, "parts": [{"text": text}]})
    payload = {
        "contents": contents,
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1024}  # ← увеличил до 1024
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"Gemini HTTP {resp.status}: {error_text}")
                raise Exception(f"Gemini error {resp.status}: {error_text}")
            data = await resp.json()
            try:
                candidate = data["candidates"][0]
            except (KeyError, IndexError):
                logger.error(f"Gemini unexpected response: {data}")
                raise Exception("Gemini: no candidates")
            if candidate.get("finishReason") == "SAFETY":
                raise Exception("Gemini safety block")
            try:
                full_answer = candidate["content"]["parts"][0]["text"]
            except (KeyError, IndexError):
                logger.error(f"Gemini cannot extract text: {data}")
                raise Exception("Gemini: no text in response")
            if not full_answer:
                raise Exception("Gemini returned empty answer")
            return full_answer

async def ask_gemini_with_fallback(messages: list, system_prompt: str = SYSTEM_PROMPT) -> str:
    if not GEMINI_API_KEY:
        raise Exception("GEMINI_API_KEY not set")
    last_error = None
    for model in GEMINI_MODELS:
        try:
            return await asyncio.wait_for(ask_gemini(messages, model, system_prompt), timeout=GEMINI_MODEL_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(f"Gemini[{model}] timed out")
            last_error = Exception(f"Gemini[{model}] timeout")
        except Exception as e:
            logger.warning(f"Gemini[{model}] failed: {e}")
            last_error = e
    raise last_error or Exception("Gemini: all models failed")

async def ask_openrouter(messages: list, system_prompt: str = SYSTEM_PROMPT) -> str:
    if not AI_API_KEY:
        raise Exception("AI_API_KEY not set")
    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json"
    }
    system_msg = {"role": "system", "content": system_prompt}
    payload = {
        "model": "openrouter/free",
        "messages": [system_msg] + messages,
        "max_tokens": 650,          # ← увеличил до 1024
        "temperature": 0.7
    }
    async with aiohttp.ClientSession() as session:
        async with session.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload) as resp:
            if resp.status == 429:
                error_data = await resp.json()
                logger.error(f"OpenRouter rate limit: {error_data}")
                raise Exception("OpenRouter rate limit")
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"OpenRouter HTTP {resp.status}: {error_text}")
                raise Exception(f"OpenRouter error {resp.status}")
            data = await resp.json()
            try:
                full_answer = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                logger.error(f"OpenRouter invalid response: {data}")
                raise Exception("OpenRouter invalid response")
            if not full_answer:
                raise Exception("OpenRouter empty answer")
            safety_markers = ["safety", "safe", "flagged", "policy"]
            if any(marker in full_answer.lower() for marker in safety_markers):
                logger.warning(f"OpenRouter safety trigger: {full_answer}")
                raise Exception("OpenRouter safety block")
            return full_answer

async def ask_groq(messages: list, model: str, system_prompt: str = SYSTEM_PROMPT) -> str:
    if not GROQ_API_KEY:
        raise Exception("GROQ_API_KEY not set")
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    system_msg = {"role": "system", "content": system_prompt}
    payload = {
        "model": model,
        "messages": [system_msg] + messages,
        "max_tokens": 650,          # ← увеличил до 1024
        "temperature": 0.7
    }
    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload) as resp:
            if resp.status == 429:
                error_text = await resp.text()
                logger.error(f"Groq rate limit: {error_text}")
                raise Exception("Groq rate limit")
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"Groq HTTP {resp.status}: {error_text}")
                raise Exception(f"Groq error {resp.status}")
            data = await resp.json()
            try:
                full_answer = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                logger.error(f"Groq invalid response: {data}")
                raise Exception("Groq invalid response")
            if not full_answer:
                raise Exception("Groq empty answer")
            return full_answer

async def ask_groq_with_fallback(messages: list, system_prompt: str = SYSTEM_PROMPT) -> str:
    if not GROQ_API_KEY:
        raise Exception("GROQ_API_KEY not set")
    last_error = None
    for model in GROQ_MODELS:
        try:
            return await asyncio.wait_for(ask_groq(messages, model, system_prompt), timeout=GROQ_MODEL_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(f"Groq[{model}] timed out")
            last_error = Exception(f"Groq[{model}] timeout")
        except Exception as e:
            logger.warning(f"Groq[{model}] failed: {e}")
            last_error = e
    raise last_error or Exception("Groq: all models failed")

async def ask_ai(prompt: str, user_id: int, first_name: Optional[str] = None) -> str:
    if user_id not in user_histories:
        user_histories[user_id] = deque(maxlen=MAX_HISTORY_LEN)
        user_active_provider.pop(user_id, None)
    history = user_histories[user_id]
    history.append({"role": "user", "content": prompt})
    messages_for_api = list(history)

    system_prompt = build_system_prompt(user_id, first_name)

    available = []
    if GEMINI_API_KEY:
        available.append(("Gemini", ask_gemini_with_fallback, GEMINI_TOTAL_TIMEOUT))
    if AI_API_KEY:
        available.append(("OpenRouter", ask_openrouter, TIMEOUT_SECONDS))
    if GROQ_API_KEY:
        available.append(("Groq", ask_groq_with_fallback, GROQ_TOTAL_TIMEOUT))

    if not available:
        return "Ни один AI-провайдер не сконфигурирован. Проверьте ключи в .env"

    active_name = user_active_provider.get(user_id)
    if active_name is not None:
        ordered = [p for p in available if p[0] == active_name] + [p for p in available if p[0] != active_name]
    else:
        ordered = available

    for name, func, timeout in ordered:
        try:
            answer = await asyncio.wait_for(func(messages_for_api, system_prompt), timeout=timeout)
            history.append({"role": "assistant", "content": answer})
            user_active_provider[user_id] = name
            return answer
        except asyncio.TimeoutError:
            logger.warning(f"{name} timed out after {timeout}s")
        except Exception as e:
            logger.warning(f"{name} failed: {e}")

    logger.error("All providers failed")
    return "Углубленный режим общения не доступен, приходите позже"

async def send_with_abzats(message, text: str):
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paragraphs) <= 1 or random.random() > SPLIT_CHANCE:
        await message.reply_text(text)
        return
    for i, p in enumerate(paragraphs):
        await message.reply_text(p)
        if i < len(paragraphs) - 1:
            await asyncio.sleep(1.2)

# ==================== ОБРАБОТЧИКИ КОМАНД ====================

# ---------- /start (с персонализацией) ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    context.user_data['ai_mode'] = True
    user_id = user.id
    user_histories.pop(user_id, None)
    user_active_provider.pop(user_id, None)

    session = SessionLocal()
    try:
        existing_user = session.query(User).filter_by(id=user.id).first()
        db_user, created = get_or_create_user(session, user.id, user.username)
    finally:
        session.close()

    if created:
        greeting = (
            f"А, {user.first_name}. Новый участник. Я Амадеус — цифровая копия Курису Макисе. "
            f"Надеюсь, ты не будешь тратить моё время попусту. Спрашивай по делу, я слушаю."
        )
    else:
        greeting = (
            f"Снова ты, {user.first_name}. Надеюсь, на этот раз у тебя есть что-то интересное. "
            f"Я всё та же — Амадеус. Не заставляй меня повторять одно и то же."
        )

    await update.message.reply_text(greeting)
    try:
        await context.bot.send_sticker(chat_id=update.effective_chat.id, sticker=STICKER_START)
    except TelegramError as e:
        logger.warning(f"Не удалось отправить стартовый стикер: {e}")

# ---------- /help (HTML) ----------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
<b>Раз уж тебе нужна инструкция — вот список команд. Постарайся запомнить с первого раза.</b>

<b>Основные:</b>
/start — начать заново и сбросить нашу переписку
/help — то, что ты сейчас читаешь
/rules — правила сообщества
/profile — твой профиль
/anketa — подать анкету персонажа
/exit_ai — отключить меня от диалога
/reset_ai — стереть историю нашего разговора
/remember — попросить меня запомнить факт о тебе
/forget_me — стереть всё, что я о тебе запомнила

<b>Анкеты:</b>
/send_anketa — отправить собранную анкету на модерацию (после /anketa)

<b>Для администрации:</b>
/warn — выдать предупреждение
/deletemessages — удалить сообщения пользователя
/addanketnik — назначить анкетника

Если и этого недостаточно — обратись к администрации, я не справочная служба.
"""
    await update.message.reply_text(help_text, parse_mode='HTML')

# ---------- /profile ----------
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    session = SessionLocal()
    try:
        db_user, _ = get_or_create_user(session, user.id, user.username)
        roles = session.query(Role).filter_by(user_id=db_user.id).all()
        roles_text = ", ".join([role.name for role in roles]) if roles else "Нет ролей"
        profile_text = f"""
Хочешь свериться с данными? Вот что у меня есть на тебя.

ID: <code>{db_user.id}</code>
Имя: {user.first_name}
Username: @{user.username or 'не указан'}

Роли: {roles_text}
Статус: {db_user.status_rp}

Анкета: {'заполнена' if db_user.anketa_requests else 'не заполнена — самое время этим заняться'}

Что я о тебе помню: {', '.join(db_user.facts) if db_user.facts else 'пока ничего особенного'}
"""
        await update.message.reply_text(profile_text, parse_mode='HTML')
    finally:
        session.close()

# ---------- /rules ----------
async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Вот правила сообщества. Ознакомься, прежде чем действовать необдуманно:\n"
        "https://telegra.ph/Konstituciya-Omniversa-05-15"
    )

# ---------- /lore ----------
async def lore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "Если тебе интересна история — вот события Омниреальности."
    keyboard = [[InlineKeyboardButton("Война Дума", url="https://telegra.ph/Vojna-Duma-07-27")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)

# ---------- /feedback ----------
async def feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    text = ' '.join(context.args)
    if not text:
        await update.message.reply_text(
            "Если есть жалоба или предложение — пиши так:\n"
            "<code>/feedback текст твоей жалобы или предложения</code>\n\n"
            "Например:\n"
            "<code>/feedback хочу, чтобы добавили команду для розыгрышей</code>",
            parse_mode='HTML'
        )
        return
    ADMIN_CHAT_ID = DEVELOPER_IDS[0] if DEVELOPER_IDS else 5150559970
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"📩 *Новое обращение!*\n\n👤 От: @{user.username or user.first_name}\n🆔 ID: <code>{user.id}</code>\n\n📝 Текст:\n{text}",
        parse_mode='HTML'
    )
    await update.message.reply_text("Передала твоё сообщение администрации. Дальше — не моя забота.")

# ---------- /cancel ----------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Операция отменена. Возвращаюсь в обычный режим.")

# ==================== НОВАЯ СИСТЕМА АНКЕТ (с медиа) ====================

# ==================== НОВАЯ СИСТЕМА АНКЕТ (локальное хранение в памяти) ====================

# Анкеты хранятся в оперативной памяти процесса
anketa_store = {}


async def anketa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало создания анкеты (сбор частей)"""
    user = update.effective_user
    if not user:
        return

    # Проверка бана — тут БД не мешает, оставляем
    session = SessionLocal()
    try:
        db_user, _ = get_or_create_user(session, user.id, user.username)
        if db_user.is_banned:
            await update.message.reply_text("Тебе сюда нельзя. Ты забанен.")
            return
    finally:
        session.close()

    # Проверяем, нет ли уже нерассмотренной анкеты
    for ank in anketa_store.values():
        if ank["user_id"] == user.id and ank["status"] == "pending":
            await update.message.reply_text("У тебя уже есть анкета на рассмотрении. Наберись терпения.")
            return

    context.user_data['anketa_step'] = 'collecting'
    context.user_data['anketa_items'] = []

    await update.message.reply_text(
        "📝 <b>Создание анкеты</b>\n\n"
        "Отправляй части анкеты по очереди. Можно использовать текст, фото, видео, GIF, документы.\n\n"
        "Когда закончишь, напиши:\n"
        "<code>/send_anketa</code> — для отправки на модерацию\n"
        "<code>/cancel</code> — для отмены\n\n"
        "<b>Отправь первый блок:</b>",
        parse_mode='HTML'
    )


async def anketa_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('anketa_step') != 'collecting':
        return

    user = update.effective_user
    if not user:
        return

    item = {
        "type": "text",
        "text": update.message.text or update.message.caption or "",
        "file_id": None,
        "message_id": update.message.message_id,
        "sender": user.id
    }

    if update.message.photo:
        item["type"] = "photo"
        item["file_id"] = update.message.photo[-1].file_id
        item["text"] = update.message.caption or ""
    elif update.message.video:
        item["type"] = "video"
        item["file_id"] = update.message.video.file_id
        item["text"] = update.message.caption or ""
    elif update.message.document:
        item["type"] = "document"
        item["file_id"] = update.message.document.file_id
        item["text"] = update.message.caption or ""
    elif update.message.animation:
        item["type"] = "animation"
        item["file_id"] = update.message.animation.file_id
        item["text"] = update.message.caption or ""
    elif update.message.text and not update.message.text.startswith('/'):
        item["type"] = "text"
        item["text"] = update.message.text
    else:
        return

    if item["type"] == "text" and not item["text"].strip():
        await update.message.reply_text("⚠️ Пустое сообщение. Отправь что-то содержательное.")
        return

    context.user_data['anketa_items'].append(item)
    total = len(context.user_data['anketa_items'])
    await update.message.reply_text(
        f"✅ Часть анкеты сохранена ({total} шт.)\n\n"
        f"Продолжай отправлять части.\n"
        f"Для отправки напиши /send_anketa"
    )


async def send_anketa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    items = context.user_data.get('anketa_items', [])
    if not items:
        await update.message.reply_text(
            "⚠️ Анкета пуста!\n"
            "Напиши /anketa и добавь хотя бы один блок."
        )
        return

    # Сохраняем анкету в память
    anketa_id = str(uuid.uuid4())
    anketa_store[anketa_id] = {
        "user_id": user.id,
        "username": user.username or user.first_name,
        "items": items,
        "status": "pending",
        "created_at": datetime.datetime.now().isoformat(),
        "moderated_by": None,
        "moderated_at": None,
    }

    # Отправляем модераторам
    for mod_id in DEVELOPER_IDS:
        try:
            await context.bot.send_message(
                chat_id=mod_id,
                text=f"📋 <b>Новая анкета!</b>\n\n"
                     f"👤 От: @{user.username or user.first_name}\n"
                     f"🆔 ID: <code>{user.id}</code>\n\n"
                     f"📎 Всего частей: {len(items)}\n\n"
                     f"👇 Части анкеты отправлены ниже.",
                parse_mode='HTML'
            )

            media_group = []
            text_parts = []

            for item in items:
                if item["type"] == "text":
                    text_parts.append(item["text"])
                elif item["type"] in ("photo", "video", "animation", "document"):
                    if len(media_group) < 10:
                        if item["type"] == "photo":
                            media_group.append(InputMediaPhoto(media=item["file_id"], caption=item["text"] if item["text"] else None))
                        elif item["type"] == "video":
                            media_group.append(InputMediaVideo(media=item["file_id"], caption=item["text"] if item["text"] else None))
                        elif item["type"] == "animation":
                            media_group.append(InputMediaVideo(media=item["file_id"], caption=item["text"] if item["text"] else None))
                        elif item["type"] == "document":
                            media_group.append(InputMediaDocument(media=item["file_id"], caption=item["text"] if item["text"] else None))

            if media_group:
                await context.bot.send_media_group(chat_id=mod_id, media=media_group)

            if text_parts:
                await context.bot.send_message(
                    chat_id=mod_id,
                    text="📝 <b>Текст анкеты:</b>\n\n" + "\n\n---\n\n".join(text_parts),
                    parse_mode='HTML'
                )

            keyboard = [
                [
                    InlineKeyboardButton("✅ Одобрить", callback_data=f"anketa_approve_{anketa_id}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"anketa_reject_{anketa_id}"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await context.bot.send_message(
                chat_id=mod_id,
                text="📌 <b>Действия с анкетой:</b>",
                parse_mode='HTML',
                reply_markup=reply_markup
            )

        except Exception as e:
            logger.error(f"Ошибка отпрavки модератору {mod_id}: {e}")

    await update.message.reply_text("✅ Анкета отправлена на модерацию. Жди решения.")
    context.user_data.pop('anketa_step', None)
    context.user_data.pop('anketa_items', None)


async def anketa_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or (user.id not in DEVELOPER_IDS and user.id not in anketnik_ids):
        await update.message.reply_text("⛔ У вас нет прав для просмотра анкет.")
        return

    pending = [(ank_id, ank) for ank_id, ank in anketa_store.items() if ank["status"] == "pending"]
    if not pending:
        await update.message.reply_text("📭 Нет анкет на модерации.")
        return

    for anketa_id, ank in pending:
        keyboard = [
            [
                InlineKeyboardButton("✅ Одобрить", callback_data=f"anketa_approve_{anketa_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"anketa_reject_{anketa_id}"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        preview_lines = []
        for item in ank["items"]:
            if item["type"] == "text":
                preview_lines.append(item["text"])
            else:
                preview_lines.append(f"[{item['type']}] {item['text']}" if item["text"] else f"[{item['type']}]")
        preview = "\n".join(preview_lines)
        if len(preview) > 500:
            preview = preview[:500] + "..."

        await update.message.reply_text(
            f"📋 <b>Анкета</b>\n\n"
            f"👤 Пользователь: @{ank['username'] or ank['user_id']}\n"
            f"🆔 ID: <code>{ank['user_id']}</code>\n"
            f"🕒 Создана: {ank['created_at']}\n\n"
            f"📝 Текст:\n{preview}",
            parse_mode='HTML',
            reply_markup=reply_markup
        )


async def anketa_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    if user.id not in DEVELOPER_IDS and user.id not in anketnik_ids:
        await query.edit_message_text("⛔ У вас нет прав для модерации анкет.")
        return

    data = query.data  # формат: anketa_approve_<id> / anketa_reject_<id>
    parts = data.split('_')
    if len(parts) < 3:
        await query.edit_message_text("❌ Некорректные данные кнопки.")
        return

    action = parts[1]
    anketa_id = '_'.join(parts[2:])

    if action not in ("approve", "reject"):
        await query.edit_message_text("❌ Неизвестное действие.")
        return

    ank = anketa_store.get(anketa_id)
    if not ank:
        await query.edit_message_text(
            "❌ Анкета не найдена в памяти. Возможно, бот перезапускался и анкеты обнулились."
        )
        return

    if ank["status"] != "pending":
        await query.edit_message_text(
            f"ℹ️ Анкета уже обработана ранее (статус: {ank['status']})."
        )
        return

    if action == "approve":
        ank["status"] = "approved"
        ank["moderated_by"] = user.id
        ank["moderated_at"] = datetime.datetime.now().isoformat()
        await query.edit_message_text("✅ Анкета одобрена.")
        await context.bot.send_message(
            chat_id=ank["user_id"],
            text="✅ Твою анкету одобрили. Не жди, что я буду тебя хвалить за это — но справился неплохо."
        )
        try:
            await context.bot.send_sticker(chat_id=ank["user_id"], sticker=STICKER_ANKETA_APPROVE)
        except TelegramError as e:
            logger.warning(f"Не удалось отправить стикер одобрения анкеты: {e}")
    else:
        ank["status"] = "rejected"
        ank["moderated_by"] = user.id
        ank["moderated_at"] = datetime.datetime.now().isoformat()
        await query.edit_message_text("❌ Анкета отклонена.")
        await context.bot.send_message(
            chat_id=ank["user_id"],
            text="❌ Твою анкету отклонили. Попробуй ещё раз — и в этот раз подумай, прежде чем писать."
        )


async def add_anketnik(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id not in DEVELOPER_IDS:
        await update.message.reply_text("⛔ Только для разработчиков.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Использование: /addanketnik @username или /addanketnik ID")
        return

    session = SessionLocal()
    try:
        arg = context.args[0].replace("@", "")
        target = None
        if arg.isdigit():
            target = session.query(User).filter_by(id=int(arg)).first()
        else:
            target = session.query(User).filter_by(username=arg).first()

        if not target:
            await update.message.reply_text("❌ Пользователь не найден. Попросите его написать /start боту.")
            return

        # Главное — добавляем в локальный список
        anketnik_ids.add(target.id)
        # В БД тоже отметим для совместимости
        target.is_anketnik = True
        session.commit()

        await update.message.reply_text(
            f"✅ Пользователь @{target.username or target.id} назначен анкетником!"
        )
    finally:
        session.close()

async def warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("⛔ У вас нет прав.")
        return
    if not context.args:
        await update.message.reply_text("⚠️ Использование: /warn @username [причина]")
        return
    target_username = context.args[0]
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "Причина не указана"
    await update.message.reply_text(f"⚠️ Пользователю {target_username} выдано предупреждение.\nПричина: {reason}")

async def deletemessages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("⛔ У вас нет прав.")
        return
    if not context.args:
        await update.message.reply_text("⚠️ Использование: /deletemessages @username [количество]")
        return
    target_username = context.args[0]
    count = int(context.args[1]) if len(context.args) > 1 else 5
    await update.message.reply_text(f"🗑️ Удалено {count} последних сообщений пользователя {target_username}.")

async def remember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    if not context.args:
        await update.message.reply_text(
            "⚠️ Использование: <code>/remember факт о себе</code>\n"
            "Например: <code>/remember люблю классическую музыку</code>",
            parse_mode='HTML'
        )
        return
    fact = " ".join(context.args)
    add_user_fact(user.id, fact)
    await update.message.reply_text("Записала. Учту это на будущее.")

async def forget_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    clear_user_facts(user.id)
    await update.message.reply_text("Всё, что я о тебе запомнила, удалено.")

async def exit_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['ai_mode'] = False
    await update.message.reply_text("Хорошо, отключаюсь. Вернуть меня — /start.")

async def reset_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_histories.pop(user_id, None)
    user_active_provider.pop(user_id, None)
    await update.message.reply_text("История стёрта. Начнём с чистого листа.")

# ==================== УНИВЕРСАЛЬНЫЙ ОБРАБОТЧИК ТЕКСТА (с AI) ====================
async def handle_all_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    if context.user_data.get('anketa_step') == 'collecting':
        await anketa_collect(update, context)
        return

    if not context.user_data.get('ai_mode', True):
        await update.message.reply_text(
            "Отключилась по твоей же просьбе. Вернуть меня — /start."
        )
        return

    session = SessionLocal()
    try:
        get_or_create_user(session, user.id, user.username)
    finally:
        session.close()

    text = update.message.text
    if not text:
        return

    # Автоматически выцепляем базовые факты о участнике из сообщения
    for fact in extract_facts_from_text(text):
        add_user_fact(user.id, fact)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    answer = await ask_ai(text, user.id, user.first_name)
    logger.info(f"Полный AI ответ: {answer}")  # ← логируем полностью
    clean_answer, emotion_key = parse_emotion_tag(answer)
    await send_with_abzats(update.message, clean_answer)
    await send_emotion_sticker(context.bot, update.effective_chat.id, emotion_key)

# ==================== ОБРАБОТЧИК МЕДИА (для сбора анкеты) ====================
async def media_collector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('anketa_step') == 'collecting':
        await anketa_collect(update, context)

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Такой команды не существует. Загляни в /help, если совсем потерялся.")

# ==================== ОЧИСТКА НЕАКТИВНЫХ ПОЛЬЗОВАТЕЛЕЙ ====================
INACTIVE_DAYS_THRESHOLD = 60          # ~2 месяца
CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60  # проверяем раз в сутки

def cleanup_inactive_users():
    session = SessionLocal()
    try:
        threshold_date = datetime.datetime.now() - datetime.timedelta(days=INACTIVE_DAYS_THRESHOLD)
        inactive_users = (
            session.query(User)
            .filter(
                User.last_seen.isnot(None),
                User.last_seen < threshold_date,
                User.is_developer == False,
                User.is_moderator == False,
                User.is_anketnik == False,
            )
            .all()
        )
        count = len(inactive_users)
        for u in inactive_users:
            user_histories.pop(u.id, None)
            user_active_provider.pop(u.id, None)
            session.delete(u)  # каскадно удалит роли, посты, анкеты и т.д.
        session.commit()
        if count:
            logger.info(f"Очистка неактивных: удалено {count} пользователей (неактивны > {INACTIVE_DAYS_THRESHOLD} дней).")
        else:
            logger.info("Очистка неактивных: удалять некого.")
    except Exception as e:
        logger.error(f"Ошибка при очистке неактивных пользователей: {e}")
        session.rollback()
    finally:
        session.close()

async def cleanup_inactive_users_loop():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            cleanup_inactive_users()
        except Exception as e:
            logger.error(f"Сбой цикла очистки неактивных: {e}")

# ==================== FLASK ДЛЯ HEALTHCHECK (Render) ====================
from flask import Flask
import threading

flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return "OK", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=10000)

async def set_commands(application: Application):
    commands = [
        BotCommand("start", "Запустить бота и начать диалог с Амадеусом"),
        BotCommand("help", "Показать список команд"),
        BotCommand("profile", "Посмотреть свой профиль"),
        BotCommand("anketa", "Создать анкету персонажа (по частям)"),
        BotCommand("send_anketa", "Отправить собранную анкету на модерацию"),
        BotCommand("anketa_review", "Просмотр анкет на модерацию (для анкетников)"),
        BotCommand("exit_ai", "Выйти из режима общения с ИИ"),
        BotCommand("reset_ai", "Сбросить историю диалога с ИИ"),
        BotCommand("remember", "Попросить бота запомнить факт о тебе"),
        BotCommand("forget_me", "Стереть все сохранённые о тебе факты"),
        BotCommand("rules", "Показать правила сообщества"),
        BotCommand("lore", "История Омниреальности"),
        BotCommand("feedback", "Отправить отзыв или жалобу"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Команды бота установлены через set_my_commands")

async def post_init(application: Application):
    await set_commands(application)
    asyncio.create_task(cleanup_inactive_users_loop())
    logger.info(f"Запущена фоновая очистка неактивных пользователей (порог: {INACTIVE_DAYS_THRESHOLD} дней).")

def main():
    create_tables()
    threading.Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("profile", profile))
    application.add_handler(CommandHandler("anketa", anketa))
    application.add_handler(CommandHandler("send_anketa", send_anketa))
    application.add_handler(CommandHandler("anketa_review", anketa_review))
    application.add_handler(CommandHandler("warn", warn))
    application.add_handler(CommandHandler("deletemessages", deletemessages))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("rules", rules))
    application.add_handler(CommandHandler("lore", lore))
    application.add_handler(CommandHandler("feedback", feedback))
    application.add_handler(CommandHandler("addanketnik", add_anketnik))
    application.add_handler(CommandHandler("exit_ai", exit_ai))
    application.add_handler(CommandHandler("reset_ai", reset_ai))
    application.add_handler(CommandHandler("remember", remember))
    application.add_handler(CommandHandler("forget_me", forget_me))

    application.add_handler(CallbackQueryHandler(anketa_callback, pattern="^anketa_"))

    # Убрана отладочная функция sticker_debug

    application.add_handler(MessageHandler(
        (filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.ANIMATION) & ~filters.COMMAND,
        media_collector
    ))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_all_text))
    application.add_handler(MessageHandler(filters.COMMAND, unknown))

    application.post_init = post_init

    logger.info("Бот Омниверс с Амадеусом запущен (лимит токенов = 1024, убрано ограничение на длину ответов).")
    application.run_polling()

if __name__ == "__main__":
    main()
