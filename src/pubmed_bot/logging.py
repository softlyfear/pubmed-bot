"""Логирование в stdout (12-factor, удобно для Docker)."""

import logging
import sys


def setup_logging(level: str) -> None:
    """Включить логи в stdout с уровнем из LOG_LEVEL."""
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
        force=True,
    )
