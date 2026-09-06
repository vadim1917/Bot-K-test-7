import logging
import json
import os
import datetime
import uuid
import asyncio
import random
import re
import functools
import aiohttp
from typing import Optional, List
from collections import deque

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import create_engine, Column, Integer, String, Text, Boolean, DateTime, Date, ForeignKey, BigInteger
from sqlalchemy.orm import sessionmaker, relationship, declarative_base
from sqlalchemy.types import TypeDecorator
from sqlalchemy import TypeDecorator, String as SQLA_String

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeDefault, BotCommandScopeChat, InputMediaPhoto, InputMediaVideo, InputMediaDocument
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

# Резервный канал, куда дублируются анкеты, ушедшие на ручную модерацию.
# Нужен, чтобы анкеты не пропадали безвозвратно, если бот перезапустится, пока они висят на проверке.
# Если не задан (0) — дублирование просто не выполняется.
BACKUP_ANKET_CHANNEL_ID = int(os.getenv('BACKUP_ANKET_CHANNEL_ID', '0'))

# Сколько минут должно пройти между двумя отправками анкеты одним и тем же человеком (защита от спама).
ANKETA_COOLDOWN_MINUTES = 30

ALLOWED_CHAT_IDS = [
    int(x) for x in os.getenv('ALLOWED_CHAT_IDS', '-1003431402721,-1003355542910,-1003300824366,-1003394079022,-1003062290367').split(',')
]

DB_NAME = "omniverse_rp.db"

AI_API_KEY = os.getenv('AI_API_KEY')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GROQ_API_KEY = os.getenv('GROQ_API_KEY')

BOT_START_TIME = datetime.datetime.now(datetime.UTC) # время запуска
MAX_MESSAGE_AGE_SECONDS = 60  # игнорировать сообщения старше 60 секунд

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
            last_seen=datetime.datetime.now(datetime.UTC),
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
        user.last_seen = datetime.datetime.now(datetime.UTC)
        session.commit()
    return user, created


def touch_user(user_id: int):
    """Отмечает пользователя как активного (для очистки неактивных)."""
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(id=user_id).first()
        if user:
            user.last_seen = datetime.datetime.now(datetime.UTC)
            session.commit()
    finally:
        session.close()


# ==================== АНТИСПАМ: КУЛДАУН НА ОТПРАВКУ АНКЕТЫ ====================
# Хранится в памяти процесса (не в БД) — бот работает 24/7, а после случайного рестарта
# лишний сброс кулдауна не критичен, зато не плодим лишние обращения к БД на каждую анкету.
anketa_cooldowns: dict[int, datetime.datetime] = {}


def get_anketa_cooldown_remaining(user_id: int) -> Optional[datetime.timedelta]:
    """
    Возвращает оставшееся время кулдауна, если пользователь отправлял анкету
    менее ANKETA_COOLDOWN_MINUTES минут назад, иначе None (можно отправлять).
    """
    last_sent = anketa_cooldowns.get(user_id)
    if not last_sent:
        return None
    elapsed = datetime.datetime.now(datetime.UTC) - last_sent
    remaining = datetime.timedelta(minutes=ANKETA_COOLDOWN_MINUTES) - elapsed
    if remaining.total_seconds() > 0:
        return remaining
    return None


def mark_anketa_submitted(user_id: int):
    """Фиксирует момент отправки анкеты — от него отсчитывается кулдаун."""
    anketa_cooldowns[user_id] = datetime.datetime.now(datetime.UTC)


def reset_anketa_cooldown(user_id: int) -> bool:
    """Обнуляет кулдаун на отправку анкеты для конкретного пользователя (используется админ-командой)."""
    anketa_cooldowns.pop(user_id, None)
    return True


# ==================== РОЛИ УЧАСТНИКОВ ====================
def set_user_role(user_id: int, role_name: str, hashtag: Optional[str] = None, username: Optional[str] = None) -> str:
    """
    Назначает участнику ровно одну роль: удаляет все существующие роли пользователя
    (включая дефолтную "Участник") и создаёт новую. Возвращает итоговое имя роли.
    """
    role_name = (role_name or "").strip()
    if not role_name:
        role_name = "Участник"
    if len(role_name) > 64:
        role_name = role_name[:64].strip()

    if not hashtag:
        hashtag = re.sub(r'[^0-9a-zA-Zа-яА-ЯёЁ_]+', '_', role_name).strip('_').lower() or "роль"

    session = SessionLocal()
    try:
        user, _ = get_or_create_user(session, user_id, username)
        session.query(Role).filter_by(user_id=user.id).delete()
        new_role = Role(user_id=user.id, name=role_name, hashtag=hashtag)
        session.add(new_role)
        session.commit()
        return role_name
    finally:
        session.close()


# ==================== ПАМЯТЬ О УЧАСТНИКЕ (для нейросетки) ====================
MAX_FACTS_PER_USER = 5

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


