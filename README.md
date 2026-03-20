# Marketplace Ops Tool

Простой внутренний операционный инструмент для команды маркетплейса.

## Стек

- FastAPI
- SQLite
- Jinja2
- SQLAlchemy
- APScheduler
- python-telegram-bot

## Что уже есть в проекте

- dashboard с базовой статистикой
- база задач
- импорт CSV
- импорт `plans.csv` из корня проекта с upsert по `sku + planned_date`
- планировщик по датам
- Telegram-уведомления
- журнал действий (`AuditLog`)
- `.env.example`
- `requirements.txt`

## Простая структура проекта

```text
app/
  config.py              # Настройки и чтение .env
  database.py            # Подключение к SQLite и сессии SQLAlchemy
  models.py              # Таблицы Task и ActionLog
  main.py                # Точка входа FastAPI
  routers/
    dashboard.py         # Главная страница dashboard
    tasks.py             # Экран задач, создание, CSV и смена статуса
  services/
    logger.py            # Запись в журнал действий
    scheduler.py         # Фоновый планировщик APScheduler
    telegram_service.py  # Отправка сообщений в Telegram
  templates/
    base.html            # Общий шаблон
    dashboard.html       # Главная панель
    tasks/
      list.html          # Экран задач
  static/
    style.css            # Стили интерфейса
  data/
    ops.db               # SQLite база создастся автоматически
```

## Как запустить

### 1. Создать виртуальное окружение

```bash
python -m venv .venv
```

Windows PowerShell:

```bash
.venv\Scripts\Activate.ps1
```

### 2. Установить зависимости

```bash
pip install -r requirements.txt
```

### 3. Создать `.env`

```bash
copy .env.example .env
```

Минимально можно оставить значения по умолчанию. Для Telegram заполните:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_DEFAULT_CHAT_ID`

### 4. Запустить приложение

```bash
uvicorn app.main:app --reload
```

После запуска:

- `http://127.0.0.1:8000/` - dashboard
- `http://127.0.0.1:8000/tasks/` - список задач, создание и импорт CSV

Чтобы импортировать план-файл из корня проекта:

1. Положите `plans.csv` рядом с `README.md`
2. Откройте dashboard
3. Нажмите кнопку `Import CSV`

## Формат CSV

Используйте такие заголовки:

```csv
title,description,assignee,due_date,telegram_chat_id
Проверить остатки,Сверить товары на складе,Ирина,2026-03-20 10:00,123456789
Подготовить отчет,Собрать метрики по акциям,Алексей,2026-03-21,
```

Поддерживаемые даты:

- `YYYY-MM-DD`
- `YYYY-MM-DD HH:MM`

Для корневого `plans.csv` ожидаются поля:

```csv
sku,planned_date,quantity,delivery_type,planned_price,comment,title
SKU-001,2026-03-20 10:00,120,fbo,1499,Priority delivery,Plan for SKU-001
```

## Что где находится

- Основной запуск приложения: `app/main.py`
- Настройки из `.env`: `app/config.py`
- Описание таблиц: `app/models.py`
- Подключение к базе: `app/database.py`
- Dashboard: `app/routers/dashboard.py`
- Работа с задачами и CSV: `app/routers/tasks.py`
- Планировщик: `app/services/scheduler.py`
- Telegram: `app/services/telegram_service.py`
- Журнал действий: `app/services/logger.py`
- HTML-шаблоны: `app/templates/`
- Стили: `app/static/style.css`

## Как это работает

### Dashboard

Показывает:

- количество всех задач
- количество открытых задач
- количество завершенных задач
- последние задачи
- последние действия в системе

### Планировщик

После запуска приложения фоновый планировщик:

- регулярно проверяет задачи с датой
- ищет задачи, срок которых наступил
- пытается отправить уведомление в Telegram
- пишет результат в журнал действий

### Журнал действий

Сохраняет:

- создание задачи
- импорт CSV
- изменение статуса
- работу планировщика

## Что можно добавить дальше

- редактирование и удаление задач
- фильтры по исполнителю и статусу
- авторизацию
- выгрузку отчетов
## Отзывы Ozon

В проект добавлен отдельный модуль для отзывов и вопросов Ozon.

Что уже подготовлено:

- SQLAlchemy-модель `Review`
- страница `/reviews`
- блок последних отзывов на dashboard
- сервис `app/services/ozon_reviews_service.py`

Где находится:

- модель: `app/models.py`
- маршруты: `app/routers/reviews.py`
- шаблон страницы: `app/templates/reviews/list.html`
- сервис-заглушка: `app/services/ozon_reviews_service.py`

Пока это только структура без реальной загрузки из API. Следующим шагом сюда можно будет добавить клиент Ozon API и синхронизацию отзывов в базу.
