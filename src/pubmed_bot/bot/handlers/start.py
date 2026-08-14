"""/start: дисклеймер и выбор области."""

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from pubmed_bot.bot.keyboards import start_keyboard
from pubmed_bot.bot.texts import START_TEXT

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(START_TEXT, reply_markup=start_keyboard())