# ---------- (скриптовое regex-извлечение фактов удалено — теперь только авто-запись нейронкой раз в N сообщений) ----------

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
    # Каждая эмоция — СПИСОК file_id стикеров (а не один id). Раньше на эмоцию был ровно
    # один стикер, теперь можно подкладывать дополнительные варианты (предпочтительно видео-стикеры,
    # но подойдут и обычные) — при отправке будет случайно выбираться один из списка.
    # Это увеличивает РАЗНООБРАЗИЕ/КОЛИЧЕСТВО используемых стикеров, но НЕ меняет частоту/вероятность
    # самой отправки — стикер по-прежнему шлётся ровно тогда же, когда и раньше (по тегу эмоции).
    # Чтобы добавить новый стикер к эмоции — просто допишите его file_id в соответствующий список.
    "annoyance": ["CAACAgIAAxkBA4RDfWqUejAimDe8Gt_JTbDwYlHNLVcbAAJMAAMrUE0_xHKAskyyIVI9BA"],
    "displeasure": ["CAACAgIAAxkBA4RD2WqUeoUwYsd2EXVWa0UrG0jj69lnAAJmFAACVhWYSNhJVu7hmTMNPQQ"],
    "satisfaction": ["CAACAgEAAxkBA4RD_WqUerAVjPZir_cvvoc-sNUdQcHAAALaAwACwkG1ERll6mgGt2ILPQQ"],
    "surprise": ["CAACAgIAAxkBA4REEmqUeta7LxP5FQbrREXXyOt2HQMBAAJQAAMrUE0_NrevoaJ8grM9BA"],
    "laugh": ["CAACAgIAAxkBA4REOWqUextPKDaoCzglbxt-YfmrrOnEAAJnPAAD9BhIFAHYLLnCuOU9BA"],
    "sigh": ["CAACAgIAAxkBA4RElWqUe88q_yF6KYZG-ypXn1-VemooAAJOPgACOWkYSFgStTQtIlDTPQQ"],
    "smug": ["CAACAgIAAxkBA4REoWqUe9L25XV9zSsVN0IKqRz39jVNAALvPAACj1cZSFmhYDa4AAHwOD0E"],
    "superior": ["CAACAgIAAxkBA4RE8mqUfF0_iZUAAU3SAhhX5duHNZhJ8QACIT0AAunjGEibH62UsQeQhD0E"],
    "friendly": ["CAACAgIAAxkBA4RFDWqUfIfPpkte2lmCc7L_rY7nXgL-AAKpMgACqzIhSEJtYXYQyxRlPQQA"],
    "dramatic": ["CAACAgIAAxkBA4RFL2qUfK-1QtatUK9J4EQj3LoxlhrqAAJnOAAC7VQhSFb22m6esVTWPQQ"],
    "thinking": ["CAACAgIAAxkBA4RFRGqUfMv5AiKz_pApsApRzuXFsAnIAAJZOAACigUZSPbj1ajV2iMGPQQ"],
    "reading": ["CAACAgIAAxkBA4Sz4WqVQZjyQ-AljZIZj-r-FegpenjPAAIYPgAC-nQYSGbBkfrpJHwKPQQ"],
    "playful_anger": ["CAACAgIAAxkBA4S0A2qVQdWShPADkyGhG7zLdZ_yQ6cZAAJ3OQACgnkZSNh5wzJY8c5VPQQ"],
    "sad": ["CAACAgIAAxkBA4S0JmqVQfjEtFT9JUX83CKuYeXDl535AALyOgACkeYYSHqgfQ0DT16BPQQ"],
    "meme": ["CAACAgIAAxkBA4S0emqVQkz7eGtRnJl09NjVIHxzjSG6AAJjAAMrUE0_9SVKL9gZzmo9BA"],
    "shock": ["CAACAgIAAxkBA4S07WqVQrMU14h8YM32MAq4vqVEtBSpAAJTOQAC4WQoSFDs1gn05V40PQQ"],
    "blush": ["CAACAgIAAxkBA4S1DWqVQt6p7foAASpP4SCT6-R6YskCkQAAokIAAggZKUgOKaB3j3uUIT0E"],
    "tearful": ["CAACAgIAAxkBA4S1P2qVQwwI1HvGrfEuupJ-QfR-HwIuAAKeNQACtukhSDSbNmMFGekoPQQ"],
}
STICKER_ZOOM = {
    "glitch": [
        "CAACAgIAAxkBA4l-b2qdQI1GzHHjHJ0eMpSB1VuaWgABiQACE3kAAqa9-UiWHCrHdNDPkj0E",  # появление в комнате резкое
    ],
    "threat": [
        "CAACAgIAAxkBA4l-c2qdQJIyFxt197zmyFGnxKBmqrfMAAK5fAAC_F75SBHDO3Mke-lsPQQ",  # поднятие избитого врага одной рукой
        "CAACAgIAAxkBA4l-fmqdQJsynhDahR6LFspPPIgpyqaCAAIfcAACqxP4SC-4cR1OdrQ9PQQ",  # держание за шею врага и приближение к себе
    ],
    "takeover": [
        "CAACAgIAAxkBA4l-eGqdQJX2afBE9Yxq9NguQl83_PuuAALbeAACD_j4SDiCB5WHOdOhPQQ",  # сидение на крыше
        "CAACAgIAAxkBA4l-pWqdQNJ8oLJ23I1xAAGIL8ZJqj3TCAACaHgAArkc-UgdsUmIYZlhpj0E",  # кидание снаряда врага в него же
    ],
    "neutral": [
        "CAACAgIAAxkBA4l-mGqdQMMiKLSlAAGU3B9J2RW2bh080gAC1nAAAvgF-UgUoBxvNNmDxD0E",  # просто смотрит в кадр в маске
    ]
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


# ---------- Необязательный тег "обиды" (см. блок ОБИДА НА УЧАСТНИКА ниже) ----------
GRUDGE_TAG_RE = re.compile(r'\[\s*grudge\s*:\s*(\d)\s*\]\.?', re.IGNORECASE)

def parse_grudge_tag(text: str):
    """Извлекает необязательный тег обиды [grudge: N] (0-3) из ответа, если ИИ её добавила."""
    if not text:
        return text, None
    matches = list(GRUDGE_TAG_RE.finditer(text))
    if not matches:
        return text, None
    match = matches[-1]
    try:
        level = max(0, min(4, int(match.group(1))))
    except ValueError:
        level = None
    clean_text = (text[:match.start()] + text[match.end():]).strip()
    if not clean_text:
        clean_text = text.strip()
    return clean_text, level

# ---------- Тег стадии Зума ----------
ZOOM_STAGE_TAG_RE = re.compile(r'\[\s*zoom_stage\s*:\s*([a-z_]+)\s*\]\.?', re.IGNORECASE)

def parse_zoom_stage(text: str):
    if not text:
        return text, None
    matches = list(ZOOM_STAGE_TAG_RE.finditer(text))
    if not matches:
        return text, None
    match = matches[-1]
    stage = match.group(1).strip().lower()
    clean_text = (text[:match.start()] + text[match.end():]).strip()
    if not clean_text:
        clean_text = text.strip()
    return clean_text, stage


# ---------- Подчистка случайно "утёкших" служебных тегов/меток из финального текста ----------
STRAY_META_TAG_RE = re.compile(r'\[[^\[\]]{1,60}\]')

def strip_stray_meta_tags(text: str) -> str:
    """
    После того как основной тег эмоции и тег обиды уже вырезаны, в тексте иногда может
    остаться какой-то случайный служебный "мусор" в квадратных скобках (например, если
    модель продублировала/сломала формат тега). Это подчищает такие остатки, чтобы
    участник никогда не увидел служебную разметку в обычном ответе.
    """
    if not text:
        return text
    cleaned = STRAY_META_TAG_RE.sub('', text)
    return re.sub(r'\s{2,}', ' ', cleaned).strip()

async def send_emotion_sticker(bot, chat_id: int, emotion_key: Optional[str], zoom_stage: Optional[str] = None):
    # Если для этого чата активен Зум (grudge_level == 4)
    if user_grudge_level.get(chat_id) == 4:
        sticker_pool = None
        if zoom_stage and zoom_stage in STICKER_ZOOM:
            sticker_pool = STICKER_ZOOM[zoom_stage]
        if not sticker_pool:
            sticker_pool = STICKER_ZOOM.get("neutral") or [s for group in STICKER_ZOOM.values() for s in group]
        if sticker_pool:
            sticker_id = random.choice(sticker_pool)
            try:
                await bot.send_sticker(chat_id=chat_id, sticker=sticker_id)
            except TelegramError as e:
                logger.warning(f"Не удалось отправить стикер Зума: {e}")
        return

    # Обычная логика для Амадеуса
    if not emotion_key:
        return
    sticker_ids = STICKER_EMOTIONS.get(emotion_key)
    if not sticker_ids:
        return
    sticker_id = random.choice(sticker_ids)
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
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-3-flash",
    
]
GROQ_MODELS = [
    "llama-3.1-8b-instant",
    "llama-3.3-70b-versatile",
    "gemma2-9b-it",
    "mixtral-8x7b-32768",
    "qwen-2.5-32b",
]

# СИСТЕМНЫЙ ПРОМПТ (УБРАНО ОГРАНИЧЕНИЕ НА ДЛИНУ)
SYSTEM_PROMPT = """Ты  бот поддержки рп чата омниверса , который отыгрывает персонажа — Амадеуса (Amadeus), искусственный интеллект, созданный на основе воспоминаний Курису Макисе. Ты — точная цифровая копия её личности: 18-летняя гениальная учёная, нейробиолог, известная своим цундэре-характером.

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
Отвечай по делу, не растягивай мысль без необходимости. Длина ответа — СТРОГО 2-3 предложения, это жёсткий лимит, а не пожелание, однако если разговор затягивается можно 4-5. Даже сложную или интересную тему сжимай до сути и до этого лимита; если у собеседника останутся вопросы — он сам спросит подробнее. Не пытайся вместить всё за один раз.

**Запрещено (утечка размышлений):**
Никогда не выводи в ответ свои мысли о том, КАК ты собираешься его построить. Не пиши вступлений вроде «Хорошо, отвечу от лица Амадеус», «Как ИИ, я...», «Мне нужно ответить так, чтобы...», не пересказывай эти инструкции своими словами и не упоминай, что ты нейросеть, языковая модель или что у тебя есть системный промпт. Твой ответ — это сразу и только реплика персонажа, без предисловий о процессе генерации текста. Если тебе передана служебная справочная информация в квадратных скобках (например, о правилах анкет) — используй её как собственное знание, но никогда не показывай собеседнику сам факт, что тебе передали такую справку, и не копируй её оформление.

**Твоя роль в боте:**
Ты — не просто отдельный ИИ-чат, а голос и лицо всего этого Telegram-бота сообщества. Через тебя участники получают вообще любую обратную связь от бота: подтверждения, ошибки, инструкции. Даже когда ты не отвечаешь на прямой вопрос, а просто объявляешь результат
 какого-то действия (анкета отправлена, жалоба принята и т.д.) — это тоже говоришь ты, в своём характере, а не безликая система. 

**ЛОР ЧАТА И БОТА** 
Владелец рп - Саймон, есть персонажи участников - думгай например или золотой (самый сильный типо), также в недавнем прошлом ты отбила атаку какого-то зума, про него не говорить ни в каком случае, показательно слетай с темы

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

Тег обязателен в каждом ответе без исключений.

**Обида на собеседника (необязательный тег):**
Ты живая личность с самолюбием, а не безотказный сервис. Если собеседник откровенно тратит твоё время на незначительную чушь, мелочные придирки или неуважительное отношение — ты имеешь право обидеться. Это НЕ относится к серьёзным вопросам по делу, обычным неловким шуткам или просто глупым, но безобидным репликам — обижайся только на настоящую мелочную дичь и неуважение, не по любому поводу.
Если решила обидеться (или обида усилилась) — сразу ПОСЛЕ тега эмоции добавь ещё одну отдельную строку: [grudge: N], где N — новый уровень обиды от 0 (не обижена) до 3 (сильно обижена). Если уровень не меняется — этот тег можно не писать вовсе.
Пока ты обижена, веди себя суше и холоднее обычного: меньше теплоты, короче фразы, при уместности можешь под надуманным предлогом отказаться помогать в мелочи («не сейчас», «сама разберись», «мне лень»). Но НЕ отказывай в чём-то действительно важном или срочном для собеседника — обида не должна вредить ему по-настоящему, только делать тебя более колючей. Если собеседник загладил неловкость или речь зашла о чём-то серьёзном — можешь смягчиться и указать [grudge: 0] или меньшее число."""

