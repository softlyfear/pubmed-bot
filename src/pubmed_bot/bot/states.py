"""FSM: область поиска и текст заметки."""

from aiogram.fsm.state import State, StatesGroup


class SearchStates(StatesGroup):
    waiting_query = State()


class NoteStates(StatesGroup):
    waiting_body = State()
