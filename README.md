# PubMed Bot

Telegram бот для поиска и управления научными статьями из PubMed. Полностью разработан с использованием AI.

## Возможности

- 🔍 **Поиск статей** в PubMed с автоматическим переводом запроса на английский и конвертацией в медицинскую терминологию
- 📚 **Избранное** - сохраняйте интересующие вас статьи
- 📝 **Заметки** - делайте заметки к статьям (до 2000 символов)
- 📧 **Подписки** - получайте новые статьи по вашему запросу раз в сутки
- 🌐 **Переводы** - автоматический перевод названий и аннотаций статей на русский (DeepL API)
- 💾 **Экспорт** - сохраняйте статьи в текстовый файл
- 🛡️ **Безопасность** - приватные чаты, защита от двойных кликов, rate limiting

## Архитектура

```
src/
├── pubmed_bot/
│   ├── adapters/          # Внешние API и БД
│   │   ├── db/            # SQLite через SQLAlchemy
│   │   └── ncbi/          # NCBI E-utilities
│   ├── bot/               # Telegram bot (aiogram 3)
│   │   ├── handlers/      # Обработчики команд и callback'ов
│   │   ├── keyboards.py   # Inline-клавиатуры
│   │   ├── formatting.py  # HTML-разметка сообщений
│   │   └── middlewares.py # FSM, rate limiting, блокировка двойных кликов
│   ├── domain/            # Бизнес-логика
│   │   ├── models.py      # Dataclasses для данных
│   │   └── exceptions.py  # Custom исключения
│   └── services/          # Service layer
│       ├── search.py      # Поиск с Gemini-переписью
│       ├── article.py     # Загрузка и перевод статей
│       ├── favorites.py   # Управление избранным
│       ├── notes.py       # Управление заметками
│       └── subscriptions/ # Управление подписками
```

## Стек технологий

- **Framework**: aiogram 3 (Telegram Bot API)
- **СУБД**: SQLite с SQLAlchemy 2 (async)
- **Переводы**: DeepL API
- **AI**: Gemini 3.5 Flash Lite (переписание запросов)
- **Научные API**: NCBI E-utilities
- **Миграции**: Alembic
- **Контейнеризация**: Docker + Docker Compose

## Установка и запуск

### Требования

- Docker + Docker Compose
- `.env` файл с API ключами (DeepL, Gemini, Telegram)

### Запуск

```bash
# С профилем pubmed-bot (изолированно)
docker compose --profile pubmed-bot up -d

# Без профиля (контейнер не запустится)
docker compose up -d

# Просмотр логов
docker compose logs -f bot

# Остановка
docker compose --profile pubmed-bot down
```

## Конфигурация

Создайте `.env` файл:

```env
TELEGRAM_TOKEN=your_token_here
DEEPL_API_KEY=your_key_here
GEMINI_API_KEY=your_key_here
SQLITE_PATH=/data/pubmed.db
USER_SEARCH_PER_MIN=10
USER_OPEN_PER_MIN=30
```

## Разработка

### Type checking и форматирование

```bash
# Type checking
mypy src/

# Linting и форматирование
ruff check src/ tests/
ruff format src/ tests/
```

### Тестирование

```bash
pytest tests/
```

## Особенности реализации

### Безопасность

- Приватные чаты только (игнорируются группы и каналы)
- Защита от двойных кликов через `ProcessingBlockerMiddleware`
- Rate limiting на поиск (10 запросов/мин) и открытие статей (30 действий/мин)
- Никаких API ключей в логах и исходниках

### Оптимизация

- Асинхронная обработка всех операций
- FSM для управления состоянием пользователя
- Кеширование переводов в БД
- Оптимальные параметры поиска в NCBI (документы с Abstract, Filtered for Humans)

### UI/UX

- HTML-разметка с корректной обработкой тегов
- Нумерованные списки с компактными кнопками
- Навигация "Главное меню" и "Новый запрос" на всех экранах
- Alert-сообщения для блокировок и ошибок
- Переводы для всех пользовательских сообщений

## Автор

Разработано полностью с использованием AI.

## Лицензия

MIT
