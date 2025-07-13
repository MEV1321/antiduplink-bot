from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message

router = Router()

@router.message(Command("start"))
async def cmd_start(message: Message):
    ...

@router.message(Command("status"))
async def cmd_status(message: Message):
    ...

@router.message(F.text | F.caption)
async def check_duplicate_links(message: Message):
    ...