"""Точка входа контейнера: права на volume SQLite, миграции, polling."""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config

from pubmed_bot.main import main as run_polling

_APP_UID = 1000
_APP_GID = 1000


def _data_dir() -> Path:
    raw = os.environ.get("SQLITE_PATH", "/data/pubmed.db")
    return Path(raw).expanduser().parent


def _drop_root(data_dir: Path) -> None:
    """Named volume часто root:root; процесс бота — uid 1000."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chown(data_dir, _APP_UID, _APP_GID)
    for path in data_dir.rglob("*"):
        os.chown(path, _APP_UID, _APP_GID)
    if hasattr(os, "setgroups"):
        os.setgroups([])
    os.setgid(_APP_GID)
    os.setuid(_APP_UID)
    os.environ["HOME"] = "/app"


def main() -> None:
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    _drop_root(data_dir)
    command.upgrade(Config("alembic.ini"), "head")
    run_polling()


if __name__ == "__main__":  # pragma: no cover
    main()
