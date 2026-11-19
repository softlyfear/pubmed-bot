"""Точка входа контейнера: права на каталог SQLite, миграции, polling."""

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


def _chown_tree(data_dir: Path, uid: int, gid: int) -> None:
    """Own the data directory. Do not follow symlinks planted in the volume."""
    os.chown(data_dir, uid, gid)
    for path in data_dir.rglob("*"):
        if path.is_symlink():
            continue
        os.chown(path, uid, gid, follow_symlinks=False)


def _drop_root(data_dir: Path) -> None:
    """Каталог данных часто принадлежит root; процесс бота — uid 1000."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    _chown_tree(data_dir, _APP_UID, _APP_GID)
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
