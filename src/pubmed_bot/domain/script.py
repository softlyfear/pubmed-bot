"""Детекция кириллицы по имени символа Unicode, не по «похоже на английский»."""

import unicodedata


def contains_cyrillic(text: str) -> bool:
    """True, если в строке есть хотя бы один символ скрипта Cyrillic."""
    return any(unicodedata.name(char, "").startswith("CYRILLIC") for char in text)
