#!/usr/bin/env python3
"""
Приложение для автоматической генерации и публикации постов в Telegram с использованием OpenAI и DALL-E.
Основные функции:
- Генерация текстовых постов на тему путешествий
- Генерация изображений через DALL-E
- Публикация постов в Telegram с изображением
- Обратная связь через статусные сообщения
- Обработка ошибок и fallback-механизмы

Запуск: uvicorn app:app --host 0.0.0.0 --port $PORT
"""

import os
import logging
import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Tuple, Any, List
from pathlib import Path

import openai
import requests
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, status, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

import database as db

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - для совместимости, если zoneinfo недоступен
    ZoneInfo = None

# Загрузка переменных окружения из .env файла (только для локальной разработки)
# В production на Render переменные окружения задаются в дашборде
if os.path.exists('.env'):
    load_dotenv()

# Инициализация БД при наличии DATABASE_URL (таблицы создаются до инициализации менеджеров)
if os.getenv("DATABASE_URL"):
    db.init_db()

# Настройка логгера
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("travel-post-generator")

class ScheduleManager:
    """Управление расписанием публикаций (файл или PostgreSQL при DATABASE_URL)."""
    
    _default_schedule = {
        "next_post_time": None,
        "frequency_hours": 24,
        "enabled": True,
        "next_run_at": None,
    }

    def __init__(self, schedule_file: str = "schedule.json"):
        self.schedule_file = Path(schedule_file)
        self._use_db = bool(os.getenv("DATABASE_URL"))
        self.schedule = self._load_schedule()
    
    def _load_schedule(self) -> Dict[str, Any]:
        """Загружает расписание из БД или файла"""
        if self._use_db:
            data = db.db_schedule_load()
            if data:
                return data
            return dict(self._default_schedule)
        if self.schedule_file.exists():
            try:
                with open(self.schedule_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Ошибка при загрузке расписания: {e}")
        return dict(self._default_schedule)
    
    def _save_schedule(self):
        """Сохраняет расписание в БД или файл"""
        if self._use_db:
            if db.db_schedule_save(self.schedule):
                return
        try:
            with open(self.schedule_file, 'w', encoding='utf-8') as f:
                json.dump(self.schedule, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка при сохранении расписания: {e}")
    
    def get_next_post_time(self) -> Optional[str]:
        """Возвращает время следующей публикации"""
        return self.schedule.get("next_post_time")
    
    def set_next_post_time(self, post_time: str):
        """Устанавливает время следующей публикации (формат: HH:MM или ISO datetime). Обновляет next_run_at."""
        self.schedule["next_post_time"] = post_time
        if post_time and ":" in str(post_time) and len(str(post_time)) <= 5:
            try:
                hour, minute = map(int, str(post_time).strip().split(":")[:2])
                now = datetime.now()
                candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if candidate <= now:
                    candidate += timedelta(days=1)
                self.schedule["next_run_at"] = candidate.isoformat()
            except (ValueError, TypeError):
                pass
        self._save_schedule()
    
    def set_frequency(self, hours: int):
        """Устанавливает частоту публикаций в часах"""
        self.schedule["frequency_hours"] = hours
        self._save_schedule()
    
    def get_frequency(self) -> int:
        """Возвращает частоту публикаций в часах"""
        return self.schedule.get("frequency_hours", 24)
    
    def is_enabled(self) -> bool:
        """Проверяет, включено ли расписание"""
        return self.schedule.get("enabled", True)
    
    def set_enabled(self, enabled: bool):
        """Включает/выключает расписание"""
        self.schedule["enabled"] = enabled
        self._save_schedule()
    
    def get_next_run_at(self) -> Optional[datetime]:
        """Возвращает datetime следующей запланированной публикации (для планировщика)."""
        next_run = self.schedule.get("next_run_at")
        if next_run:
            try:
                return datetime.fromisoformat(next_run)
            except (ValueError, TypeError):
                pass
        # Вычисляем из next_post_time (HH:MM)
        time_str = self.schedule.get("next_post_time")
        if not time_str or ":" not in str(time_str):
            return None
        try:
            hour, minute = map(int, str(time_str).strip().split(":")[:2])
            now = datetime.now()
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            self.schedule["next_run_at"] = candidate.isoformat()
            self._save_schedule()
            return candidate
        except (ValueError, TypeError):
            return None
    
    def set_next_run_at(self, dt: datetime):
        """Устанавливает время следующего запуска (после публикации по расписанию)."""
        self.schedule["next_run_at"] = dt.isoformat()
        self._save_schedule()
    
    def set_next_run_after_publish(self):
        """Вызвать после публикации: следующий запуск = сейчас + frequency_hours."""
        self.schedule["next_run_at"] = (datetime.now() + timedelta(hours=self.get_frequency())).isoformat()
        self._save_schedule()

class StatsManager:
    """Управление статистикой вовлеченности (файл или PostgreSQL при DATABASE_URL)."""
    
    def __init__(self, stats_file: str = "stats.json"):
        self.stats_file = Path(stats_file)
        self._use_db = bool(os.getenv("DATABASE_URL"))
        self.stats = self._load_stats()
    
    def _load_stats(self) -> Dict[str, Any]:
        """Загружает статистику из файла (при БД в памяти не храним список постов)."""
        if self._use_db:
            return {"posts": []}
        if self.stats_file.exists():
            try:
                with open(self.stats_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Ошибка при загрузке статистики: {e}")
        return {"posts": []}
    
    def _save_stats(self):
        """Сохраняет статистику в файл (при БД не используется)."""
        if self._use_db:
            return
        try:
            with open(self.stats_file, 'w', encoding='utf-8') as f:
                json.dump(self.stats, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка при сохранении статистики: {e}")
    
    def add_post(self, post_id: str, text_id: Optional[str] = None, photo_id: Optional[str] = None):
        """Добавляет информацию о новом посте"""
        if self._use_db:
            db.db_stats_add_post(post_id, text_id, photo_id)
            return
        post_data = {
            "post_id": post_id,
            "text_id": text_id,
            "photo_id": photo_id,
            "timestamp": datetime.now().isoformat(),
            "views": 0,
            "comments": 0
        }
        if "posts" not in self.stats:
            self.stats["posts"] = []
        self.stats["posts"].append(post_data)
        if len(self.stats["posts"]) > 100:
            self.stats["posts"] = self.stats["posts"][-100:]
        self._save_stats()
    
    def update_post_stats(self, post_id: str, views: Optional[int] = None, comments: Optional[int] = None):
        """Обновляет статистику поста"""
        if self._use_db:
            db.db_stats_update_post(post_id, views, comments)
            return
        for post in self.stats.get("posts", []):
            if post.get("post_id") == post_id or post.get("text_id") == post_id or post.get("photo_id") == post_id:
                if views is not None:
                    post["views"] = views
                if comments is not None:
                    post["comments"] = comments
                self._save_stats()
                return
        return
    
    def get_recent_stats(self, days: int = 7) -> Dict[str, Any]:
        """Возвращает статистику за последние N дней"""
        if self._use_db:
            return db.db_stats_get_recent(days)
        cutoff_date = datetime.now() - timedelta(days=days)
        recent_posts = [
            post for post in self.stats.get("posts", [])
            if datetime.fromisoformat(post["timestamp"]) >= cutoff_date
        ]
        total_views = sum(post.get("views", 0) for post in recent_posts)
        total_comments = sum(post.get("comments", 0) for post in recent_posts)
        avg_views = total_views / len(recent_posts) if recent_posts else 0
        avg_comments = total_comments / len(recent_posts) if recent_posts else 0
        return {
            "period_days": days,
            "total_posts": len(recent_posts),
            "total_views": total_views,
            "total_comments": total_comments,
            "avg_views": round(avg_views, 1),
            "avg_comments": round(avg_comments, 1),
            "posts": recent_posts[-10:]
        }


class CommentsManager:
    """Хранение последних комментариев из группы (через Zapier). Файл или PostgreSQL при DATABASE_URL."""

    def __init__(self, comments_file: str = "comments.json"):
        self.comments_file = Path(comments_file)
        self._use_db = bool(os.getenv("DATABASE_URL"))
        self.comments = self._load_comments()

    def _load_comments(self) -> Dict[str, Any]:
        if self._use_db:
            return {"comments": []}
        if self.comments_file.exists():
            try:
                with open(self.comments_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception as e:
                logger.error(f"Ошибка при загрузке комментариев: {e}")
        return {"comments": []}

    def _save_comments(self) -> None:
        if self._use_db:
            return
        try:
            with open(self.comments_file, "w", encoding="utf-8") as f:
                json.dump(self.comments, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка при сохранении комментариев: {e}")

    def add_comment(
        self,
        chat_id: str,
        message_id: Optional[str],
        text: str,
        timestamp: Optional[str] = None,
    ) -> None:
        if self._use_db:
            db.db_comments_add(chat_id, message_id, text, timestamp)
            return
        if "comments" not in self.comments:
            self.comments["comments"] = []
        self.comments["comments"].append(
            {
                "chat_id": str(chat_id),
                "message_id": str(message_id) if message_id is not None else None,
                "text": text,
                "timestamp": timestamp or datetime.now().isoformat(),
            }
        )
        if len(self.comments["comments"]) > 200:
            self.comments["comments"] = self.comments["comments"][-200:]
        self._save_comments()

    def get_latest_comment_any(self) -> Optional[str]:
        if self._use_db:
            return db.db_comments_get_latest_any()
        comments = self.comments.get("comments") or []
        if not comments:
            return None
        return comments[-1].get("text")

    def get_latest_comment_for_chat(self, chat_id: str) -> Optional[str]:
        if self._use_db:
            return db.db_comments_get_latest_for_chat(chat_id)
        comments = self.comments.get("comments") or []
        for item in reversed(comments):
            if str(item.get("chat_id")) == str(chat_id):
                return item.get("text")
        return None

class GroupsManager:
    """Управление списком групп (файл или PostgreSQL при DATABASE_URL)."""
    
    def __init__(self, groups_file: str = "groups.json"):
        self.groups_file = Path(groups_file)
        self._use_db = bool(os.getenv("DATABASE_URL"))
        self.groups, self._active_group_id = self._load_groups()
    
    def _load_groups(self) -> tuple:
        """Возвращает (list of groups, active_group_id)."""
        if self._use_db:
            return db.db_groups_load()
        if self.groups_file.exists():
            try:
                with open(self.groups_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data, None
                    return data.get("groups", []), data.get("active_group_id")
            except Exception as e:
                logger.error(f"Ошибка при загрузке групп: {e}")
        return [], None
    
    def _save_groups(self):
        if self._use_db:
            db.db_groups_save(self.groups, self._active_group_id)
            return
        try:
            with open(self.groups_file, 'w', encoding='utf-8') as f:
                json.dump({
                    "groups": self.groups,
                    "active_group_id": self._active_group_id
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка при сохранении групп: {e}")
    
    def get_all(self) -> List[Dict[str, Any]]:
        if self._use_db:
            self.groups, self._active_group_id = db.db_groups_load()
        return list(self.groups)
    
    def add_group(self, group_id: str, title: str = "") -> bool:
        gid = str(group_id)
        if self._use_db:
            db.db_groups_add(gid, title or f"Группа {gid}")
            self.groups, self._active_group_id = db.db_groups_load()
            return True
        for g in self.groups:
            if str(g.get("group_id")) == gid:
                g["title"] = title or g.get("title", "")
                self._save_groups()
                return True
        self.groups.append({"group_id": gid, "title": title or f"Группа {gid}"})
        self._save_groups()
        return True
    
    def set_active(self, group_id: str) -> bool:
        gid = str(group_id)
        if self._use_db:
            db.db_groups_set_active(gid)
            self.groups, self._active_group_id = db.db_groups_load()
            return True
        for g in self.groups:
            if str(g.get("group_id")) == gid:
                self._active_group_id = gid
                self._save_groups()
                return True
        self._active_group_id = gid
        self.groups.append({"group_id": gid, "title": f"Группа {gid}"})
        self._save_groups()
        return True
    
    def get_active(self) -> Optional[str]:
        if self._active_group_id:
            return self._active_group_id
        if self.groups:
            return str(self.groups[0].get("group_id"))
        return None

class Settings:
    """
    Класс для управления настройками приложения через переменные окружения.
    Все необходимые API-ключи и идентификаторы читаем из переменных окружения.
    """
    
    def __init__(self):
        # OpenAI настройки
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        if not self.openai_api_key:
            logger.warning("OPENAI_API_KEY не установлен. Генерация текста и изображений будет недоступна.")
        
        # Telegram настройки
        self.telegram_token = os.getenv("TELEGRAM_TOKEN")
        if not self.telegram_token:
            logger.error("TELEGRAM_TOKEN не установлен. Приложение не сможет публиковать посты.")
        
        self.telegram_group_id = os.getenv("TELEGRAM_GROUP_ID")
        if not self.telegram_group_id:
            logger.error("TELEGRAM_GROUP_ID не установлен. Приложение не знает, куда публиковать посты.")
        
        self.admin_chat_id = os.getenv("ADMIN_CHAT_ID")
        if not self.admin_chat_id:
            logger.warning("ADMIN_CHAT_ID не установлен. Статусные сообщения не будут отправляться.")
        
        # Режим Zapier: публикация в Telegram идёт через Zapier (авторизация бота/группы в Zapier)
        self.zapier_mode = os.getenv("ZAPIER_MODE", "").strip().lower() in ("1", "true", "yes")
        if self.zapier_mode:
            logger.info("ZAPIER_MODE включён: публикация в Telegram через Zapier (бот и группа настраиваются в Zapier).")

        # Локальный часовой пояс администратора (для ввода локального времени)
        self.local_timezone_name = os.getenv("LOCAL_TIMEZONE")
        self.local_timezone: Optional[Any] = None
        if self.local_timezone_name:
            if ZoneInfo is None:
                logger.warning("LOCAL_TIMEZONE задан, но модуль zoneinfo недоступен. Будет игнорироваться.")
            else:
                try:
                    self.local_timezone = ZoneInfo(self.local_timezone_name)
                    logger.info(f"LOCAL_TIMEZONE установлен: {self.local_timezone_name}")
                except Exception as e:
                    logger.warning(f"Не удалось инициализировать LOCAL_TIMEZONE={self.local_timezone_name}: {e}")
        
        # Проверка критически важных настроек
        if not self.zapier_mode and not all([self.telegram_token, self.telegram_group_id]):
            logger.critical("Не все необходимые переменные окружения установлены. Приложение может работать некорректно.")
    
    def validate(self) -> bool:
        """Проверяет, что все необходимые настройки присутствуют."""
        if self.zapier_mode:
            return bool(self.telegram_token)  # для бота-администратора; публикация — через Zapier
        return bool(self.telegram_token) and bool(get_active_group_id())

    def convert_local_time_to_server_hhmm(self, hour: int, minute: int) -> Optional[str]:
        """
        Перевод локального времени администратора (LOCAL_TIMEZONE) в серверное HH:MM (UTC),
        которое затем используется в расписании.
        """
        if not self.local_timezone or ZoneInfo is None:
            return None
        try:
            now_utc = datetime.now(timezone.utc)
            local_now = now_utc.astimezone(self.local_timezone)
            candidate_local = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate_local <= local_now:
                candidate_local += timedelta(days=1)
            candidate_utc = candidate_local.astimezone(timezone.utc)
            return candidate_utc.strftime("%H:%M")
        except Exception as e:
            logger.warning(f"Ошибка конвертации локального времени в серверное: {e}")
            return None

# Инициализация настроек
settings = Settings()

# Инициализация менеджеров
schedule_manager = ScheduleManager()
stats_manager = StatsManager()
comments_manager = CommentsManager()
groups_manager = GroupsManager()
# Если в .env задана одна группа, добавляем её в список при первом запуске
if settings.telegram_group_id and not groups_manager.get_all():
    groups_manager.add_group(settings.telegram_group_id, "Группа по умолчанию")
    groups_manager.set_active(settings.telegram_group_id)

def get_active_group_id() -> Optional[str]:
    """ID группы для публикации: из списка групп или из TELEGRAM_GROUP_ID."""
    return groups_manager.get_active() or settings.telegram_group_id

async def _scheduler_loop():
    """Фоновый цикл: публикация по расписанию (время и частота из бота-администратора). В режиме Zapier публикация идёт через Zapier — планировщик не постит."""
    if settings.zapier_mode:
        logger.info("Планировщик: режим Zapier — публикация по расписанию через Zapier (опрос /zapier/should-post).")
        while True:
            await asyncio.sleep(3600)
        return
    logger.info("Планировщик публикаций запущен")
    while True:
        try:
            await asyncio.sleep(60)  # проверка раз в минуту
            if not schedule_manager.is_enabled():
                continue
            next_run = schedule_manager.get_next_run_at()
            if next_run and datetime.now() >= next_run:
                logger.info("Запуск публикации по расписанию")
                await generate_and_publish_post(background=True)
        except asyncio.CancelledError:
            logger.info("Планировщик публикаций остановлен")
            break
        except Exception as e:
            logger.exception(f"Ошибка в планировщике: {e}")

_scheduler_task: Optional[asyncio.Task] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler_task
    if os.getenv("DATABASE_URL"):
        await asyncio.to_thread(db.init_db)
    _scheduler_task = asyncio.create_task(_scheduler_loop())
    yield
    if _scheduler_task:
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass

# Инициализация FastAPI приложения
app = FastAPI(
    title="Travel Post Generator API",
    description="API для автоматической генерации и публикации постов о путешествиях в Telegram",
    version="1.0.0",
    contact={
        "name": "Support",
        "email": "support@example.com",
    },
    lifespan=lifespan,
)

# Модель для ответа API
class HealthCheck(BaseModel):
    """Модель для эндпоинта проверки работоспособности"""
    status: str
    timestamp: str
    details: Optional[Dict[str, Any]] = None

class PostGenerationResponse(BaseModel):
    """Модель для ответа на запрос генерации поста"""
    status: str
    post_id: Optional[str] = None
    image_url: Optional[str] = None
    message: str
    timestamp: str

class ScheduleRequest(BaseModel):
    """Модель для запроса установки расписания"""
    next_post_time: Optional[str] = None
    frequency_hours: Optional[int] = None
    enabled: Optional[bool] = None

class ScheduleResponse(BaseModel):
    """Модель для ответа с информацией о расписании"""
    next_post_time: Optional[str]
    frequency_hours: int
    enabled: bool
    message: str

class StatsResponse(BaseModel):
    """Модель для ответа со статистикой"""
    period_days: int
    total_posts: int
    total_views: int
    total_comments: int
    avg_views: float
    avg_comments: float
    posts: List[Dict[str, Any]]

# ====== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======

async def send_telegram_message(
    chat_id: str,
    message: str,
    parse_mode: str = "HTML",
    reply_markup: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Отправляет сообщение в указанный чат Telegram.
    
    Args:
        chat_id: ID чата для отправки
        message: Текст сообщения
        parse_mode: Режим парсинга (HTML или Markdown)
        
    Returns:
        bool: True, если сообщение успешно отправлено, иначе False
    """
    if not settings.telegram_token:
        return False
    
    try:
        url = f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        
        response = await asyncio.to_thread(requests.post, url, data=payload)
        response_data = response.json()
        
        if response_data.get("ok"):
            return True
        else:
            logger.error(f"Ошибка при отправке сообщения: {response_data.get('description')}")
            return False
            
    except Exception as e:
        logger.exception(f"Ошибка при отправке сообщения в Telegram: {str(e)}")
        return False

async def send_telegram_photo(chat_id: str, photo_url: str, caption: str = "") -> bool:
    """Отправляет фото по URL в указанный чат Telegram."""
    if not settings.telegram_token or not photo_url:
        return False
    try:
        url = f"https://api.telegram.org/bot{settings.telegram_token}/sendPhoto"
        payload = {
            "chat_id": chat_id,
            "photo": photo_url,
            "parse_mode": "HTML",
        }
        if caption:
            payload["caption"] = caption[:1024]
        response = await asyncio.to_thread(requests.post, url, data=payload)
        response_data = response.json()
        if response_data.get("ok"):
            return True
        logger.error(f"Ошибка при отправке фото: {response_data.get('description')}")
        return False
    except Exception as e:
        logger.exception(f"Ошибка при отправке фото в Telegram: {str(e)}")
        return False

async def send_status_message(message: str) -> bool:
    """
    Отправляет статусное сообщение администратору через Telegram.
    
    Args:
        message: Текст сообщения для отправки
        
    Returns:
        bool: True, если сообщение успешно отправлено, иначе False
    """
    if not settings.admin_chat_id:
        logger.info(f"Статусное сообщение (без отправки, ADMIN_CHAT_ID не установлен): {message}")
        return False
    
    if not settings.admin_chat_id:
        logger.info(f"Статусное сообщение (без отправки, ADMIN_CHAT_ID не установлен): {message}")
        return False
    
    return await send_telegram_message(settings.admin_chat_id, message)

async def get_latest_message() -> Optional[str]:
    """
    Возвращает последний комментарий, полученный через Zapier (CommentsManager).
    Прямые вызовы getUpdates не используются, чтобы не конфликтовать с webhook.
    """
    return comments_manager.get_latest_comment_any()

async def do_generate_now(admin_chat_id: str) -> None:
    """
    Генерация поста без расписания: отправляет фото и текст админу, при наличии группы — публикует в группу.
    """
    post_data = await _generate_post_content_for_zapier()
    if not post_data:
        await send_telegram_message(admin_chat_id, "❌ Не удалось сгенерировать пост. Проверьте OPENAI_API_KEY и логи сервера.")
        return
    caption = post_data.get("photo_caption") or ""
    body = post_data.get("body_text") or ""
    image_url = post_data.get("photo_url") or ""
    full_text = post_data.get("full_text") or f"{caption}\n\n{body}"
    # Админу: фото с подписью и отдельно текст (или только текст, если нет фото)
    if image_url:
        await send_telegram_photo(
            admin_chat_id,
            image_url,
            caption=f"✅ <b>Пост без расписания</b>\n\n{caption}",
        )
    if body.strip():
        await send_telegram_message(admin_chat_id, body)
    elif not image_url and full_text.strip():
        await send_telegram_message(admin_chat_id, "✅ <b>Пост без расписания</b>\n\n" + full_text)
    # Публикация в группу, если задана
    group_id = get_active_group_id()
    if group_id and (image_url or full_text.strip()):
        await send_post_with_image(image_url, full_text)
        await send_telegram_message(admin_chat_id, "📤 Пост опубликован в группу.")
    elif not group_id:
        await send_telegram_message(admin_chat_id, "💡 Чтобы публиковать в группу, задайте TELEGRAM_GROUP_ID или добавьте группу через /addgroup.")

async def is_travel_related(comment: str) -> bool:
    """
    Проверяет, относится ли комментарий к теме путешествий.
    
    Args:
        comment: Текст комментария для проверки
        
    Returns:
        bool: True, если комментарий относится к путешествиям, иначе False
    """
    if not comment or not settings.openai_api_key:
        return False
    
    check_prompt = """
    Определи, относится ли следующий текст к тематике путешествий.
    Ответь только YES или NO.
    Текст:
    {comment}
    """.format(comment=comment)
    
    try:
        openai_client = openai.OpenAI(api_key=settings.openai_api_key)
        response = await asyncio.to_thread(
            openai_client.chat.completions.create,
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": check_prompt}],
            temperature=0,
            max_tokens=10
        )
        
        answer = response.choices[0].message.content.strip().upper()
        is_related = answer == "YES"
        logger.info(f"Проверка тематики комментария: '{comment[:50]}...' -> {'Соответствует' if is_related else 'Не соответствует'}")
        return is_related
        
    except Exception as e:
        await send_status_message(f"⚠️ Ошибка при проверке тематики комментария: {str(e)}")
        logger.exception("Ошибка при проверке тематики комментария")
        return False

async def generate_hashtags(post_text: str) -> str:
    """
    Генерирует релевантные хештеги для поста.
    
    Args:
        post_text: Текст поста, на основе которого генерируются хештеги
        
    Returns:
        str: Строка с хештегами, разделенными пробелами
    """
    if not settings.openai_api_key:
        logger.warning("OPENAI_API_KEY не установлен, используем fallback хештеги")
        return "#путешествия #путешественникам #отдых"
    
    try:
        hashtag_prompt = """
        Создай 3-5 релевантных хештегов для следующего поста о путешествиях.
        Хештеги должны быть популярными и соответствовать содержанию поста.
        Выведи их в одну строку, разделив пробелами, без запятых и без дополнительного текста.
        Пример правильного формата: #путешествия #советыпутешественникам #отдых
        
        Пост:
        {post_text}
        """.format(post_text=post_text[:1000])  # Ограничиваем длину для экономии токенов
        
        openai_client = openai.OpenAI(api_key=settings.openai_api_key)
        response = await asyncio.to_thread(
            openai_client.chat.completions.create,
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": hashtag_prompt}],
            max_tokens=100,
            temperature=0.3
        )
        
        hashtags = response.choices[0].message.content.strip()
        # Убедимся, что хештеги начинаются с #
        if not hashtags.startswith('#'):
            hashtags = '#' + hashtags.replace(' ', ' #')
        return hashtags
        
    except Exception as e:
        await send_status_message(f"⚠️ Ошибка при генерации хештегов: {str(e)}")
        logger.exception("Ошибка при генерации хештегов")
        return "#путешествия #путешественникам #отдых"

async def generate_post(extra_context: Optional[str] = None) -> str:
    """
    Генерирует текстовый пост для Telegram с использованием OpenAI.
    
    Args:
        extra_context: Дополнительный контекст (например, комментарий из группы)
        
    Returns:
        str: Сгенерированный пост с хештегами
    """
    BASE_PROMPT = """
    Напиши текстовый пост для Telegram на русском языке на тему путешествий. Требования к посту:
    1. Добавь цепляющий заголовок в первой строке.
    2. После заголовка оставь одну пустую строку.
    3. Основной текст 1000–1500 символов.
    4. Пиши живым, лёгким, вдохновляющим языком.
    5. Используй абзацы по 2–4 строки для удобства чтения в Telegram.
    6. Можно использовать эмодзи, но не более 5–7 на весь текст.
    7. Не используй кавычки, фигурные скобки, обратные слеши, HTML-теги, Markdown-разметку и специальные символы форматирования.
    8. Не используй списки с маркерами типа *, -, #. Если нужен список, делай его через нумерацию 1. 2. 3.
    9. В конце добавь короткий вовлекающий вопрос к читателю.
    10. Текст должен быть полностью готов к публикации без дополнительного редактирования.
    Тематика поста:
    Советы путешественникам, интересные места, необычные маршруты, лайфхаки в поездках.
    """
    
    full_prompt = BASE_PROMPT
    if extra_context:
        # Длинный комментарий обрезаем, чтобы не перегружать промпт
        max_comment_len = 500
        context = extra_context[:max_comment_len] + ("..." if len(extra_context) > max_comment_len else "")
        full_prompt += f"\n\nДополнительно учти комментарий участника группы:\n{context}\nОрганично интегрируй его смысл в пост."
    
    # Fallback-пост на случай ошибки
    fallback_content = """
    Открой для себя мир за окном! 🌍

    Путешествия делают нас свободнее, мудрее и счастливее. Не ждите идеального момента - создайте его сами! 

    Соберите рюкзак, купите билет и отправляйтесь в путь. Пусть каждый день приносит новые впечатления и знакомства.

    Какое место мечтаете посетить в этом году?

    #путешествия #открытия #смелыелюди
    """
    
    if not settings.openai_api_key:
        logger.warning("OPENAI_API_KEY не установлен, используем fallback-пост")
        await send_status_message("📝 OPENAI_API_KEY не установлен, используем fallback-пост")
        return fallback_content.strip()
    
    try:
        openai_client = openai.OpenAI(api_key=settings.openai_api_key)
        response = await asyncio.to_thread(
            openai_client.chat.completions.create,
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": full_prompt}],
            max_tokens=2000,
            temperature=0.7
        )
        
        post = response.choices[0].message.content.strip()
        
        # Добавляем хештеги
        hashtags = await generate_hashtags(post)
        return f"{post}\n\n{hashtags}"
        
    except Exception as e:
        logger.exception("Ошибка при генерации поста через OpenAI")
        await send_status_message(f"⚠️ Ошибка при генерации поста через OpenAI: {str(e)}")
        
        # Добавляем информацию о комментарии в fallback, если он был
        if extra_context:
            fallback_content = f"""
            Открой для себя мир за окном! 🌍

            Путешествия делают нас свободнее, мудрее и счастливее. Не ждите идеального момента - создайте его сами! 

            Соберите рюкзак, купите билет и отправляйтесь в путь. Пусть каждый день приносит новые впечатления и знакомства.

            Напомним комментарий одного из участников: "{extra_context[:100]}..."

            Какое место мечтаете посетить в этом году?

            #путешествия #открытия #смелыелюди
            """
        
        await send_status_message("📝 Генерируем резервный пост...")
        return fallback_content.strip()

async def generate_image_prompt(post_text: str) -> str:
    """
    Генерирует промпт для DALL-E на основе текста поста.
    
    Args:
        post_text: Текст поста для генерации промпта
        
    Returns:
        str: Промпт для генерации изображения
    """
    if not settings.openai_api_key:
        logger.warning("OPENAI_API_KEY не установлен, используем стандартный промпт")
        return "Beautiful travel destination, cinematic style, natural lighting"
    
    try:
        image_prompt_instruction = """
        Based on the following Telegram travel post, create a detailed cinematic visual prompt
        in English for DALL-E image generation.
        The prompt should describe:
        - environment
        - atmosphere
        - lighting
        - camera angle
        - mood
        - realistic style
        Post:
        {post_text}
        """.format(post_text=post_text[:1500])  # Ограничиваем длину для экономии токенов
        
        openai_client = openai.OpenAI(api_key=settings.openai_api_key)
        response = await asyncio.to_thread(
            openai_client.chat.completions.create,
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": image_prompt_instruction}],
            temperature=0.7,
            max_tokens=300
        )
        
        return response.choices[0].message.content.strip()
        
    except Exception as e:
        logger.exception("Ошибка при генерации промпта для изображения")
        await send_status_message(f"⚠️ Ошибка при генерации промпта для изображения: {str(e)}")
        return "Beautiful travel destination, cinematic style, natural lighting"

async def generate_image(image_prompt: str) -> Optional[str]:
    """
    Генерирует изображение через DALL-E API.
    
    Args:
        image_prompt: Промпт для генерации изображения
        
    Returns:
        Optional[str]: URL сгенерированного изображения или None при ошибке
    """
    if not settings.openai_api_key:
        logger.warning("OPENAI_API_KEY не установлен, пропускаем генерацию изображения")
        await send_status_message("⚠️ OPENAI_API_KEY не установлен, пропускаем генерацию изображения")
        return None
    
    try:
        await send_status_message("🖼️ Запрашиваем изображение у DALL-E...")
        logger.info(f"Запрос к DALL-E с промптом: {image_prompt[:100]}...")
        
        openai_client = openai.OpenAI(api_key=settings.openai_api_key)
        response = await asyncio.to_thread(
            openai_client.images.generate,
            model="dall-e-3",
            prompt=image_prompt,
            size="1024x1024",
            quality="standard",
            n=1
        )
        
        image_url = response.data[0].url
        logger.info(f"Изображение успешно сгенерировано. URL: {image_url}")
        await send_status_message("✅ Изображение успешно сгенерировано!")
        return image_url
        
    except Exception as e:
        error_msg = f"⚠️ Ошибка при генерации изображения через DALL-E: {str(e)}"
        logger.exception(error_msg)
        await send_status_message(error_msg)
        return None

def _save_generated_post_to_file(
    post_text: str,
    image_prompt: Optional[str] = None,
    image_url: Optional[str] = None,
) -> None:
    """
    Сохраняет сгенерированный пост и метаданные в файлы на сервере
    (для аналитики и архива по требованию проекта).
    """
    data_dir = Path("data")
    posts_dir = data_dir / "generated_posts"
    posts_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d_%H-%M-%S")
    payload = {
        "timestamp": now.isoformat(),
        "text": post_text,
        "image_prompt": image_prompt,
        "image_url": image_url,
    }
    try:
        path = posts_dir / f"{ts}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        last_post_path = data_dir / "last_post.json"
        with open(last_post_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info(f"Сгенерированный пост сохранён: {path}, last_post.json")
    except Exception as e:
        logger.exception(f"Ошибка сохранения поста в файл: {e}")

def _split_post_for_caption_and_body(post_text: str) -> Tuple[str, str]:
    """Разбивает текст поста на заголовок (caption для фото) и основной текст, как в Telegram."""
    lines = post_text.split('\n')
    title = lines[0] if lines else "Путешествия"
    content_start = 1
    for i, line in enumerate(lines):
        if i > 0 and line.strip() == '':
            content_start = i + 1
            break
    body = '\n'.join(lines[content_start:]) if content_start < len(lines) else ""
    return title[:1024], body

async def _generate_post_content_for_zapier() -> Optional[Dict[str, Any]]:
    """
    Генерирует пост (текст + изображение) и возвращает данные для публикации через Zapier.
    Не публикует в Telegram. Сохраняет контент в файл.
    """
    if not settings.openai_api_key:
        logger.warning("OPENAI_API_KEY не установлен, генерация для Zapier недоступна")
        return None
    try:
        latest_comment = comments_manager.get_latest_comment_any()
        if latest_comment and await is_travel_related(latest_comment):
            generated_post = await generate_post(latest_comment)
        else:
            generated_post = await generate_post()
        image_prompt = await generate_image_prompt(generated_post)
        image_url = await generate_image(image_prompt)
        _save_generated_post_to_file(generated_post, image_prompt, image_url)
        photo_caption, body_text = _split_post_for_caption_and_body(generated_post)
        return {
            "photo_url": image_url,
            "photo_caption": photo_caption,
            "body_text": body_text.strip(),
            "full_text": generated_post,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.exception(f"Ошибка генерации контента для Zapier: {e}")
        return None

async def send_post_with_image(image_url: Optional[str], post_text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Публикует пост с изображением в Telegram.
    
    Args:
        image_url: URL изображения для публикации
        post_text: Текст поста
        
    Returns:
        Tuple[Optional[str], Optional[str]]: ID изображения и ID текстового сообщения
    """
    try:
        # Разделяем пост на заголовок и содержание
        lines = post_text.split('\n')
        title = lines[0] if lines else "Путешествия"
        
        # Ищем пустую строку после заголовка для определения начала основного текста
        content_start = 1
        for i, line in enumerate(lines):
            if i > 0 and line.strip() == '':
                content_start = i + 1
                break
        
        # Формируем основной текст (без заголовка и первой пустой строки)
        content_text = '\n'.join(lines[content_start:]) if content_start < len(lines) else ""
        
        photo_message_id = None
        text_message_id = None
        
        if image_url:
            # Отправляем изображение с заголовком в caption
            photo_url = f"https://api.telegram.org/bot{settings.telegram_token}/sendPhoto"
            photo_payload = {
                "chat_id": get_active_group_id(),
                "photo": image_url,
                "caption": title[:1024],  # Ограничение Telegram на длину caption
                "parse_mode": "HTML"
            }
            
            photo_response = await asyncio.to_thread(requests.post, photo_url, data=photo_payload)
            photo_response_data = photo_response.json()
            
            if photo_response_data.get("ok"):
                photo_message_id = str(photo_response_data["result"]["message_id"])
                logger.info(f"Изображение успешно опубликовано. ID: {photo_message_id}")
            else:
                error_desc = photo_response_data.get('description', 'Неизвестная ошибка')
                logger.error(f"Ошибка при отправке изображения: {error_desc}")
                await send_status_message(f"⚠️ Ошибка при отправке изображения: {error_desc}")
        
        # Отправляем основной текст
        if content_text.strip():
            text_message_id = await send_to_telegram(content_text)
        
        # Если не было изображения, отправляем весь пост как одно сообщение
        elif not image_url and post_text.strip():
            text_message_id = await send_to_telegram(post_text)
        
        return photo_message_id, text_message_id
        
    except Exception as e:
        error_msg = f"⚠️ Критическая ошибка при публикации поста с изображением: {str(e)}"
        logger.exception(error_msg)
        await send_status_message(error_msg)
        
        # Пытаемся отправить хотя бы текст как финальный fallback
        if post_text.strip():
            text_message_id = await send_to_telegram(post_text)
            return None, text_message_id
        
        return None, None

async def send_to_telegram(text: str) -> Optional[str]:
    """
    Отправляет текстовое сообщение в Telegram.
    
    Args:
        text: Текст сообщения для отправки
        
    Returns:
        Optional[str]: ID отправленного сообщения или None при ошибке
    """
    try:
        url = f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage"
        payload = {
            "chat_id": get_active_group_id(),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }
        
        response = await asyncio.to_thread(requests.post, url, data=payload)
        response_data = response.json()
        
        if response_data.get("ok"):
            message_id = str(response_data["result"]["message_id"])
            logger.info(f"Текстовый пост успешно опубликован. ID: {message_id}")
            return message_id
        else:
            error_desc = response_data.get('description', 'Неизвестная ошибка')
            logger.error(f"Ошибка при публикации текстового поста: {error_desc}")
            await send_status_message(f"❌ Ошибка при публикации поста: {error_desc}")
            return None
            
    except Exception as e:
        logger.exception(f"Ошибка при отправке поста в Telegram: {str(e)}")
        await send_status_message(f"❌ Ошибка при отправке поста в Telegram: {str(e)}")
        return None

async def update_post_stats_async(post_id: str):
    """Асинхронно обновляет статистику поста через некоторое время после публикации"""
    # Ждем 5 минут после публикации, чтобы собрать начальную статистику
    await asyncio.sleep(300)  # 5 минут
    
    try:
        views, comments = await get_post_statistics(post_id)
        stats_manager.update_post_stats(post_id, views=views, comments=comments)
        logger.info(f"Статистика обновлена для поста {post_id}: просмотры={views}, комментарии={comments}")
    except Exception as e:
        logger.exception(f"Ошибка при обновлении статистики поста {post_id}: {e}")

async def get_post_statistics(post_id: str) -> Tuple[int, int]:
    """
    Получает статистику поста из Telegram (просмотры и комментарии).
    
    Args:
        post_id: ID сообщения в Telegram
        
    Returns:
        Tuple[int, int]: Количество просмотров и комментариев
    """
    try:
        # Получаем информацию о сообщении
        url = f"https://api.telegram.org/bot{settings.telegram_token}/getChat"
        chat_response = await asyncio.to_thread(requests.get, url, params={"chat_id": get_active_group_id()})
        
        # Для получения статистики нужно использовать getChatMemberCount или forwardMessage
        # Но Telegram API не предоставляет прямого способа получить просмотры/комментарии
        # Поэтому будем использовать приблизительные методы
        
        # Пытаемся получить обновления и посчитать комментарии к посту
        updates_url = f"https://api.telegram.org/bot{settings.telegram_token}/getUpdates"
        updates_response = await asyncio.to_thread(requests.get, updates_url)
        updates_data = updates_response.json()
        
        comments_count = 0
        if updates_data.get("ok") and "result" in updates_data:
            for update in updates_data["result"]:
                if "message" in update:
                    msg = update["message"]
                    # Проверяем, является ли сообщение ответом на наш пост
                    if msg.get("reply_to_message") and str(msg.get("reply_to_message", {}).get("message_id")) == str(post_id):
                        comments_count += 1
        
        # Просмотры сложно получить точно через API, используем приблизительное значение
        # В реальности можно использовать Telegram Bot API для каналов или другие методы
        views = 0  # Будет обновляться вручную или через другие методы
        
        return views, comments_count
        
    except Exception as e:
        logger.exception(f"Ошибка при получении статистики поста: {e}")
        return 0, 0

async def handle_bot_command(command: str, chat_id: str, message_text: str = "") -> str:
    """
    Обрабатывает команды от Telegram бота.
    
    Args:
        command: Команда бота (например, /schedule, /stats)
        chat_id: ID чата, откуда пришла команда
        message_text: Полный текст сообщения
        
    Returns:
        str: Ответ на команду
    """
    # Проверяем, что команда от администратора
    if chat_id != settings.admin_chat_id:
        return "❌ У вас нет прав для выполнения этой команды."
    
    command = command.lower().strip()
    
    if command == "/start" or command == "/help":
        zapier_note = "\n📌 <i>Публикация в Telegram идёт через Zapier (бот и группа подключаются в Zapier).</i>\n" if settings.zapier_mode else ""
        tz_note = ""
        if settings.local_timezone_name:
            tz_note = f"\n🕒 Локальный часовой пояс: <code>{settings.local_timezone_name}</code> (команда /setlocal HH:MM задаёт время в нём)."
        return f"""🤖 <b>SMM-эксперт путешественника</b>{zapier_note}{tz_note}

<b>Доступные команды:</b>
/generate_now - Сгенерировать пост без расписания (только для администратора)
/schedule - Показать текущее расписание публикаций
/settime HH:MM - Установить время следующей публикации (серверное время, например: /settime 14:30)
/setlocal HH:MM - Установить время следующей публикации по локальному времени (если задан LOCAL_TIMEZONE)
/setfreq N - Установить частоту публикаций в часах (например: /setfreq 24)
/stats - Показать статистику вовлеченности за последние 7 дней
/stats N - Показать статистику за последние N дней
/groups - Список групп для публикаций
/setgroup ID - Выбрать активную группу для публикаций
/addgroup - Добавить группу (отправьте в чате группы, где бот админ)
/toggle_schedule - Включить/выключить генерацию по расписанию
/nextpost - Показать информацию о следующем запланированном посте

Примеры:
/settime 09:00 - установить публикацию на 9 утра (по времени сервера)
/setlocal 10:00 - установить публикацию на 10:00 по локальному времени
/setfreq 12 - публиковать каждые 12 часов"""
    
    elif command == "/schedule":
        next_time = schedule_manager.get_next_post_time()
        frequency = schedule_manager.get_frequency()
        enabled = schedule_manager.is_enabled()
        
        status_emoji = "✅" if enabled else "⏸️"
        response = f"{status_emoji} <b>Текущее расписание:</b>\n\n"
        response += f"📅 <b>Следующая публикация:</b> {next_time or 'Не установлено'}\n"
        response += f"⏰ <b>Частота:</b> каждые {frequency} часов\n"
        response += f"🔄 <b>Статус:</b> {'Включено' if enabled else 'Выключено'}\n\n"
        response += "Используйте /settime для установки времени или /setfreq для изменения частоты."
        return response
    
    elif command == "/generate_now":
        # Обработка вынесена в do_generate_now (фото + публикация в группу)
        return ""
    
    elif command.startswith("/settime"):
        # Парсим время из команды /settime HH:MM
        parts = message_text.split()
        if len(parts) < 2:
            return "❌ Неверный формат. Используйте: /settime HH:MM\nПример: /settime 14:30"
        
        time_str = parts[1]
        try:
            # Проверяем формат времени
            hour, minute = map(int, time_str.split(':'))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return "❌ Неверное время. Используйте формат HH:MM (например: 14:30)"
            
            # Устанавливаем время следующей публикации
            schedule_manager.set_next_post_time(time_str)
            
            response = f"✅ Время следующей публикации установлено: <b>{time_str}</b>\n\n"
            response += f"📅 Следующий пост будет опубликован в {time_str}"
            return response
            
        except ValueError:
            return "❌ Неверный формат времени. Используйте: /settime HH:MM\nПример: /settime 14:30"

    elif command.startswith("/setlocal"):
        # Устанавливаем время следующей публикации по локальному часовому поясу (LOCAL_TIMEZONE)
        parts = message_text.split()
        if len(parts) < 2:
            return "❌ Неверный формат. Используйте: /setlocal HH:MM\nПример: /setlocal 10:00"
        if not settings.local_timezone:
            return "❌ Локальный часовой пояс не настроен. Установите переменную окружения LOCAL_TIMEZONE (например, Europe/Moscow)."
        time_str = parts[1]
        try:
            hour, minute = map(int, time_str.split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return "❌ Неверное время. Используйте формат HH:MM (например: 10:00)"
            server_time = settings.convert_local_time_to_server_hhmm(hour, minute)
            if not server_time:
                return "❌ Не удалось перевести локальное время в серверное. Проверьте LOCAL_TIMEZONE."
            schedule_manager.set_next_post_time(server_time)
            response = f"✅ Время следующей публикации установлено по локальному времени: <b>{time_str}</b>\n"
            response += f"🕒 Это соответствует серверному времени (UTC) примерно: <b>{server_time}</b>"
            return response
        except ValueError:
            return "❌ Неверный формат времени. Используйте: /setlocal HH:MM\nПример: /setlocal 10:00"
    
    elif command.startswith("/setfreq"):
        # Парсим частоту из команды /setfreq N
        parts = message_text.split()
        if len(parts) < 2:
            return "❌ Неверный формат. Используйте: /setfreq N\nПример: /setfreq 24 (каждые 24 часа)"
        
        try:
            hours = int(parts[1])
            if hours < 1:
                return "❌ Частота должна быть не менее 1 часа"
            
            schedule_manager.set_frequency(hours)
            response = f"✅ Частота публикаций установлена: <b>каждые {hours} часов</b>\n\n"
            response += f"📅 Посты будут публиковаться каждые {hours} часов"
            return response
            
        except ValueError:
            return "❌ Неверный формат. Используйте: /setfreq N\nПример: /setfreq 24"
    
    elif command.startswith("/stats"):
        # Парсим количество дней из команды /stats N
        parts = message_text.split()
        days = 7  # По умолчанию 7 дней
        if len(parts) > 1:
            try:
                days = int(parts[1])
                if days < 1:
                    days = 7
            except ValueError:
                pass
        
        stats = stats_manager.get_recent_stats(days)
        
        response = f"📊 <b>Статистика вовлеченности за последние {days} дней:</b>\n\n"
        response += f"📝 <b>Всего постов:</b> {stats['total_posts']}\n"
        response += f"👁️ <b>Всего просмотров:</b> {stats['total_views']}\n"
        response += f"💬 <b>Всего комментариев:</b> {stats['total_comments']}\n"
        response += f"📈 <b>Среднее просмотров:</b> {stats['avg_views']}\n"
        response += f"💭 <b>Среднее комментариев:</b> {stats['avg_comments']}\n"
        
        if stats['posts']:
            response += "\n<b>Последние посты:</b>\n"
            for post in stats['posts'][-5:]:  # Показываем последние 5
                post_time = datetime.fromisoformat(post['timestamp']).strftime("%d.%m %H:%M")
                response += f"• {post_time}: 👁️ {post.get('views', 0)} 💬 {post.get('comments', 0)}\n"
        
        return response
    
    elif command == "/toggle_schedule":
        current = schedule_manager.is_enabled()
        schedule_manager.set_enabled(not current)
        new_status = "Включено" if not current else "Выключено"
        emoji = "✅" if not current else "⏸️"
        return f"{emoji} Расписание теперь: <b>{new_status}</b>"
    
    elif command == "/groups":
        try:
            all_groups = groups_manager.get_all()
            active_id = get_active_group_id()
            if not all_groups and active_id:
                all_groups = [{"group_id": active_id, "title": "Группа из TELEGRAM_GROUP_ID"}]
            resp = "👥 <b>Группы для публикаций</b>\n\n"
            for i, g in enumerate(all_groups, 1):
                gid = str(g.get("group_id", ""))
                title = g.get("title", gid)
                mark = " ✅ (активная)" if gid == str(active_id) else ""
                resp += f"{i}. {title}\n   ID: <code>{gid}</code>{mark}\n\n"
            resp += "Используйте /setgroup ID чтобы выбрать группу, /addgroup — добавить группу (отправьте в чате группы)."
            return resp
        except Exception as e:
            logger.exception(f"Ошибка при получении информации о группах: {e}")
            return f"❌ Ошибка: {str(e)}"
    
    elif command.startswith("/setgroup"):
        parts = message_text.split(maxsplit=1)
        if len(parts) < 2:
            return "❌ Используйте: /setgroup ID_группы\nПример: /setgroup -1001234567890"
        gid = parts[1].strip()
        if groups_manager.set_active(gid):
            return f"✅ Активная группа установлена: <code>{gid}</code>"
        return f"❌ Не удалось установить группу {gid}"
    
    elif command == "/addgroup":
        return "📌 Отправьте /addgroup в чате той группы, куда добавлен бот — группа будет добавлена в список. Либо добавьте группу вручную: /setgroup ID_группы"
    
    elif command == "/nextpost":
        next_time = schedule_manager.get_next_post_time()
        frequency = schedule_manager.get_frequency()
        
        if next_time:
            response = f"📅 <b>Следующий запланированный пост:</b>\n\n"
            response += f"⏰ <b>Время:</b> {next_time}\n"
            response += f"🔄 <b>Частота:</b> каждые {frequency} часов\n\n"
            response += "✅ Пост будет опубликован автоматически в указанное время."
        else:
            response = "⚠️ Время следующей публикации не установлено.\n\n"
            response += "Используйте /settime HH:MM для установки времени следующей публикации."
        
        return response
    
    else:
        return "❌ Неизвестная команда. Используйте /help для списка доступных команд."

async def generate_and_publish_post(background: bool = False) -> Dict[str, Any]:
    """
    Основная функция: генерирует и публикует пост в Telegram.
    
    Args:
        background: Выполняется ли задача в фоновом режиме
        
    Returns:
        Dict[str, Any]: Результат выполнения операции
    """
    start_time = datetime.now()
    logger.info("Запуск процесса генерации и публикации поста")
    
    if not settings.validate():
        error_msg = "Критические настройки не установлены. Проверьте переменные окружения."
        logger.critical(error_msg)
        if not background:
            await send_status_message(f"❌ {error_msg}")
        return {
            "status": "error",
            "message": error_msg,
            "timestamp": datetime.now().isoformat()
        }
    
    try:
        # Получаем последний комментарий (через Zapier CommentsManager)
        await send_status_message("🔍 Ищем последний комментарий из группы (через Zapier)...")
        latest_comment = comments_manager.get_latest_comment_any()
        
        # Генерируем пост
        if latest_comment and await is_travel_related(latest_comment):
            await send_status_message("💬 Найден релевантный комментарий. Генерируем персонализированный пост...")
            logger.info(f"Используем комментарий для персонализации: {latest_comment[:100]}...")
            generated_post = await generate_post(latest_comment)
        else:
            await send_status_message("📝 Генерируем стандартный пост...")
            generated_post = await generate_post()
        
        logger.info(f"Сгенерированный пост (первые 200 символов): {generated_post[:200]}...")
        
        # Генерируем промпт для изображения
        await send_status_message("🎨 Генерируем промпт для изображения...")
        image_prompt = await generate_image_prompt(generated_post)
        logger.info(f"Сгенерированный промпт для изображения: {image_prompt[:200]}...")
        
        # Генерируем изображение через DALL-E
        await send_status_message("🖼️ Генерируем изображение через DALL-E...")
        image_url = await generate_image(image_prompt)
        
        # Сохраняем сгенерированный контент в файл на сервере (аналитика и архив)
        _save_generated_post_to_file(generated_post, image_prompt, image_url)
        
        # В режиме Zapier не публикуем в Telegram — публикация идёт через Zapier
        if settings.zapier_mode:
            photo_caption, body_text = _split_post_for_caption_and_body(generated_post)
            result = {
                "status": "success",
                "message": "Контент сгенерирован для публикации через Zapier",
                "timestamp": datetime.now().isoformat(),
                "processing_time": (datetime.now() - start_time).total_seconds(),
                "zapier_payload": {
                    "photo_url": image_url,
                    "photo_caption": photo_caption,
                    "body_text": body_text.strip(),
                    "full_text": generated_post,
                },
            }
            await send_status_message("✅ Пост сгенерирован для Zapier. Опубликуйте его через Zapier (Telegram).")
            if schedule_manager.is_enabled():
                schedule_manager.set_next_run_after_publish()
            return result
        
        # Публикуем пост с изображением в Telegram (не Zapier)
        await send_status_message("📤 Публикуем пост с изображением...")
        photo_id, text_id = await send_post_with_image(image_url, generated_post)
        
        # Формируем результат
        result = {
            "status": "success",
            "post_id": photo_id or text_id,
            "image_url": image_url,
            "message": "Пост успешно опубликован",
            "timestamp": datetime.now().isoformat(),
            "processing_time": (datetime.now() - start_time).total_seconds()
        }
        
        # Сохраняем статистику поста
        if photo_id or text_id:
            post_id_for_stats = photo_id or text_id
            stats_manager.add_post(
                post_id=post_id_for_stats,
                text_id=text_id,
                photo_id=photo_id
            )
            
            # Пытаемся получить начальную статистику
            # В фоне обновим статистику позже
            asyncio.create_task(update_post_stats_async(post_id_for_stats))
        
        # Обновляем следующее время публикации по расписанию
        if schedule_manager.is_enabled():
            schedule_manager.set_next_run_after_publish()
        
        # Отправляем статусное сообщение об успехе
        status_msg = "✅ Пост успешно опубликован!\n"
        if photo_id:
            status_msg += f"🖼️ ID изображения: {photo_id}\n"
        if text_id:
            status_msg += f"📝 ID текста: {text_id}\n"
        status_msg += f"⏱️ Время обработки: {result['processing_time']:.2f} сек"
        await send_status_message(status_msg)
        
        return result
        
    except Exception as e:
        error_msg = f"Критическая ошибка при выполнении процесса: {str(e)}"
        logger.exception(error_msg)
        await send_status_message(f"❌ {error_msg}")
        
        return {
            "status": "error",
            "message": str(e),
            "timestamp": datetime.now().isoformat(),
            "processing_time": (datetime.now() - start_time).total_seconds()
        }

# ====== ЭНДПОИНТЫ API ======

@app.get("/health", response_model=HealthCheck)
async def health_check():
    """
    Эндпоинт для проверки работоспособности сервиса.
    
    Returns:
        HealthCheck: Статус сервиса и дополнительная информация
    """
    is_healthy = settings.validate()
    
    details = {
        "openai_api_configured": bool(settings.openai_api_key),
        "zapier_mode": settings.zapier_mode,
        "telegram_configured": bool(settings.telegram_token and (get_active_group_id() or settings.zapier_mode)),
        "admin_notifications": bool(settings.admin_chat_id),
        "database_configured": bool(os.getenv("DATABASE_URL")),
    }
    
    if is_healthy:
        logger.info("Проверка работоспособности: OK")
        return HealthCheck(
            status="healthy",
            timestamp=datetime.now().isoformat(),
            details=details
        )
    else:
        logger.warning("Проверка работоспособности: частично неработоспособен")
        return HealthCheck(
            status="degraded",
            timestamp=datetime.now().isoformat(),
            details=details
        )

@app.post("/generate", response_model=PostGenerationResponse)
async def generate_post_endpoint(background_tasks: BackgroundTasks):
    """
    Эндпоинт для запуска процесса генерации и публикации поста.
    
    Returns:
        PostGenerationResponse: Результат генерации и публикации поста
    """
    logger.info("Получен запрос на генерацию нового поста")
    
    # Отправляем подтверждение получения запроса
    await send_status_message("🔄 Получен запрос на генерацию нового поста")
    
    # Запускаем процесс в фоне, чтобы не блокировать HTTP-соединение
    background_tasks.add_task(generate_and_publish_post, background=True)
    
    return PostGenerationResponse(
        status="processing",
        message="Запрос на генерацию поста принят. Процесс запущен в фоновом режиме.",
        timestamp=datetime.now().isoformat()
    )

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """
    Webhook для получения обновлений от Telegram Bot API.
    Обрабатывает команды от администратора.
    """
    try:
        data = await request.json()
        
        # Проверяем, что это сообщение
        if "message" not in data:
            return JSONResponse(content={"ok": True})
        
        message = data["message"]
        chat_id = str(message.get("chat", {}).get("id"))
        text = message.get("text", "").strip() if message.get("text") else ""
        chat = message.get("chat", {})
        chat_type = chat.get("type", "")
        from_user = message.get("from") or {}
        from_id = str(from_user.get("id", ""))
        is_bot = from_user.get("is_bot", False)
        is_admin = settings.admin_chat_id and from_id == str(settings.admin_chat_id)
        
        # Сообщения из группы (не команды) — сохраняем как комментарии для следующего поста
        if text and not text.startswith("/") and chat_type in ("group", "supergroup") and not is_bot:
            active_gid = get_active_group_id()
            all_groups = [str(g.get("group_id")) for g in groups_manager.get_all()]
            if chat_id == str(active_gid) or chat_id in all_groups:
                message_id = str(message.get("message_id", ""))
                comments_manager.add_comment(chat_id, message_id, text)
                logger.info(f"Комментарий из группы {chat_id} сохранён: {text[:80]}...")
            return JSONResponse(content={"ok": True})
        
        # Дальше только команды
        if not text.startswith("/"):
            return JSONResponse(content={"ok": True})
        
        parts = text.split(maxsplit=1)
        command = parts[0]
        
        # Добавление группы: /addgroup отправлено в чате группы администратором
        if command == "/addgroup" and chat_type in ("group", "supergroup") and is_admin:
            title = chat.get("title", f"Группа {chat_id}")
            groups_manager.add_group(chat_id, title)
            groups_manager.set_active(chat_id)
            response_text = f"✅ Группа добавлена и выбрана для публикаций:\n📝 {title}\n🆔 <code>{chat_id}</code>"
            await send_telegram_message(chat_id, response_text)
            return JSONResponse(content={"ok": True})
        
        # Генерация без расписания: фото админу и публикация в группу
        if command == "/generate_now" and is_admin:
            await send_telegram_message(chat_id, "🔄 Генерирую пост...")
            await do_generate_now(chat_id)
            return JSONResponse(content={"ok": True})
        
        # Обрабатываем команду
        response_text = await handle_bot_command(command, chat_id, text)
        
        # Клавиатура администратора с кнопками-командами
        admin_keyboard = {
            "keyboard": [
                [{"text": "/generate_now"}],
                [{"text": "/toggle_schedule"}],
                [{"text": "/schedule"}],
            ],
            "resize_keyboard": True,
            "one_time_keyboard": False,
        }
        
        # Отправляем ответ (для /start и /help добавляем клавиатуру)
        if command in ("/start", "/help"):
            await send_telegram_message(chat_id, response_text, reply_markup=admin_keyboard)
        else:
            await send_telegram_message(chat_id, response_text)
        
        return JSONResponse(content={"ok": True})
        
    except Exception as e:
        logger.exception(f"Ошибка при обработке webhook: {e}")
        return JSONResponse(content={"ok": False, "error": str(e)}, status_code=500)

# ====== ЭНДПОИНТЫ ДЛЯ ZAPIER ======
# Публикация в Telegram идёт через Zapier: авторизация бота/группы в Zapier,
# расписание и частота задаются в боте-администраторе; Zapier опрашивает should-post и публикует.

@app.get("/zapier/should-post")
async def zapier_should_post():
    """
    Опрос для Zapier: пора ли публиковать пост по расписанию.
    Zapier вызывает этот URL по расписанию (например каждые 15 мин).
    Если пора — возвращаем контент поста; Zapier отправляет его в Telegram своим шагом.
    """
    if not settings.zapier_mode:
        return JSONResponse(
            content={"should_post": False, "post": None, "error": "ZAPIER_MODE не включён"},
            status_code=400,
        )
    if not schedule_manager.is_enabled():
        return JSONResponse(content={"should_post": False, "post": None})
    next_run = schedule_manager.get_next_run_at()
    if not next_run or datetime.now() < next_run:
        return JSONResponse(content={"should_post": False, "post": None})
    # Время пришло — генерируем контент и возвращаем для публикации через Zapier
    post_data = await _generate_post_content_for_zapier()
    if not post_data:
        return JSONResponse(
            content={"should_post": False, "post": None, "error": "Не удалось сгенерировать контент"},
            status_code=500,
        )
    schedule_manager.set_next_run_after_publish()
    return JSONResponse(content={"should_post": True, "post": post_data})

@app.get("/zapier/schedule")
async def zapier_schedule():
    """Текущее расписание (время и частота из бота-администратора) для настройки Zapier."""
    return JSONResponse(content={
        "next_post_time": schedule_manager.get_next_post_time(),
        "frequency_hours": schedule_manager.get_frequency(),
        "enabled": schedule_manager.is_enabled(),
        "next_run_at": schedule_manager.schedule.get("next_run_at"),
    })


class ZapierComment(BaseModel):
    chat_id: str
    message_id: Optional[str] = None
    text: str
    username: Optional[str] = None
    timestamp: Optional[str] = None


@app.post("/zapier/comment")
async def zapier_comment(comment: ZapierComment):
    """
    Приём комментариев из Telegram через Zapier.
    Второй Zap: триггер Telegram (новое сообщение в группе) → Webhook POST сюда.
    """
    comments_manager.add_comment(
        chat_id=comment.chat_id,
        message_id=comment.message_id,
        text=comment.text,
        timestamp=comment.timestamp,
    )
    logger.info(f"Получен комментарий из Zapier для чата {comment.chat_id}: {comment.text[:80]}...")
    return JSONResponse(content={"status": "ok"})

@app.post("/zapier/generate-post")
async def zapier_generate_post():
    """
    Генерация поста по запросу (для ручного Zap в Zapier или теста).
    Возвращает контент для публикации в Telegram через Zapier; не проверяет расписание.
    """
    if not settings.zapier_mode:
        return JSONResponse(
            content={"error": "ZAPIER_MODE не включён"},
            status_code=400,
        )
    post_data = await _generate_post_content_for_zapier()
    if not post_data:
        return JSONResponse(
            content={"error": "Не удалось сгенерировать контент"},
            status_code=500,
        )
    return JSONResponse(content=post_data)

@app.get("/schedule", response_model=ScheduleResponse)
async def get_schedule():
    """
    Эндпоинт для получения текущего расписания публикаций.
    """
    return ScheduleResponse(
        next_post_time=schedule_manager.get_next_post_time(),
        frequency_hours=schedule_manager.get_frequency(),
        enabled=schedule_manager.is_enabled(),
        message="Расписание успешно получено"
    )

@app.post("/schedule", response_model=ScheduleResponse)
async def set_schedule(schedule_request: ScheduleRequest):
    """
    Эндпоинт для установки расписания публикаций.
    """
    if schedule_request.next_post_time is not None:
        schedule_manager.set_next_post_time(schedule_request.next_post_time)
    
    if schedule_request.frequency_hours is not None:
        schedule_manager.set_frequency(schedule_request.frequency_hours)
    
    if schedule_request.enabled is not None:
        schedule_manager.set_enabled(schedule_request.enabled)
    
    # Уведомляем администратора об изменении расписания
    next_time = schedule_manager.get_next_post_time()
    frequency = schedule_manager.get_frequency()
    enabled = schedule_manager.is_enabled()
    
    status_msg = "📅 <b>Расписание обновлено:</b>\n\n"
    status_msg += f"⏰ <b>Следующая публикация:</b> {next_time or 'Не установлено'}\n"
    status_msg += f"🔄 <b>Частота:</b> каждые {frequency} часов\n"
    status_msg += f"✅ <b>Статус:</b> {'Включено' if enabled else 'Выключено'}"
    await send_status_message(status_msg)
    
    return ScheduleResponse(
        next_post_time=next_time,
        frequency_hours=frequency,
        enabled=enabled,
        message="Расписание успешно обновлено"
    )

@app.get("/stats", response_model=StatsResponse)
async def get_stats(days: int = 7):
    """
    Эндпоинт для получения статистики вовлеченности.
    
    Args:
        days: Количество дней для анализа (по умолчанию 7)
    """
    stats = stats_manager.get_recent_stats(days)
    return StatsResponse(**stats)

@app.get("/test-notification")
async def test_notification():
    """
    Эндпоинт для тестирования отправки уведомлений администратору.
    
    Returns:
        JSONResponse: Результат отправки тестового уведомления
    """
    logger.info("Запрос на отправку тестового уведомления")
    
    success = await send_status_message("✅ Тестовое уведомление от Travel Post Generator API")
    
    if success:
        return JSONResponse(
            content={
                "status": "success",
                "message": "Тестовое уведомление отправлено администратору"
            },
            status_code=status.HTTP_200_OK
        )
    else:
        return JSONResponse(
            content={
                "status": "error",
                "message": "Не удалось отправить тестовое уведомление. Проверьте настройки ADMIN_CHAT_ID."
            },
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

# Обработчик глобальных исключений
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """
    Глобальный обработчик исключений для логирования и уведомления.
    
    Args:
        request: HTTP-запрос
        exc: Исключение
        
    Returns:
        JSONResponse: Ответ с информацией об ошибке
    """
    logger.exception(f"Необработанное исключение: {str(exc)}")
    
    # Отправляем уведомление администратору
    await send_status_message(f"🚨 Критическая ошибка в API: {str(exc)}")
    
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error",
            "message": "Внутренняя ошибка сервера",
            "details": str(exc) if app.debug else "Произошла внутренняя ошибка"
        }
    )

# ====== ТОЧКА ВХОДА ======

if __name__ == "__main__":
    """
    Точка входа для локального запуска с помощью python app.py
    Для production используйте uvicorn app:app --host 0.0.0.0 --port $PORT
    """
    import uvicorn
    
    logger.info("Запуск приложения в режиме разработки")
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=True,
        log_level="info"
    )
