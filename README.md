# SMM_expertTraveler

## BlogerAssistent

Telegram-бот для автоматической генерации и публикации постов в тематический блог о путешествиях. Использует OpenAI (ChatGPT и DALL·E) для создания текста и изображений, поддерживает публикацию по расписанию и учёт комментариев из группы.

## Возможности

- **Генерация постов** — текст и изображение создаются через OpenAI API
- **Публикация по расписанию** — настраиваемое время и частота (в т.ч. по локальному времени)
- **Режим Zapier** — публикация в Telegram через Zapier по API приложения
- **Комментарии из группы** — последний релевантный комментарий учитывается при генерации поста
- **Бот-администратор** — команды для управления расписанием, ручная публикация, несколько групп
- **PostgreSQL** — при наличии `DATABASE_URL` расписание, статистика и комментарии хранятся в БД (удобно для деплоя на Render)

## Стек

- **Python 3.10+**
- **FastAPI**, **uvicorn**
- **OpenAI API** (ChatGPT, DALL·E)
- **PostgreSQL** (опционально, через `psycopg2-binary`)
- **Zapier** (опционально) — триггер по расписанию и публикация в Telegram

## Установка

### Клонирование и зависимости

```bash
git clone https://github.com/YOUR_USERNAME/blogerAssistent.git
cd blogerAssistent
pip install -r requirements.txt
```

### Переменные окружения

Скопируйте `EnvExample` в `.env` и заполните значения:

| Переменная | Описание |
|------------|----------|
| `OPENAI_API_KEY` | Ключ OpenAI API |
| `TELEGRAM_TOKEN` | Токен бота Telegram (админ-бот) |
| `ADMIN_CHAT_ID` | ID чата администратора для уведомлений и команд |
| `TELEGRAM_GROUP_ID` | ID группы для публикаций (опционально в режиме Zapier) |
| `ZAPIER_MODE` | `true` — публикация только через Zapier |
| `LOCAL_TIMEZONE` | Часовой пояс для `/setlocal` (например `Europe/Moscow`) |
| `DATABASE_URL` | URL PostgreSQL (опционально, для Render рекомендуется) |

Подробнее см. в `EnvExample`.

## Запуск

Локально:

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

На Render укажите команду:

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

## Документация в репозитории

- [WEBHOOK_SETUP.md](WEBHOOK_SETUP.md) — настройка webhook для Telegram-бота
- [ZAPIER_SETUP.md](ZAPIER_SETUP.md) — включение режима Zapier и эндпоинты API
- [DATABASE.md](DATABASE.md) — подключение PostgreSQL на Render

## Структура проекта

```
blogerAssistent/
├── app.py           # Основное приложение (FastAPI, webhook, планировщик, Zapier API)
├── database.py      # Работа с PostgreSQL (расписание, статистика, комментарии, группы)
├── requirements.txt
├── EnvExample       # Пример переменных окружения
├── WEBHOOK_SETUP.md
├── ZAPIER_SETUP.md
└── DATABASE.md
```

## Важно при деплое на Render (Free)

На бесплатном тарифе сервис переходит в режим ожидания после ~15 минут без запросов. Для публикации по расписанию через Zapier рекомендуется настроить опрос **каждые 15 минут** (Schedule by Zapier), чтобы один из запросов приходил на уже «разбуженный» инстанс. Расписание и время публикации задаются в боте (`/settime`, `/setlocal`, `/setfreq`).


