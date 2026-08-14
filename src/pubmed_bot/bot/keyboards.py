"""Inline-клавиатуры. Slash-команда только /start."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from pubmed_bot.bot.texts import BTN_FAV_ADD, BTN_FAV_REMOVE, BTN_NEW_QUERY, BTN_NOTES

CALLBACK_OPEN_PREFIX = "o:"
CALLBACK_FIND = "m:find"
CALLBACK_DOMAIN_PREFIX = "d:"
FIND_CALLBACKS = frozenset(
    {
        CALLBACK_FIND,
        f"{CALLBACK_DOMAIN_PREFIX}sport",
        f"{CALLBACK_DOMAIN_PREFIX}medicine",
        f"{CALLBACK_DOMAIN_PREFIX}other",
    }
)
CALLBACK_MORE = "p:next"
CALLBACK_FAV = "m:fav"
CALLBACK_NOTES = "m:notes"
CALLBACK_MAIN = "m:main"
CALLBACK_SUB = "m:sub"
CALLBACK_SUB_ADD = "s:add"
CALLBACK_SUB_OFF_PREFIX = "s:x:"
CALLBACK_BACK = "b:list"
CALLBACK_FAV_ADD_PREFIX = "f:"
CALLBACK_FAV_DEL_PREFIX = "fd:"
CALLBACK_NOTE_PREFIX = "n:"
CALLBACK_NOTE_DEL_PREFIX = "nd:"
CALLBACK_EXPORT_PREFIX = "x:"
MAX_CALLBACK_BYTES = 64


def article_callback_data(pmid: str) -> str:
    """callback_data открытия статьи: `o:<pmid>`, не длиннее 64 байт."""
    return _pmid_callback(CALLBACK_OPEN_PREFIX, pmid)


def fav_add_callback_data(pmid: str) -> str:
    return _pmid_callback(CALLBACK_FAV_ADD_PREFIX, pmid)


def fav_del_callback_data(pmid: str) -> str:
    """callback_data удаления из избранного: `fd:<pmid>`."""
    return _pmid_callback(CALLBACK_FAV_DEL_PREFIX, pmid)


def note_callback_data(pmid: str) -> str:
    return _pmid_callback(CALLBACK_NOTE_PREFIX, pmid)


def note_del_callback_data(pmid: str) -> str:
    return _pmid_callback(CALLBACK_NOTE_DEL_PREFIX, pmid)


def export_callback_data(pmid: str) -> str:
    return _pmid_callback(CALLBACK_EXPORT_PREFIX, pmid)


def _pmid_callback(prefix: str, pmid: str) -> str:
    data = f"{prefix}{pmid}"
    if len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        msg = "callback_data превышает 64 байта"
        raise ValueError(msg)
    return data


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Найти", callback_data=CALLBACK_FIND)],
            [
                InlineKeyboardButton(text="Избранное", callback_data=CALLBACK_FAV),
                InlineKeyboardButton(text=BTN_NOTES, callback_data=CALLBACK_NOTES),
                InlineKeyboardButton(text="Подписки", callback_data=CALLBACK_SUB),
            ],
        ]
    )


def list_keyboard(
    pmids: tuple[str, ...],
    *,
    has_more: bool,
    with_subscribe: bool = False,
    with_new_query: bool = False,
) -> InlineKeyboardMarkup:
    """Нумерованный список PMID; «Новый запрос» — отдельный ряд, только если with_new_query."""
    rows = _numbered_pmid_rows(pmids)
    extra: list[InlineKeyboardButton] = []
    if has_more:
        extra.append(InlineKeyboardButton(text="Ещё", callback_data=CALLBACK_MORE))
    if with_subscribe:
        extra.append(
            InlineKeyboardButton(text="Подписка на этот запрос", callback_data=CALLBACK_SUB_ADD)
        )
    if extra:
        rows.append(extra)
    if with_new_query:
        rows.append([
            InlineKeyboardButton(text=BTN_NEW_QUERY, callback_data=CALLBACK_FIND),
            InlineKeyboardButton(text="Главное меню", callback_data=CALLBACK_MAIN),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def favorites_list_keyboard(pmids: tuple[str, ...]) -> InlineKeyboardMarkup:
    """Номера `o:<pmid>` с кнопками удаления; без поиска и подписки."""
    rows: list[list[InlineKeyboardButton]] = []
    for index, pmid in enumerate(pmids, start=1):
        rows.append([
            InlineKeyboardButton(text=str(index), callback_data=article_callback_data(pmid)),
            InlineKeyboardButton(
                text="Удалить",
                callback_data=fav_del_callback_data(pmid),
            ),
        ])
    rows.append([InlineKeyboardButton(text="Главное меню", callback_data=CALLBACK_MAIN)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def notes_list_keyboard(pmids: tuple[str, ...]) -> InlineKeyboardMarkup:
    """Три кнопки для каждой заметки: открыть статью, редактировать, удалить."""
    rows: list[list[InlineKeyboardButton]] = []
    for index, pmid in enumerate(pmids, start=1):
        rows.append([
            InlineKeyboardButton(text=str(index), callback_data=article_callback_data(pmid)),
            InlineKeyboardButton(text="Править", callback_data=note_callback_data(pmid)),
            InlineKeyboardButton(text="Удалить", callback_data=note_del_callback_data(pmid)),
        ])
    rows.append([InlineKeyboardButton(text="Главное меню", callback_data=CALLBACK_MAIN)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _numbered_pmid_rows(pmids: tuple[str, ...]) -> list[list[InlineKeyboardButton]]:
    buttons = [
        InlineKeyboardButton(text=str(index), callback_data=article_callback_data(pmid))
        for index, pmid in enumerate(pmids, start=1)
    ]
    return [buttons[offset : offset + 5] for offset in range(0, len(buttons), 5)]


def subscriptions_keyboard(ids: tuple[int, ...]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"Отключить {index}",
                callback_data=f"{CALLBACK_SUB_OFF_PREFIX}{sub_id}",
            )
        ]
        for index, sub_id in enumerate(ids, start=1)
    ]
    rows.append([InlineKeyboardButton(text="Главное меню", callback_data=CALLBACK_MAIN)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def article_keyboard(
    pmid: str | None = None,
    *,
    is_favorite: bool = False,
) -> InlineKeyboardMarkup:
    """Назад; на карточке — избранное (добавить или убрать), заметка, экспорт."""
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="Назад к списку", callback_data=CALLBACK_BACK)],
    ]
    if pmid:
        fav_text = BTN_FAV_REMOVE if is_favorite else BTN_FAV_ADD
        fav_data = fav_del_callback_data(pmid) if is_favorite else fav_add_callback_data(pmid)
        rows.append(
            [
                InlineKeyboardButton(text=fav_text, callback_data=fav_data),
                InlineKeyboardButton(text="Заметка", callback_data=note_callback_data(pmid)),
                InlineKeyboardButton(text="Экспорт", callback_data=export_callback_data(pmid)),
            ]
        )
    rows.append([
        InlineKeyboardButton(text="Новый запрос", callback_data=CALLBACK_FIND),
        InlineKeyboardButton(text="Главное меню", callback_data=CALLBACK_MAIN),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
