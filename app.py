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
from datetime import datetime
from typing import Optional, Dict, Tuple, Any

import openai
import requests
from fastapi import FastAPI, HTTPException, status, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

# Загрузка переменных окружения из .env файла (только для локальной разработки)
# В production на Koyeb переменные окружения будут заданы через интерфейс
if os.path.exists('.env'):
    load_dotenv()

# Настройка логгера
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("travel-post-generator")

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
        
        # Проверка критически важных настроек
        if not all([self.telegram_token, self.telegram_group_id]):
            logger.critical("Не все необходимые переменные окружения установлены. Приложение может работать некорректно.")
    
    def validate(self) -> bool:
        """Проверяет, что все необходимые настройки присутствуют"""
        return all([self.telegram_token, self.telegram_group_id])

# Инициализация настроек
settings = Settings()

# Инициализация FastAPI приложения
app = FastAPI(
    title="Travel Post Generator API",
    description="API для автоматической генерации и публикации постов о путешествиях в Telegram",
    version="1.0.0",
    contact={
        "name": "Support",
        "email": "support@example.com",
    }
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

# ====== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======

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
    
    try:
        url = f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage"
        payload = {
            "chat_id": settings.admin_chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        
        # Асинхронный запрос через requests в отдельном потоке
        response = await asyncio.to_thread(requests.post, url, data=payload)
        response_data = response.json()
        
        if response_data.get("ok"):
            logger.info(f"Статусное сообщение отправлено администратору: {message[:100]}...")
            return True
        else:
            error_desc = response_data.get('description', 'Неизвестная ошибка')
            logger.error(f"Ошибка при отправке статусного сообщения: {error_desc}")
            return False
            
    except Exception as e:
        logger.exception(f"Критическая ошибка при отправке статусного сообщения: {str(e)}")
        return False

async def get_latest_message() -> Optional[str]:
    """
    Получает последнее сообщение из Telegram группы.
    
    Returns:
        Optional[str]: Текст последнего сообщения или None, если сообщений нет
    """
    try:
        url = f"https://api.telegram.org/bot{settings.telegram_token}/getUpdates"
        response = await asyncio.to_thread(requests.get, url)
        response_data = response.json()
        
        if not response_data.get("ok"):
            error_desc = response_data.get('description', 'Неизвестная ошибка')
            await send_status_message(f"⚠️ Ошибка при получении обновлений из Telegram: {error_desc}")
            return None
            
        if "result" not in response_data or not response_data["result"]:
            return None
        
        # Берем последнее сообщение из группы
        for update in reversed(response_data["result"]):
            if "message" in update:
                message = update["message"]
                if str(message.get("chat", {}).get("id")) == str(settings.telegram_group_id):
                    return message.get("text")
        return None
        
    except Exception as e:
        await send_status_message(f"⚠️ Критическая ошибка при получении последнего сообщения: {str(e)}")
        logger.exception("Ошибка при получении последнего сообщения")
        return None

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
        full_prompt += f"\n\nДополнительно учти комментарий участника группы:\n{extra_context}\nОрганично интегрируй его смысл в пост."
    
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
                "chat_id": settings.telegram_group_id,
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
            "chat_id": settings.telegram_group_id,
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
        # Получаем последний комментарий
        await send_status_message("🔍 Ищем последний комментарий в группе...")
        latest_comment = await get_latest_message()
        
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
        
        # Публикуем пост с изображением
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
        "telegram_configured": bool(settings.telegram_token and settings.telegram_group_id),
        "admin_notifications": bool(settings.admin_chat_id)
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