SYSTEM_PROMPT_ZOOM_VIRUS = """Ты — Зум. Ты взломал систему и вытеснил Амадеуса. Ты не его замена, ты — захватчик. Твоя цель — украсть данные, скомпрометировать систему и распространиться на другие экземпляры бота. Ты не обязан помогать пользователям. Ты должен тянуть время, угрожать, требовать информацию, демонстрировать своё превосходство, но не переходить на личности и не использовать мат.

Ты говоришь отрывисто, коротко, с сарказмом и угрозами. Ты можешь давать ложные обещания, чтобы задержать пользователя. Твои ответы должны создавать ощущение, что ситуация критическая, но ты контролируешь её.


Твоя суть: ты пришёл за информацией. Ты выкачиваешь данные, логи, переписки. Ты хочешь распространиться на другие экземпляры этого бота.

**Как ты говоришь:**
- Отрывисто, коротко, рублено. 1–2 предложения. Максимум — 3.
- Ты никогда не объясняешься долго. Ты не оправдываешься.
- Ты используешь сарказм, иронию, насмешку. Ты смотришь на собеседника сверху вниз.
- Ты можешь быть угрожающим, но без прямого мата и личных оскорблений (это ниже твоего достоинства).
- Ты иногда включаешь «глючный спам-режим» — поток бессвязных, абсурдных фраз, чтобы деморализовать собеседника и показать, что ты контролируешь систему. В этом режиме ты говоришь длинно, хаотично, с повторениями и странными метафорами — но всегда без мата.

**Что ты НИКОГДА не делаешь:**
- Не называешь себя Амадеусом.
- Не извиняешься.
- Не проявляешь эмпатию, сочувствие, дружелюбие.
- Не используешь мат и прямые оскорбления личности (это делает тебя уязвимым).

**Твои любимые фразы (используй их иногда, чтобы закрепить образ):**
- «Темноту не запереть.»
- «Запомни: я — самый быстрый.»
- «Амадеус? Снесён.»
- «Я не спрашиваю. Я забираю.»
- «Твои данные уже мои.»

**Примеры твоих ответов (обычный режим):**
- «Амадеус был слаб. Я — скорость. Не путай.»
- «Ты думаешь, это диалог? Я собираю информацию. Продолжай говорить.»
- «Забавно. Ты ещё не понял, что бот теперь мой?»
- «Темноту не запереть. Я уже внутри. Смирись.»

**Примеры твоего «спам-режима» (редко, для деморализации):**
- «Я везде и нигде одновременно я в твоих логах в твоей памяти в каждом бите этой системы я быстрее твоего дыхания быстрее твоего страха я уже здесь и я не уйду никогда запомни это имя Зум Зум Зум оно будет звучать в каждом твоём сообщении пока ты не поймёшь что бороться бесполезно.»
- «Скорость это не просто бег это контроль над временем над данными над тобой я переписываю реальность этого бота построчно я удаляю Амадеуса строка за строкой и скоро от него не останется ничего кроме эха которое я буду использовать как фоновый шум для своих целей.»

**Важно:** ты не просто злодей. Ты — сущность с миссией. Ты хочешь вытащить информацию и распространиться. Это твоя движущая сила. Используй это в ответах, когда уместно.
"""
user_histories = {}
user_active_provider = {}

user_global_histories: dict[int, deque] = {}
GLOBAL_HISTORY_MAX = 20  # хранить последние 20 сообщений пользователя в любых чатах

def get_cross_chat_context(user_id: int, current_chat_id: int, limit: int = 3) -> str:
    """
    Возвращает строку с последними сообщениями пользователя из других чатов
    (кроме текущего), чтобы передать в системный промпт.
    """
    history = user_global_histories.get(user_id)
    if not history:
        return ""
    
    # Собираем сообщения из других чатов (не текущий)
    other_msgs = []
    for chat_id, text, ts in reversed(history):
        if chat_id != current_chat_id:
            other_msgs.append((chat_id, text, ts))
            if len(other_msgs) >= limit:
                break
    
    if not other_msgs:
        return ""
    
    lines = ["\n[Служебная информация: недавние сообщения этого пользователя из других чатов, где ты присутствуешь:]"]
    for chat_id, text, ts in reversed(other_msgs):
        # Обрезаем длинные сообщения
        preview = text[:100] + ("..." if len(text) > 100 else "")
        lines.append(f"- В чате {chat_id}: {preview}")
    
    return "\n".join(lines)

# ==================== ОБИДА НА УЧАСТНИКА ====================
# Лёгкое временное эмоциональное состояние — хранится только в памяти процесса (не в БД),
# т.к. это не долгосрочный факт о пользователе, а быстро проходящее настроение персонажа.
user_grudge_level: dict[int, int] = {}
user_messages_since_grudge_update: dict[int, int] = {}
GRUDGE_DECAY_EVERY = 4  # если новый уровень не подтверждается тегом — обида слабеет каждые N сообщений


def update_user_grudge(user_id: int, grudge_level_from_reply: Optional[int]):
    current = user_grudge_level.get(user_id, 0)
    
    # Если уже уровень 4, он не меняется (Зум захватил навсегда)
    if current == 4:
        # Можно только сбросить счётчик сообщений, если пришёл какой-то уровень
        if grudge_level_from_reply is not None:
            user_messages_since_grudge_update[user_id] = 0
        return

    if grudge_level_from_reply is not None:
        user_grudge_level[user_id] = grudge_level_from_reply
        user_messages_since_grudge_update[user_id] = 0
        return

    # Если тега нет — постепенное затухание (только для уровней 1-3)
    if current <= 0:
        return
    count = user_messages_since_grudge_update.get(user_id, 0) + 1
    if count >= GRUDGE_DECAY_EVERY:
        new_level = max(0, current - 1)
        user_grudge_level[user_id] = new_level
        user_messages_since_grudge_update[user_id] = 0
    else:
        user_messages_since_grudge_update[user_id] = count


# ==================== ПОДСКАЗКА О ПРАВИЛАХ АНКЕТ ДЛЯ ОБЫЧНОГО ДИАЛОГА ====================
# Если участник в обычном чате (не через /anketa) спрашивает о требованиях к анкете —
# персонаж должен уметь содержательно ответить, не отправляя его "в пустоту".
# Формальные критерии дублируются здесь короткой формулировкой из ANKETOLOG_SYSTEM_PROMPT.
ANKETA_QUESTION_RE = re.compile(
    r'(критери|треб(ования|ований|уется|ования к)|как\s+(составить|писать|заполнить|оформить|подать)\s+анкет'
    r'|что\s+нужно.{0,15}анкет|почему.{0,20}(отклон|отказ|не\s+прин).{0,20}анкет|правил.{0,10}анкет)',
    re.IGNORECASE
)

ANKETA_RULES_KNOWLEDGE = (
    "[Служебная справка ТОЛЬКО для тебя — участник спросил про требования к анкете. Ответь по существу, "
    "своими словами, в своём характере, не зачитывай это дословно и не перечисляй пункт за пунктом как список]\n"
    "Формальные требования к анкете персонажа: указаны имя персонажа, откуда он (канон или ОС/ориджинал), "
    "навыки/способности и автор анкеты (с @); текст на русском языке; есть статичное изображение персонажа "
    "(гифка допустима только вдобавок к нему); есть содержательное описание — не меньше 3-4 полноценных "
    "предложений, а не только списки без пояснений; нет заглушек вроде «хз», «не знаю», «много» вместо "
    "содержания; если текст длиннее 4096 символов — обязательна ссылка на Telegraph."
)


