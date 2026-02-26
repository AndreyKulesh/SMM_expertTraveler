# Настройка интеграции с Zapier (Telegram через Zapier)

По проекту интеграция с Telegram API идёт через Zapier: авторизация бота и группы, управление группами и публикация постов настраиваются в Zapier.

## Включение режима Zapier

В переменных окружения задайте:

```
ZAPIER_MODE=true
```

При этом:
- **Публикация в Telegram** выполняется только через Zapier (приложение не постит само).
- **Бот и группа** подключаются в Zapier (авторизация через Zapier).
- **Расписание и частота** по-прежнему задаются в боте-администраторе (`/settime`, `/setfreq`, `/schedule`).
- Приложение генерирует контент (OpenAI) и отдаёт его по API для Zapier.

Обязательны: `OPENAI_API_KEY`, `TELEGRAM_TOKEN`, `ADMIN_CHAT_ID` (бот для команд администратора и уведомлений).  
`TELEGRAM_GROUP_ID` в режиме Zapier не обязателен для публикации (группу выбираете в Zapier).

---

## Эндпоинты для Zapier

Базовый URL приложения (деплой на Render): `https://smm-experttraveler.onrender.com`

### 1. Опрос «пора ли публиковать» (рекомендуемый сценарий)

**GET** `https://smm-experttraveler.onrender.com/zapier/should-post`

- Zapier вызывает этот URL по расписанию (например, каждые 15 минут).
- Если по расписанию бота-администратора пора публиковать — приложение генерирует пост и возвращает контент.
- Ответ при «пора»:
  - `should_post: true`
  - `post`: объект с полями `photo_url`, `photo_caption`, `body_text`, `full_text`.
- Ответ при «ещё не время»: `should_post: false`, `post: null`.

В Zapier:
1. **Trigger**: Schedule by Zapier — интервал (например, каждые 15 минут) или время по расписанию.
2. **Action**: Webhooks by Zapier — GET запрос на `https://smm-experttraveler.onrender.com/zapier/should-post`.
3. **Filter**: только если в ответе `should_post` = true и есть `post`.
4. **Action**: Telegram — Send Photo (URL из `post.photo_url`, Caption из `post.photo_caption`).
5. При необходимости ещё один шаг: Telegram — Send Message с `post.body_text`.

Подключение Telegram (бот и группа) делается в шагах 4–5 в Zapier — авторизация через Zapier.

### 2. Текущее расписание

**GET** `https://smm-experttraveler.onrender.com/zapier/schedule`

Возвращает: `next_post_time`, `frequency_hours`, `enabled`, `next_run_at`. Можно использовать, чтобы настроить расписание в Zapier вручную или для отладки.

### 3. Разовая генерация поста (без проверки расписания)

**POST** `https://smm-experttraveler.onrender.com/zapier/generate-post`

Генерирует пост и возвращает объект поста (`photo_url`, `photo_caption`, `body_text`, `full_text`). Подходит для ручного Zap или теста: Trigger по кнопке или по расписанию → этот URL → затем Telegram в Zapier.

### 4. Комментарии из группы (для контекста следующего поста)

**Вариант без Zapier (рекомендуется):** один и тот же бот-администратор добавлен в группу. Webhook приложения при получении любого **не-команды** из этой группы сохраняет сообщение как комментарий (в БД или `comments.json`). Конфликта нет: обновления приходят в webhook, команды обрабатываются, обычные сообщения группы — просто сохраняются. Второй бот не нужен.

**Вариант через Zapier:** если по какой-то причине бот не в группе, можно создать **второй Zap**: Trigger «Telegram — New Message in Chat» → Action Webhooks POST на `https://smm-experttraveler.onrender.com/zapier/comment` с полями `chat_id`, `message_id`, `text`.

При генерации поста приложение берёт последний сохранённый комментарий, проверяет его через OpenAI на тему путешествий (`is_travel_related`) и при совпадении подставляет в промпт (с обрезкой до 500 символов).

---

## Управление группами

Группы, в которые публикуется контент, настраиваются в Zapier: в шаге «Telegram» вы выбираете нужную группу (или несколько Zaps для разных групп). В приложении команды `/groups`, `/setgroup`, `/addgroup` по-прежнему доступны для учёта групп на стороне бота; фактическая публикация идёт в ту группу, которую вы выбрали в Zapier.

---

## Краткая схема

1. В боте-администраторе: `/settime 09:00`, `/setfreq 24` (время и частота).
2. В Zapier: Schedule (например, каждые 15 мин) → GET `/zapier/should-post` → при наличии поста — Telegram: Send Photo + при необходимости Send Message.
3. Подключение Telegram (бот и группа) — только в Zapier.

Так достигается: авторизация через Zapier (бот и группа), управление группами в Zapier, публикация по расписанию и настройка времени/частоты в боте-администраторе.
