# pubmed-bot

Telegram bot for PubMed search, favorites, notes, subscriptions, and Russian
translation. aiogram 3, SQLite via SQLAlchemy async, Alembic, DeepL, and Gemini.

## Architecture

- `src/pubmed_bot/adapters/`: NCBI, DeepL, Gemini, and database adapters.
- `src/pubmed_bot/domain/`: dataclasses and domain errors.
- `src/pubmed_bot/services/`: search, article, favorites, notes, subscriptions,
  translation, rate limiting, audit, and export.
- `src/pubmed_bot/bot/`: aiogram factory, handlers, keyboards, formatting,
  middlewares, states, and texts.
- `src/pubmed_bot/workers/`: subscription and audit workers.
- `src/pubmed_bot/config.py`, `container.py`, `main.py`: settings and composition.
- `tests/`: pytest tests with mocked adapters and fixtures.

## Commands

```sh
uv sync --frozen
uv run pytest
uv run ty check
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

## Security and privacy

- Private chats only; keep the double-click guard and rate limiting.
- Do not log API keys, bot tokens, user queries, or article text.
- Tests mock NCBI, DeepL, and Gemini; do not call live APIs in tests.
- Keep the SQLite database inside the project and out of Git.
- Do not change provider/model or auth without authorization.

## Git

Follow the global Git rules. One completed change per commit. Author and
committer come from `git config`.