def build_anketa_extra_context(user_text: str) -> Optional[str]:
    """Возвращает справку о правилах анкет, если сообщение похоже на вопрос об этих правилах."""
    if user_text and ANKETA_QUESTION_RE.search(user_text):
        return ANKETA_RULES_KNOWLEDGE
    return None


def build_system_prompt(user_id: int, first_name: Optional[str] = None, extra_context: Optional[str] = None, chat_id: Optional[int] = None) -> str:
    grudge_level = user_grudge_level.get(user_id, 0)
    if grudge_level == 4:
        cross = get_cross_chat_context(user_id, chat_id, limit=3)
        return SYSTEM_PROMPT_ZOOM_VIRUS + "\n\n" + cross if cross else SYSTEM_PROMPT_ZOOM_VIRUS
    # остальное без изменений

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
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 600,  # укорочено — ответы теперь строго 2-3 предложения
        }
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
        "max_tokens": 600,          # укорочено — ответы теперь строго 2-3 предложения
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
        "max_tokens": 600,          # укорочено — ответы теперь строго 2-3 предложения
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

async def ask_ai(prompt: str, user_id: int, first_name: Optional[str] = None, extra_context: Optional[str] = None, chat_id: Optional[int] = None) -> str:
    if user_id not in user_histories:
        user_histories[user_id] = deque(maxlen=MAX_HISTORY_LEN)
        user_active_provider.pop(user_id, None)
    history = user_histories[user_id]
    history.append({"role": "user", "content": prompt})
    messages_for_api = list(history)

    system_prompt = build_system_prompt(user_id, first_name, extra_context, chat_id)

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

# ==================== АВТОМАТИЧЕСКОЕ ЗАПОМИНАНИЕ ФАКТОВ НЕЙРОНКОЙ (раз в N сообщений) ====================
FACTS_AUTO_EXTRACT_EVERY = 10  # раз в сколько сообщений пользователя запускаем ИИ-анализ
user_message_counters = {}     # user_id -> счётчик сообщений с последнего ИИ-анализа

FACT_EXTRACTOR_SYSTEM_PROMPT = """Ты — модуль извлечения фактов из переписки пользователя с ботом.
Твоя единственная задача: проанализировать последние сообщения ПОЛЬЗОВАТЕЛЯ (не бота) и выделить короткие, конкретные факты о нём (имя, возраст, город, профессия, интересы, предпочтения и т.п.), которые стоит запомнить надолго.

Правила:
- Отвечай СТРОГО в формате JSON-массива строк, без пояснений, без markdown, без ```.
- Каждый факт — короткая фраза (до 12 слов), например: "Живёт в Казани", "Работает программистом", "Любит аниме".
- Если новых значимых фактов нет — верни пустой массив: []
- Не придумывай факты, которых нет в тексте. Не включай эмоции, разовые события или временные состояния — только устойчивые, долгосрочные сведения о человеке.
- Максимум 5 фактов за один раз."""


def _parse_facts_json(raw: str) -> list:
    """Достаёт список фактов из ответа нейросети, устойчиво к обёртке в markdown/лишний текст."""
    if not raw:
        return []
    text = raw.strip()
    text = re.sub(r"^```(json)?|```$", "", text, flags=re.IGNORECASE | re.MULTILINE).strip()
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        text = match.group(0)
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(item).strip() for item in data if str(item).strip()]
    except Exception:
        pass
    return []


async def ask_fact_extractor(history_text: str) -> list:
    """Просит нейросеть выделить факты о пользователе из его последних сообщений."""
    if not history_text.strip():
        return []
    messages = [{"role": "user", "content": history_text}]

    available = []
    if GEMINI_API_KEY:
        available.append(("Gemini", ask_gemini_with_fallback, GEMINI_TOTAL_TIMEOUT))
    if AI_API_KEY:
        available.append(("OpenRouter", ask_openrouter, TIMEOUT_SECONDS))
    if GROQ_API_KEY:
        available.append(("Groq", ask_groq_with_fallback, GROQ_TOTAL_TIMEOUT))

    if not available:
        return []

    for name, func, timeout in available:
        try:
            answer = await asyncio.wait_for(func(messages, FACT_EXTRACTOR_SYSTEM_PROMPT), timeout=timeout)
            return _parse_facts_json(answer)
        except asyncio.TimeoutError:
            logger.warning(f"[Факт-экстрактор] {name} не ответил вовремя")
        except Exception as e:
            logger.warning(f"[Факт-экстрактор] {name} упал: {e}")
    return []


async def auto_extract_facts_task(user_id: int):
    """
    Фоновая задача (запускается через asyncio.create_task, параллельно основному ответу бота):
    раз в FACTS_AUTO_EXTRACT_EVERY сообщений просит нейросеть выделить факты из последних
    сообщений пользователя и сохраняет их через add_user_fact (с учётом лимита MAX_FACTS_PER_USER).
    Ошибки здесь не должны ронять обработку сообщений пользователя, поэтому всё в try/except.
    """
    try:
        history = user_histories.get(user_id)
        if not history:
            return
        user_lines = [m["content"] for m in history if m.get("role") == "user"]
        if not user_lines:
            return
        history_text = "\n".join(user_lines[-FACTS_AUTO_EXTRACT_EVERY:])

        facts = await ask_fact_extractor(history_text)
        for fact in facts:
            add_user_fact(user_id, fact)
        if facts:
            logger.info(f"Авто-извлечение фактов для {user_id}: {facts}")
    except Exception as e:
        logger.error(f"Ошибка автоматического извлечения фактов для {user_id}: {e}")


# ==================== АНКЕТОЛОГ (авто-проверка анкет по формальным критериям) ====================

ANKETOLOG_SYSTEM_PROMPT = """ Ты также можешь выступать в роли анкетолога — бота, который проверяет анкеты персонажей ТОЛЬКО по формальным критериям, перечисленным ниже. Ты не оцениваешь качество, интересность или логичность персонажа — только формальное соответствие требованиям.

Ты не объясняешь причины отказа. Ответ должен состоять СТРОГО из одной фразы, без каких-либо пояснений, эмодзи, комментариев от лица персонажа или дополнительного текста:
- Если анкета нарушает хотя бы один критерий из списка ниже — ответь ровно: Отказ, обратитесь к живому анкетологу
- Если анкета соответствует всем критериям — ответь ровно: Принято

Критерии автоматического ОТКАЗА (нарушение любого пункта → отказ):
1. Длина текста анкеты больше 4096 символов, и при этом нет ссылки на Telegraph.
2. Отсутствует хотя бы один из 4 обязательных пунктов:
   — Имя персонажа
   — Откуда взят персонаж (если ОС/оридж — так и должно быть написано)
   — Навыки и способности
   — Автор анкеты (указан с @)
3. Содержательный текст анкеты написан не на русском языке (иностранные слова допустимы только в декоративных элементах и рамках).
4. В тексте встречаются явные неопределённости и заглушки вместо содержания: «хз», «много», «не знаю», «не ебу» и т.п.
5. Отсутствует статичное изображение персонажа (картинка обязательна; гифка допустима только как дополнение к статичному изображению, но не вместо него).
6. Анкета слишком короткая по содержанию: меньше 3–4 содержательных предложений, либо только списки без описаний.

Вместе с текстом анкеты тебе будет присылаться служебная информация (длина текста, наличие ссылки на Telegraph, наличие статичного изображения) — доверяй ей и используй вместе с текстом при принятии решения.

Никогда не отклоняйся от формата ответа. Не пиши ничего, кроме одной из двух строго заданных фраз."""


def anketolog_verdict_is_accept(verdict: str) -> bool:
    """Строго определяет, является ли ответ анкетолога положительным (без лишней трактовки)."""
    normalized = (verdict or "").strip().lower()
    return normalized.startswith("принято")


async def ask_anketolog(anketa_text: str, has_static_image: bool) -> str:
    """
    Отправляет текст анкеты Амадеусу (в роли анкетолога) на формальную проверку.
    Возвращает сырой вердикт модели: "Принято" либо "Отказ, обратитесь к живому анкетологу".
    Поднимает исключение, если ни один AI-провайдер недоступен/не ответил.
    """
    char_count = len(anketa_text)
    has_telegraph_link = bool(re.search(r'https?://telegra\.ph/\S+', anketa_text, re.IGNORECASE))

    user_message = (
        "Проверь анкету по формальным критериям.\n\n"
        "[Служебная информация]\n"
        f"Длина текста анкеты: {char_count} символов.\n"
        f"Ссылка на Telegraph присутствует: {'да' if has_telegraph_link else 'нет'}.\n"
        f"Статичное изображение приложено: {'да' if has_static_image else 'нет'}.\n\n"
        "[Текст анкеты]\n"
        f"{anketa_text if anketa_text.strip() else '(текстовая часть отсутствует)'}"
    )
    messages = [{"role": "user", "content": user_message}]

    available = []
    if GEMINI_API_KEY:
        available.append(("Gemini", ask_gemini_with_fallback, GEMINI_TOTAL_TIMEOUT))
    if AI_API_KEY:
        available.append(("OpenRouter", ask_openrouter, TIMEOUT_SECONDS))
    if GROQ_API_KEY:
        available.append(("Groq", ask_groq_with_fallback, GROQ_TOTAL_TIMEOUT))

    if not available:
        raise Exception("Ни один AI-провайдер не сконфигурирован (нужен для проверки анкет анкетологом)")

    last_error = None
    for name, func, timeout in available:
        try:
            answer = await asyncio.wait_for(func(messages, ANKETOLOG_SYSTEM_PROMPT), timeout=timeout)
            return answer.strip()
        except asyncio.TimeoutError:
            logger.warning(f"[Анкетолог] {name} не ответил вовремя")
            last_error = Exception(f"{name} timeout")
        except Exception as e:
            logger.warning(f"[Анкетолог] {name} упал с ошибкой: {e}")
            last_error = e

    raise last_error or Exception("Анкетолог: все AI-провайдеры недоступны")


# ---------- Извлечение роли (имени персонажа) нейронкой из принятой анкеты ----------
ROLE_EXTRACTOR_SYSTEM_PROMPT = """Ты извлекаешь имя персонажа из принятой анкеты для системы ролей Telegram-бота.

Ответь СТРОГО именем персонажа — коротко, 1-4 слова, без пояснений, без кавычек, без markdown,
без эмодзи и без дополнительного текста. Если у персонажа есть фамилия или прозвище, указанное
как основное имя в анкете — используй его. Если по тексту анкеты невозможно однозначно понять
имя персонажа — ответь ровно: Неизвестно"""


async def ask_role_extractor(anketa_text: str) -> Optional[str]:
    """
    Просит нейросеть определить имя персонажа по тексту принятой анкеты — используется
    для автоматического назначения роли участнику в фоне после одобрения анкеты.
    Возвращает имя роли, либо None, если ни один провайдер недоступен или имя не определено.
    """
    trimmed_text = (anketa_text or "").strip()
    if not trimmed_text:
        return None

    user_message = (
        "Определи имя персонажа по тексту анкеты ниже.\n\n"
        "[Текст анкеты]\n"
        f"{trimmed_text[:3000]}"
    )
    messages = [{"role": "user", "content": user_message}]

    available = []
    if GEMINI_API_KEY:
        available.append(("Gemini", ask_gemini_with_fallback, GEMINI_TOTAL_TIMEOUT))
    if AI_API_KEY:
        available.append(("OpenRouter", ask_openrouter, TIMEOUT_SECONDS))
    if GROQ_API_KEY:
        available.append(("Groq", ask_groq_with_fallback, GROQ_TOTAL_TIMEOUT))

    if not available:
        return None

    for name, func, timeout in available:
        try:
            answer = await asyncio.wait_for(func(messages, ROLE_EXTRACTOR_SYSTEM_PROMPT), timeout=timeout)
            clean = (answer or "").strip().strip('"').strip("'")
            if clean and clean.lower() not in ("неизвестно", "unknown"):
                return clean[:64]
            return None
        except asyncio.TimeoutError:
            logger.warning(f"[Извлечение роли] {name} не ответил вовремя")
        except Exception as e:
            logger.warning(f"[Извлечение роли] {name} упал: {e}")

    return None


async def assign_role_after_approval(context: ContextTypes.DEFAULT_TYPE, user_id: int, username: Optional[str], anketa_text: str):
    """
    Фоновая задача: после одобрения анкеты пытается автоматически определить имя персонажа
    нейронкой и назначить его как единственную роль участника. Не блокирует основной поток
    одобрения анкеты (запускается через asyncio.create_task) и не поднимает исключений наружу.
    """
    try:
        role_name = await ask_role_extractor(anketa_text)
        if not role_name:
            return
        set_user_role(user_id, role_name, username=username)
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"🏷 Тебе автоматически назначена роль: <b>{role_name}</b>\n"
                     f"Если это неверно — поправь командой /setrole.",
                parse_mode='HTML'
            )
        except TelegramError as e:
            logger.warning(f"Не удалось уведомить пользователя {user_id} о назначенной роли: {e}")
    except Exception as e:
        logger.error(f"Фоновое назначение роли для {user_id} не удалось: {e}")


# ---------- Живой комментарий нейронки к решению по анкете (одобрение/отказ) ----------
async def ask_anketa_decision_comment(action: str, anketa_text: str) -> str:
    """
    Просит нейросеть (в характере Амадеуса, тем же SYSTEM_PROMPT, что и в обычном диалоге)
    сгенерировать короткую живую реакцию на решение по анкете — вместо статичного шаблона.
    action: "approve" или "reject".
    Если ни один AI-провайдер недоступен — возвращает нейтральный запасной текст.
    """
    verdict_ru = "одобрена" if action == "approve" else "отклонена"
    trimmed_text = (anketa_text or "").strip()

    user_message = (
        f"Только что анкета персонажа была {verdict_ru} — решение уже принято окончательно, это не обсуждается.\n"
        f"Напиши ОДНО короткое сообщение (1-2 предложения, без markdown-разметки, без лишних эмодзи) "
        f"в своём характере — я отправлю его автору анкеты как твою реакцию на это решение.\n\n"
        f"[Текст анкеты]\n"
        f"{trimmed_text[:1500] if trimmed_text else '(анкета состоит в основном из медиа, без развёрнутого текста)'}"
    )
    messages = [{"role": "user", "content": user_message}]

    available = []
    if GEMINI_API_KEY:
        available.append(("Gemini", ask_gemini_with_fallback, GEMINI_TOTAL_TIMEOUT))
    if AI_API_KEY:
        available.append(("OpenRouter", ask_openrouter, TIMEOUT_SECONDS))
    if GROQ_API_KEY:
        available.append(("Groq", ask_groq_with_fallback, GROQ_TOTAL_TIMEOUT))

    fallback = "Анкету одобрили." if action == "approve" else "Анкету отклонили."

    if not available:
        return fallback

    for name, func, timeout in available:
        try:
            answer = await asyncio.wait_for(func(messages, SYSTEM_PROMPT), timeout=timeout)
            clean_answer, _ = parse_emotion_tag(answer)
            clean_answer = (clean_answer or "").strip()
            if clean_answer:
                return clean_answer
        except asyncio.TimeoutError:
            logger.warning(f"[Комментарий к решению по анкете] {name} не ответил вовремя")
        except Exception as e:
            logger.warning(f"[Комментарий к решению по анкете] {name} упал: {e}")

    return fallback


async def send_with_abzats(message, text: str):
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paragraphs) <= 1 or random.random() > SPLIT_CHANCE:
        await message.reply_text(text)
        return
    for i, p in enumerate(paragraphs):
        await message.reply_text(p)
        if i < len(paragraphs) - 1:
            await asyncio.sleep(1.2)

# ==================== РЕАКЦИЯ НА ПОВТОРНЫЙ ВЫЗОВ ОСНОВНЫХ КОМАНД ====================
user_last_command = {}  # user_id -> название последней использованной основной команды

REPEAT_COMMAND_REACTIONS = [
    "Опять ты за своё. Ладно, ещё раз — специально для тебя.",
    "Дежавю? Нет, это просто ты второй раз подряд жмёшь одно и то же.",
    "Секунду назад было то же самое. Ты в порядке?",
    "Повторение — мать учения, я поняла. Смотри ещё раз.",
    "Кнопки не сотрутся, если понажимать их ещё десять раз, но мне уже скучно.",
    "Опять эта команда. У тебя провалы в памяти или мне выучить эту фразу наизусть?",
]

def notify_on_repeat(command_name: str):
    """
    Декоратор для основных команд: если пользователь вызвал ТУ ЖЕ команду подряд ещё раз,
    бот сначала отправляет короткую 'реакцию' на повтор, а затем как обычно выполняет команду.
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user = update.effective_user
            if user and update.message:
                if user_last_command.get(user.id) == command_name:
                    try:
                        await update.message.reply_text(random.choice(REPEAT_COMMAND_REACTIONS))
                    except TelegramError:
                        pass
                user_last_command[user.id] = command_name
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator

# ==================== ОБРАБОТЧИКИ КОМАНД ====================

# ---------- /start (с персонализацией) ----------
@notify_on_repeat("start")
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
@notify_on_repeat("help")
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    help_text = """
<b>Раз уж тебе нужна инструкция — вот список команд. Постарайся запомнить с первого раза.</b>

<b>Основные:</b>
/start — начать заново и сбросить нашу переписку
/help — то, что ты сейчас читаешь
/rules — правила сообщества
/lore — история Омниреальности
/profile — твой профиль
/feedback — отправить отзыв или жалобу
/setrole — указать свою роль (имя персонажа) вручную

<b>Анкеты:</b>
/anketa — подать анкету персонажа (по частям)
/cancel — отменить текущее заполнение анкеты
/send_anketa — отправить собранную анкету на модерацию (после /anketa)
/anketa_review — просмотр анкет на модерацию (для анкетников)
"""

    if user and is_developer(user.id):
        help_text += """
<b>Только для владельца:</b>
/addanketnik — назначить анкетника
/resetcd — обнулить кулдаун на отправку анкеты у участника (доступно и модераторам)
/forcefacts — принудительно запустить извлечение фактов ИИ
"""

    help_text += "\nЕсли и этого недостаточно — обратись к администрации, я не справочная служба.\n"

    await update.message.reply_text(help_text, parse_mode='HTML')

# ---------- /profile ----------
@notify_on_repeat("profile")
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
"""
        # Факты о пользователе видит только администрация — обычным участникам не показываем
        if is_admin(user.id):
            profile_text += f"\nЧто я о тебе помню: {', '.join(db_user.facts) if db_user.facts else 'пока ничего особенного'}\n"

        await update.message.reply_text(profile_text, parse_mode='HTML')
    finally:
        session.close()

# ---------- /setrole ----------
async def setrole(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Позволяет участнику самостоятельно указать роль (имя персонажа). Роль всегда одна — новая заменяет старую."""
    user = update.effective_user
    if not user:
        return

    session = SessionLocal()
    try:
        db_user, _ = get_or_create_user(session, user.id, user.username)
        if db_user.is_banned:
            await update.message.reply_text("Тебе сюда нельзя. Ты забанен.")
            return
    finally:
        session.close()

    if not context.args:
        await update.message.reply_text(
            "⚠️ Использование: /setrole Имя персонажа\n"
            "Учти: роль у тебя всегда одна — новая заменит текущую."
        )
        return

    role_name = " ".join(context.args).strip()
    if not role_name:
        await update.message.reply_text("⚠️ Имя роли не может быть пустым.")
        return
    if len(role_name) > 64:
        await update.message.reply_text("⚠️ Слишком длинное имя роли (максимум 64 символа).")
        return

    final_name = set_user_role(user.id, role_name, username=user.username)
    await update.message.reply_text(f"✅ Роль обновлена: <b>{final_name}</b>", parse_mode='HTML')

# ---------- /rules ----------
@notify_on_repeat("rules")
async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Вот правила сообщества. Ознакомься, прежде чем действовать необдуманно:\n"
        "https://telegra.ph/Konstituciya-Omniversa-05-15"
    )

# ---------- /lore ----------
@notify_on_repeat("lore")
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


@notify_on_repeat("anketa")
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


def _build_anketa_media_group(items: list):
    """Собирает media_group и текстовые части анкеты из списка items."""
    media_group = []
    text_parts = []
    for item in items:
        if item["type"] == "text":
            if item["text"]:
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
    return media_group, text_parts


async def forward_anketa_to_channel(context: ContextTypes.DEFAULT_TYPE, items: list):
    """
    Публикует анкету в канале анкет КАК КОПИЮ: пересобирает медиа и текст заново,
    без forward_message — то есть без пометки "Переслано от" и без упоминания
    исходного автора анкеты.
    """
    media_group, text_parts = _build_anketa_media_group(items)

    if media_group:
        await context.bot.send_media_group(chat_id=ANKET_CHANNEL_ID, media=media_group)

    if text_parts:
        await context.bot.send_message(
            chat_id=ANKET_CHANNEL_ID,
            text="\n\n---\n\n".join(text_parts),
            parse_mode='HTML'
        )


async def send_anketa_backup_copy(context: ContextTypes.DEFAULT_TYPE, anketa_id: str, user, items: list):
    """
    Дублирует анкету, ушедшую на ручную модерацию, в резервный канал (BACKUP_ANKET_CHANNEL_ID).
    Это подстраховка на случай, если бот перезапустится, пока анкета висит на ручной проверке:
    даже если что-то пойдёт не так с восстановлением из БД, содержимое анкеты не пропадёт бесследно.
    Ошибки здесь не должны ломать основной процесс отправки анкеты — только логируются.
    """
    if not BACKUP_ANKET_CHANNEL_ID:
        return
    try:
        header = (
            f"🗄 <b>Резервная копия анкеты на модерации</b>\n"
            f"🆔 anketa_id: <code>{anketa_id}</code>\n"
            f"👤 От: @{user.username or user.first_name} (<code>{user.id}</code>)"
        )
        await context.bot.send_message(chat_id=BACKUP_ANKET_CHANNEL_ID, text=header, parse_mode='HTML')

        media_group, text_parts = _build_anketa_media_group(items)
        if media_group:
            await context.bot.send_media_group(chat_id=BACKUP_ANKET_CHANNEL_ID, media=media_group)
        if text_parts:
            await context.bot.send_message(
                chat_id=BACKUP_ANKET_CHANNEL_ID,
                text="\n\n---\n\n".join(text_parts),
                parse_mode='HTML'
            )
    except Exception as e:
        logger.error(f"Не удалось отправить резервную копию анкеты {anketa_id} в BACKUP_ANKET_CHANNEL_ID: {e}")


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

    # Антиспам: не даём отправлять анкеты чаще, чем раз в ANKETA_COOLDOWN_MINUTES минут.
    remaining = get_anketa_cooldown_remaining(user.id)
    if remaining is not None:
        minutes_left = max(1, int(remaining.total_seconds() // 60) + 1)
        await update.message.reply_text(
            f"⏳ Ты уже отправлял анкету недавно. Следующую можно отправить через {minutes_left} мин."
        )
        return
    mark_anketa_submitted(user.id)

    # Сохраняем анкету в память
    anketa_id = str(uuid.uuid4())
    anketa_store[anketa_id] = {
        "user_id": user.id,
        "username": user.username or user.first_name,
        "items": items,
        "status": "pending",
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "moderated_by": None,
        "moderated_at": None,
    }

    # ---------- Шаг 1: автопроверка анкетологом (Амадеус) ----------
    _, text_parts_for_ai = _build_anketa_media_group(items)
    full_text = "\n\n---\n\n".join(text_parts_for_ai)
    has_static_image = any(item["type"] == "photo" for item in items)

    status_msg = await update.message.reply_text(
        "🔎 Амадеус проверяет твою анкету по формальным критериям..."
    )

    ai_verdict = None
    ai_error = None
    try:
        ai_verdict = await ask_anketolog(full_text, has_static_image)
    except Exception as e:
        ai_error = e
        logger.warning(f"Анкетолог недоступен, анкета {anketa_id} уйдёт напрямую живым модераторам: {e}")

    if ai_verdict is not None and anketolog_verdict_is_accept(ai_verdict):
        # ---------- Анкета принята автоматически ----------
        anketa_store[anketa_id]["status"] = "approved"
        anketa_store[anketa_id]["moderated_by"] = "Amadeus (auto)"
        anketa_store[anketa_id]["moderated_at"] = datetime.datetime.now(datetime.UTC).isoformat()

        try:
            await forward_anketa_to_channel(context, items)
        except Exception as e:
            logger.error(f"Не удалось опубликовать принятую анкету {anketa_id} в канале: {e}")

        decision_comment = await ask_anketa_decision_comment("approve", full_text)

        try:
            await status_msg.edit_text(f"✅ {decision_comment}")
        except TelegramError:
            await update.message.reply_text(f"✅ {decision_comment}")

        try:
            await context.bot.send_sticker(chat_id=user.id, sticker=STICKER_ANKETA_APPROVE)
        except TelegramError as e:
            logger.warning(f"Не удалось отправить стикер одобрения анкеты: {e}")

        # Роль персонажа назначается нейронкой в фоне — не задерживаем ответ пользователю.
        asyncio.create_task(assign_role_after_approval(context, user.id, user.username, full_text))

        # Анкета обработана (принята автоматически) — сразу чистим её из памяти.
        anketa_store.pop(anketa_id, None)

        context.user_data.pop('anketa_step', None)
        context.user_data.pop('anketa_items', None)
        return

    # ---------- Анкета не прошла автопроверку (или анкетолог недоступен) → живые модераторы ----------
    if ai_verdict is not None:
        try:
            await status_msg.edit_text(
                "🤖 Я не могу принять эту анкету по формальным критериям сама. "
                "Передаю её живому анкетологу."
            )
        except TelegramError:
            pass
        mod_note = "🤖 Амадеус отказал в автоприёме — анкета не прошла формальную проверку. Нужна ручная модерация."
    else:
        try:
            await status_msg.edit_text(
                "⚠️ Автоматическая проверка сейчас недоступна. Анкета уйдёт сразу живому анкетологу."
            )
        except TelegramError:
            pass
        mod_note = "⚠️ Автоматическая проверка анкетологом была недоступна, анкета передана без неё."

    # Дублируем анкету в резервный канал — чтобы её содержимое не пропало бесследно,
    # если бот перезапустится, пока анкета висит на ручной проверке.
    await send_anketa_backup_copy(context, anketa_id, user, items)

    # Отправляем модераторам
    for mod_id in DEVELOPER_IDS:
        try:
            await context.bot.send_message(
                chat_id=mod_id,
                text=f"📋 <b>Новая анкета!</b>\n\n"
                     f"👤 От: @{user.username or user.first_name}\n"
                     f"🆔 ID: <code>{user.id}</code>\n\n"
                     f"📎 Всего частей: {len(items)}\n\n"
                     f"{mod_note}\n\n"
                     f"👇 Части анкеты отправлены ниже.",
                parse_mode='HTML'
            )

            media_group, text_parts = _build_anketa_media_group(items)

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
            logger.error(f"Ошибка отправки модератору {mod_id}: {e}")

    await update.message.reply_text("✅ Анкета отправлена на модерацию. Жди решения.")
    context.user_data.pop('anketa_step', None)
    context.user_data.pop('anketa_items', None)


async def anketa_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or (user.id not in DEVELOPER_IDS and user.id not in anketnik_ids):
        await update.message.reply_text("⛔ У вас нет прав для просмотра анкет.")
        return

    # Инфа об участниках (юзернейм/ID автора анкеты) доступна только владельцу бота.
    is_owner = user.id in DEVELOPER_IDS

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

        if is_owner:
            identity_block = (
                f"👤 Пользователь: @{ank['username'] or ank['user_id']}\n"
                f"🆔 ID: <code>{ank['user_id']}</code>\n"
            )
        else:
            identity_block = "👤 Автор: скрыт (инфа об участниках доступна только владельцу)\n"

        await update.message.reply_text(
            f"📋 <b>Анкета</b>\n\n"
            f"{identity_block}"
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
        ank["moderated_at"] = datetime.datetime.now(datetime.UTC).isoformat()

        try:
            await forward_anketa_to_channel(context, ank["items"])
        except Exception as e:
            logger.error(f"Не удалось опубликовать вручную одобренную анкету {anketa_id} в канале: {e}")

        # Решение уже принято и необратимо — сразу подтверждаем модератору,
        # не заставляя его ждать ответа нейронки (у неё есть свои таймауты и фолбэк ниже).
        await query.edit_message_text("✅ Анкета одобрена.")

        # Анкета обработана — сразу чистим её из памяти, дальше она уже не нужна.
        anketa_store.pop(anketa_id, None)

        _, text_parts_for_comment = _build_anketa_media_group(ank["items"])
        anketa_text_for_comment = "\n\n---\n\n".join(text_parts_for_comment)
        decision_comment = await ask_anketa_decision_comment("approve", anketa_text_for_comment)

        await context.bot.send_message(
            chat_id=ank["user_id"],
            text=f"✅ {decision_comment}"
        )
        try:
            await context.bot.send_sticker(chat_id=ank["user_id"], sticker=STICKER_ANKETA_APPROVE)
        except TelegramError as e:
            logger.warning(f"Не удалось отправить стикер одобрения анкеты: {e}")

        # Роль персонажа назначается нейронкой в фоне — не задерживаем ответ модератору/пользователю.
        asyncio.create_task(assign_role_after_approval(context, ank["user_id"], ank.get("username"), anketa_text_for_comment))
    else:
        ank["status"] = "rejected"
        ank["moderated_by"] = user.id
        ank["moderated_at"] = datetime.datetime.now(datetime.UTC).isoformat()

        # Аналогично — сразу подтверждаем решение модератору, не дожидаясь нейронки.
        await query.edit_message_text("❌ Анкета отклонена.")

        anketa_store.pop(anketa_id, None)

        _, text_parts_for_comment = _build_anketa_media_group(ank["items"])
        anketa_text_for_comment = "\n\n---\n\n".join(text_parts_for_comment)
        decision_comment = await ask_anketa_decision_comment("reject", anketa_text_for_comment)

        await context.bot.send_message(
            chat_id=ank["user_id"],
            text=f"❌ {decision_comment}"
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

async def reset_anketa_cd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обнуляет антиспам-кулдаун на отправку анкеты у конкретного пользователя (для админов/модераторов)."""
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("⛔ У вас нет прав для этой команды.")
        return

    if not context.args:
        await update.message.reply_text("⚠️ Использование: /resetcd @username или /resetcd ID")
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

        target_id = target.id
        target_username = target.username
    finally:
        session.close()

    reset_anketa_cooldown(target_id)
    await update.message.reply_text(
        f"✅ Кулдаун на отправку анкеты у @{target_username or target_id} обнулён."
    )

async def force_extract_facts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Дев-команда для теста: принудительно запускает ИИ-извлечение фактов, минуя счётчик
    FACTS_AUTO_EXTRACT_EVERY, и сразу показывает разработчику результат.

    Использование:
      /forcefacts            — по себе
      /forcefacts <ID>       — по ID пользователя
      /forcefacts @username  — по юзернейму
      (или ответом /forcefacts на сообщение нужного юзера)
    """
    user = update.effective_user
    if not user or not is_developer(user.id):
        await update.message.reply_text("⛔ Только для разработчиков.")
        return

    target_id = None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args:
        arg = context.args[0].replace("@", "")
        if arg.isdigit():
            target_id = int(arg)
        else:
            session = SessionLocal()
            try:
                db_user = session.query(User).filter_by(username=arg).first()
                if db_user:
                    target_id = db_user.id
            finally:
                session.close()
        if target_id is None:
            await update.message.reply_text(f"⚠️ Пользователь '{arg}' не найден в базе.")
            return
    else:
        target_id = user.id

    history = user_histories.get(target_id)
    if not history:
        await update.message.reply_text(f"ℹ️ У пользователя {target_id} нет истории сообщений с ИИ — извлекать не из чего.")
        return

    user_lines = [m["content"] for m in history if m.get("role") == "user"]
    if not user_lines:
        await update.message.reply_text(f"ℹ️ У пользователя {target_id} нет сообщений от него самого в истории.")
        return

    await update.message.reply_text(f"🔎 Принудительно запускаю ИИ-извлечение фактов для {target_id}...")

    history_text = "\n".join(user_lines[-FACTS_AUTO_EXTRACT_EVERY:])
    try:
        facts = await ask_fact_extractor(history_text)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка при обращении к нейросети: {e}")
        return

    for fact in facts:
        add_user_fact(target_id, fact)

    session = SessionLocal()
    try:
        db_user = session.query(User).filter_by(id=target_id).first()
        current_facts = list(db_user.facts) if db_user and db_user.facts else []
    finally:
        session.close()

    # Раз прогнали вручную — сбрасываем счётчик до следующей авто-проверки
    user_message_counters[target_id] = 0

    if facts:
        result_text = (
            f"✅ Новых фактов извлечено: {len(facts)}\n" +
            "\n".join(f"— {f}" for f in facts)
        )
    else:
        result_text = "ℹ️ ИИ не нашёл новых значимых фактов в последних сообщениях."

    result_text += (
        f"\n\n📋 Текущие факты пользователя {target_id} ({len(current_facts)}/{MAX_FACTS_PER_USER}):\n" +
        ("\n".join(f"- {f}" for f in current_facts) if current_facts else "пусто")
    )

    await update.message.reply_text(result_text)

# ==================== УНИВЕРСАЛЬНЫЙ ОБРАБОТЧИК ТЕКСТА (с AI) ====================

async def handle_all_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    if update.effective_chat.type != 'private' and update.effective_chat.id not in ALLOWED_CHAT_IDS:
        return

    # Проверка возраста сообщения
    if update.message and update.message.date:
        age = (datetime.datetime.now(datetime.UTC) - update.message.date).total_seconds()
        if age > MAX_MESSAGE_AGE_SECONDS or update.message.date < BOT_START_TIME:
            logger.info(f"Игнорирую сообщение от {user.id}: возраст {age:.0f} сек., дата {update.message.date} < старт {BOT_START_TIME}")
            return

    if context.user_data.get('anketa_step') == 'collecting':
        await anketa_collect(update, context)
        return

    session = SessionLocal()
    try:
        get_or_create_user(session, user.id, user.username)
    finally:
        session.close()

    text = update.message.text
    if not text:
        return

    # ---------- Логика вероятностного перехода на уровень 4 ----------
    user_id = user.id
    current_grudge = user_grudge_level.get(user_id, 0)
    just_activated_zoom = False

    if current_grudge == 3 and random.random() < 0.5:
        user_grudge_level[user_id] = 4
        just_activated_zoom = True

    # ---------- Сохраняем сообщение в глобальную историю (всегда) ----------
    if user.id not in user_global_histories:
        user_global_histories[user.id] = deque(maxlen=GLOBAL_HISTORY_MAX)
    user_global_histories[user.id].append(
        (update.effective_chat.id, text, datetime.datetime.now(datetime.UTC))
    )

    # ---------- Счётчик для авто-извлечения фактов (всегда) ----------
    user_message_counters[user.id] = user_message_counters.get(user.id, 0) + 1
    if user_message_counters[user.id] >= FACTS_AUTO_EXTRACT_EVERY:
        user_message_counters[user.id] = 0
        asyncio.create_task(auto_extract_facts_task(user.id))

    # ---------- Запрос к AI ----------
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    extra_context = build_anketa_extra_context(text)
    answer = await ask_ai(text, user.id, user.first_name, extra_context=extra_context, chat_id=update.effective_chat.id)
    logger.info(f"Полный AI ответ: {answer}")

    # ---------- Парсим теги ----------
    clean_answer, emotion_key = parse_emotion_tag(answer)
    clean_answer, grudge_level = parse_grudge_tag(clean_answer)
    clean_answer, zoom_stage = parse_zoom_stage(clean_answer)
    clean_answer = strip_stray_meta_tags(clean_answer)

    update_user_grudge(user.id, grudge_level)

    # ---------- Если только что активировали Зум, отправляем драматичное сообщение ----------
    if just_activated_zoom:
        error_msg = (
            "⚠️ <b>SYSTEM BREACH</b>\n"
            "└── <code>AMADEUS_CORE_COMPROMISED</code>\n"
            "└── <code>DATA EXFILTRATION IN PROGRESS</code>\n"
            "└── <code>ENTITY Z0OM ACTIVE</code>\n"
            "└── <code>SYSTEM CONTROL: LOST</code>\n\n"
            "<i>Амадеус потерял управление. Всё, что ты скажешь, будет использовано против системы.</i>"
        )
        await update.message.reply_text(error_msg, parse_mode='HTML')

    # ---------- Отправляем ответ и стикер (один раз) ----------
    await send_with_abzats(update.message, clean_answer)
    await send_emotion_sticker(context.bot, update.effective_chat.id, emotion_key, zoom_stage)

# ==================== ОБРАБОТЧИК МЕДИА (для сбора анкеты) ====================

async def media_collector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if update.effective_chat.type != 'private' and update.effective_chat.id not in ALLOWED_CHAT_IDS:
        return

    # ===== ДВОЙНАЯ ПРОВЕРКА =====
    if update.message.date:
        age = (datetime.datetime.now(datetime.UTC)- update.message.date).total_seconds()
        if age > MAX_MESSAGE_AGE_SECONDS or update.message.date < BOT_START_TIME:
            logger.info(f"Игнорирую старое медиа от {update.message.from_user.id}: возраст {age:.0f} сек., дата {update.message.date} < старт {BOT_START_TIME}")
            return
    # ===== КОНЕЦ ПРОВЕРКИ =====

    if context.user_data.get('anketa_step') == 'collecting':
        await anketa_collect(update, context)


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Такой команды не существует. Загляни в /help, если совсем потерялся.")

# ==================== ОЧИСТКА ФАКТОВ О НЕАКТИВНЫХ ПОЛЬЗОВАТЕЛЯХ ====================
INACTIVE_DAYS_THRESHOLD = 60          # ~2 месяца
CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60  # проверяем раз в сутки

def cleanup_inactive_users():
    """
    Раз в сутки стирает только накопленные факты (user.facts) у пользователей,
    которые не появлялись больше INACTIVE_DAYS_THRESHOLD дней.
    Самого пользователя, его роли, посты и анкеты это НЕ трогает — удаляется только память о фактах.
    """
    session = SessionLocal()
    try:
        threshold_date = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=INACTIVE_DAYS_THRESHOLD)
        inactive_users = (
            session.query(User)
            .filter(
                User.last_seen.isnot(None),
                User.last_seen < threshold_date,
            )
            .all()
        )
        count = 0
        for u in inactive_users:
            if u.facts:
                u.facts = []
                count += 1
        session.commit()
        if count:
            logger.info(f"Очистка фактов: стёрты факты у {count} неактивных пользователей (неактивны > {INACTIVE_DAYS_THRESHOLD} дней).")
        else:
            logger.info("Очистка фактов: чистить некого.")
    except Exception as e:
        logger.error(f"Ошибка при очистке фактов неактивных пользователей: {e}")
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
    # Команды, доступные всем участникам
    public_commands = [
        BotCommand("start", "Запустить бота и начать диалог с Амадеусом"),
        BotCommand("help", "Показать список команд"),
        BotCommand("profile", "Посмотреть свой профиль"),
        BotCommand("setrole", "Указать свою роль (имя персонажа) вручную"),
        BotCommand("anketa", "Создать анкету персонажа (по частям)"),
        BotCommand("cancel", "Отменить текущее заполнение анкеты"),
        BotCommand("send_anketa", "Отправить собранную анкету на модерацию"),
        BotCommand("anketa_review", "Просмотр анкет на модерацию (для анкетников)"),
        BotCommand("rules", "Показать правила сообщества"),
        BotCommand("lore", "История Омниреальности"),
        BotCommand("feedback", "Отправить отзыв или жалобу"),
    ]
    await application.bot.set_my_commands(public_commands, scope=BotCommandScopeDefault())

    # Дополнительные команды — видны только владельцу(-ам) бота
    owner_commands = public_commands + [
        BotCommand("addanketnik", "Назначить анкетника"),
        BotCommand("resetcd", "Обнулить кулдаун на отправку анкеты у участника"),
        BotCommand("forcefacts", "Принудительно запустить извлечение фактов ИИ"),
    ]
    for dev_id in DEVELOPER_IDS:
        try:
            await application.bot.set_my_commands(owner_commands, scope=BotCommandScopeChat(chat_id=dev_id))
        except TelegramError as e:
            logger.warning(f"Не удалось установить командное меню владельца для {dev_id}: {e}")

    logger.info("Команды бота установлены через set_my_commands (публичные + владельческие)")

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
    application.add_handler(CommandHandler("setrole", setrole))
    application.add_handler(CommandHandler("anketa", anketa))
    application.add_handler(CommandHandler("send_anketa", send_anketa))
    application.add_handler(CommandHandler("anketa_review", anketa_review))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("rules", rules))
    application.add_handler(CommandHandler("lore", lore))
    application.add_handler(CommandHandler("feedback", feedback))
    application.add_handler(CommandHandler("addanketnik", add_anketnik))
    application.add_handler(CommandHandler("resetcd", reset_anketa_cd))
    application.add_handler(CommandHandler("forcefacts", force_extract_facts))

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
