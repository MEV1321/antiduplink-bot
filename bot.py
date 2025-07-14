import os
import re
import logging
import asyncio
import json
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.enums import ParseMode, MessageEntityType
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
import redis.asyncio as redis
from aiohttp import web

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Инициализация бота
BOT_TOKEN = os.getenv("BOT_TOKEN")
REDIS_URL = os.getenv("REDIS_URL")
USE_HTTP_SERVER = os.getenv("USE_HTTP_SERVER", "0") == "1"  # Для Render Web Services

if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN не найден! Проверьте переменные окружения.")
    exit(1)

bot = Bot(token=BOT_TOKEN)

# Подключение к Redis
redis_client = None
storage = None

if REDIS_URL:
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        storage = RedisStorage(redis=redis_client)
        logger.info("✅ Подключено к Redis")
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к Redis: {e}")
        redis_client = None
        storage = None
else:
    logger.warning("REDIS_URL не указан. Используется in-memory хранилище.")

dp = Dispatcher(storage=storage)

# Простой HTTP-сервер для Render Web Services
async def web_server():
    app = web.Application()
    app.router.add_get('/', handle_root)
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Явно указываем порт и хост
    site = web.TCPSite(runner, host='0.0.0.0', port=10000)
    await site.start()
    
    # Добавляем проверку запуска
    logger.info(f"✅ HTTP-сервер запущен на порту 10000")
    return site  # Возвращаем объект сервера

async def handle_root(request):
    return web.Response(text="Anti-Duplicate Link Bot is running")

# Функции для счетчика очистки
async def increment_cleanup_counter(chat_id: int) -> int:
    """Увеличивает счетчик сообщений и возвращает текущее значение"""
    if not redis_client:
        return 0
        
    key = f"chat:{chat_id}:counter"
    try:
        count = await redis_client.incr(key)
        if count >= 365:
            await redis_client.set(key, 0)
        return count
    except Exception as e:
        logger.error(f"Ошибка счетчика очистки: {e}")
        return 0

# Основные функции
def normalize_url(url: str) -> str:
    """Приводим URL к единому виду для сравнения"""
    url = url.split('?')[0].split('#')[0]
    if url.endswith('/'):
        url = url[:-1]
    return url.lower()

def extract_links(message: types.Message) -> list:
    """Извлекает ссылки из текста и подписей с учётом Telegram entities"""
    urls = []
    text = message.text or message.caption or ""
    
    entities = message.entities or message.caption_entities or []
    for entity in entities:
        if entity.type == MessageEntityType.URL:
            url = text[entity.offset:entity.offset + entity.length]
            urls.append(url)
        elif entity.type == MessageEntityType.TEXT_LINK:
            urls.append(entity.url)
    
    if not urls:
        url_pattern = re.compile(
            r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+[/\w\.\-?=%&#@!$+]*'
        )
        urls = url_pattern.findall(text)
    
    normalized = []
    for url in urls:
        n_url = normalize_url(url)
        if n_url not in normalized:
            normalized.append(n_url)
    
    return normalized

async def save_link(chat_id: int, url: str, message_id: int):
    """Сохраняет ссылку в Redis"""
    if not redis_client:
        return
    
    # Получаем текущие данные или создаем новые
    link_data = await get_link_data(chat_id, url) or {
        "message_id": message_id,
        "timestamp": datetime.now().isoformat(),
        "likes": {},    # Словарь лайков
        "thumbs_up": {} # Словарь реакций 👍
    }
    
    # Обновляем ID сообщения и время
    link_data["message_id"] = message_id
    link_data["timestamp"] = datetime.now().isoformat()
    
    # Сохраняем в Redis
    await redis_client.hset(
        f"chat:{chat_id}", 
        url, 
        json.dumps(link_data)
    )

async def get_link_data(chat_id: int, url: str) -> dict:
    """Получает данные о ссылке из Redis"""
    if not redis_client:
        return None
    
    data = await redis_client.hget(f"chat:{chat_id}", url)
    return json.loads(data) if data else None

async def add_reaction(chat_id: int, url: str, user_id: int, username: str, reaction_type: str):
    """Добавляет реакцию к ссылке"""
    if not redis_client:
        return False
    
    link_data = await get_link_data(chat_id, url)
    if not link_data:
        return False
    
    # Инициализируем словари реакций
    for r_type in ["likes", "thumbs_up"]:
        if r_type not in link_data:
            link_data[r_type] = {}
    
    # Добавляем реакцию
    if reaction_type == "like":
        link_data["likes"][str(user_id)] = username
    elif reaction_type == "thumbs_up":
        link_data["thumbs_up"][str(user_id)] = username
    
    # Сохраняем обновленные данные
    await redis_client.hset(f"chat:{chat_id}", url, json.dumps(link_data))
    return True

async def cleanup_old_links(chat_id: int):
    """Удаляет старые ссылки (старше 365 дней)"""
    if not redis_client:
        return
    
    all_links = await redis_client.hgetall(f"chat:{chat_id}")
    now = datetime.now()
    keys_to_delete = []
    
    for url, data_json in all_links.items():
        try:
            data = json.loads(data_json)
            timestamp = datetime.fromisoformat(data["timestamp"])
            
            if (now - timestamp) > timedelta(days=365):
                keys_to_delete.append(url)
        except Exception as e:
            logger.error(f"Ошибка при очистке ссылки: {e}")
            continue
    
    # Удаляем все устаревшие ссылки одним запросом
    if keys_to_delete:
        await redis_client.hdel(f"chat:{chat_id}", *keys_to_delete)
        logger.info(f"Удалено {len(keys_to_delete)} устаревших ссылок в чате {chat_id}")

async def delete_after_delay(message: types.Message, delay: int = 600):
    """Удаляет сообщение через указанную задержку (в секундах)"""
    await asyncio.sleep(delay)
    try:
        await message.delete()
        logger.info(f"Сообщение бота {message.message_id} удалено после задержки")
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        logger.warning(f"Не удалось удалить сообщение бота: {e}")
    except Exception as e:
        logger.error(f"Ошибка при удалении сообщения бота: {e}")

async def generate_stats(chat_id: int) -> str:
    """Генерирует статистику реакций для всех ссылок в чате"""
    if not redis_client:
        return "Статистика недоступна: Redis не подключен."
    
    all_links = await redis_client.hgetall(f"chat:{chat_id}")
    if not all_links:
        return "В этом чате еще нет сохраненных ссылок."
    
    stats = []
    for url, data_json in all_links.items():
        try:
            data = json.loads(data_json)
            
            # Форматируем статистику
            link_stats = []
            
            # Лайки
            likes = data.get("likes", {})
            if likes:
                users = [f"@{username}" if username else f"id{user_id}" 
                         for user_id, username in likes.items()]
                link_stats.append(f"❤️ Лайки: {len(likes)} ({', '.join(users)})")
            
            # Большие пальцы вверх
            thumbs_up = data.get("thumbs_up", {})
            if thumbs_up:
                users = [f"@{username}" if username else f"id{user_id}" 
                         for user_id, username in thumbs_up.items()]
                link_stats.append(f"👍 Большие пальцы: {len(thumbs_up)} ({', '.join(users)})")
            
            if link_stats:
                stats.append(f"🔗 {url}\n" + "\n".join(link_stats) + "\n")
        except Exception as e:
            logger.error(f"Ошибка при обработке статистики для {url}: {e}")
    
    if not stats:
        return "Пока никто не оценил ссылки в этом чате."
    
    return "📊 Статистика реакций:\n\n" + "\n".join(stats)

# Обработчики сообщений
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.chat.type == "private":
        await message.answer(
            "🛡️ Я бот-антидубликатор ссылок с постоянной памятью!\n\n"
            "Добавь меня в группу как администратора с правами:\n"
            "• Удалять сообщения\n"
            "• Видеть историю сообщений\n\n"
            "Я запоминаю все ссылки на 365 дней, даже после перезапуска!\n\n"
            "Также я умею собирать статистику реакций по ссылкам!",
            parse_mode=ParseMode.HTML
        )

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    """Показывает статистику хранилища"""
    chat_id = message.chat.id
    if redis_client:
        link_count = await redis_client.hlen(f"chat:{chat_id}")
        await message.answer(
            f"📊 Статус хранилища:\n\n"
            f"• Ссылок в памяти: <b>{link_count}</b>\n"
            f"• Данные сохраняются в Redis\n"
            f"• Срок хранения: 365 дней",
            parse_mode=ParseMode.HTML
        )
    else:
        await message.answer(
            "ℹ️ Используется временное хранилище в памяти. "
            "Данные будут потеряны при перезапуска бота.",
            parse_mode=ParseMode.HTML
        )

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    """Показывает статистику реакций"""
    chat_id = message.chat.id
    stats = await generate_stats(chat_id)
    await message.answer(stats, parse_mode=ParseMode.HTML)

@dp.message(F.text | F.caption)
async def check_duplicate_links(message: types.Message):
    # Пропускаем служебные сообщения и самого бота
    if message.sender_chat or (message.from_user and message.from_user.id == (await bot.me()).id):
        return
    
    chat_id = message.chat.id
    links = extract_links(message)
    
    if not links:
        # Обработка реакций
        if message.reply_to_message:
            replied_message = message.reply_to_message
            replied_links = extract_links(replied_message)
            
            if replied_links:
                user_id = message.from_user.id
                username = message.from_user.username
                text = message.text.lower() if message.text else ""
                
                # Определяем тип реакции
                reaction_type = None
                if text in ["нравится", "like"]:
                    reaction_type = "like"
                elif "👍" in text or "thumb" in text:
                    reaction_type = "thumbs_up"
                
                if reaction_type:
                    for link in replied_links:
                        success = await add_reaction(
                            chat_id, 
                            link, 
                            user_id, 
                            username,
                            reaction_type
                        )
                    
                    if success:
                        await message.reply("✅ Ваша реакция учтена!")
                        asyncio.create_task(delete_after_delay(message, delay=10))
                    return
        return
    
    # Шаг 1: Проверка на дубликаты
    duplicate_found = False
    duplicate_url = None
    original_message_id = None
    
    for link in links:
        link_data = await get_link_data(chat_id, link)
        if link_data:
            duplicate_found = True
            duplicate_url = link
            original_message_id = link_data["message_id"]
            break
    
    # Шаг 2: Если дубликат найден - удаляем и уведомляем
    if duplicate_found:
        try:
            await message.delete()
        except TelegramForbiddenError:
            logger.error(f"Нет прав на удаление в чате {chat_id}")
            await message.reply("⚠️ У меня нет прав удалять сообщения! Проверьте права администратора.")
            return
        except TelegramBadRequest as e:
            logger.error(f"Ошибка удаления: {e}")
            return
        
        # Формируем ссылку на оригинальное сообщение
        if str(chat_id).startswith("-100"):
            chat_part = str(chat_id)[4:]
        else:
            chat_part = chat_id
        
        response = (
            f"👮♂️ <b>Обнаружен дубликат ссылки!</b>\n\n"
            f"Я нашел аналогичную ссылку в истории сообщений:\n"
            f"<code>{duplicate_url}</code>\n\n"
            f"<a href='https://t.me/c/{chat_part}/{original_message_id}'>→ Перейти к оригиналу</a>"
        )
        
        try:
            # Отправляем сообщение о дубликате
            warning_msg = await bot.send_message(
                chat_id=chat_id,
                text=response,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            
            # Запускаем задачу для удаления через 15 минут (600 секунд)
            asyncio.create_task(delete_after_delay(warning_msg))
        except Exception as e:
            logger.error(f"Ошибка при отправке предупреждения: {e}")
        
        return
    
    # Шаг 3: Если дубликатов нет - сохраняем ВСЕ ссылки из сообщения
    for link in links:
        await save_link(chat_id, link, message.message_id)
    
    logger.info(f"Сохранено {len(links)} ссылок в чате {chat_id}")

    # Шаг 4: Периодическая очистка старых ссылок (1 раз на 365 сообщений)
    current_count = await increment_cleanup_counter(chat_id)
    if current_count >= 365:
        await cleanup_old_links(chat_id)
        logger.info(f"Запущена очистка старых ссылок в чате {chat_id}")

    # Шаг 5: Добавляем кнопки реакций к сообщению со ссылкой
    if message.chat.type != "private":
        builder = InlineKeyboardBuilder()
        builder.button(text="❤️ Нравится", callback_data=f"reaction_like_{message.message_id}")
        builder.button(text="👍 Палец вверх", callback_data=f"reaction_thumbs_{message.message_id}")
        builder.adjust(2)  # 2 кнопки в ряд
        
        await message.reply(
            "Оцените ссылку:",
            reply_markup=builder.as_markup()
        )

# Обработчик нажатий на кнопки
@dp.callback_query(F.data.startswith("reaction_"))
async def handle_reaction_callback(callback: types.CallbackQuery):
    try:
        # Извлекаем тип реакции и ID сообщения
        parts = callback.data.split("_")
        reaction_type = parts[1]  # like или thumbs
        message_id = int(parts[2])
        
        # Получаем сообщение, к которому привязана кнопка
        message = await bot.get_message(
            chat_id=callback.message.chat.id,
            message_id=message_id
        )
        
        # Извлекаем ссылки из сообщения
        chat_id = callback.message.chat.id
        links = extract_links(message)
        
        if links:
            user_id = callback.from_user.id
            username = callback.from_user.username
            
            for link in links:
                await add_reaction(
                    chat_id, 
                    link, 
                    user_id, 
                    username,
                    reaction_type
                )
            
            # Подтверждение пользователю
            emoji = "❤️" if reaction_type == "like" else "👍"
            await callback.answer(f"{emoji} Ваша реакция учтена!")
            
            # Удаляем сообщение с кнопками
            await callback.message.delete()
        else:
            await callback.answer("❌ Ссылки не найдены")
    
    except Exception as e:
        logger.error(f"Ошибка обработки реакции: {e}")
        await callback.answer("❌ Произошла ошибка")

# Запуск бота
async def main():
    logger.info("Starting bot...")
    
    # Проверка подключения к Redis
    if redis_client:
        try:
            await redis_client.ping()
            logger.info("Redis подключен и отвечает")
        except Exception as e:
            logger.error(f"Ошибка подключения к Redis: {e}")
    
    # Запускаем HTTP-сервер СИНХРОННО перед ботом
    http_server = None
    if USE_HTTP_SERVER:
        http_server = await web_server()  # Ждем полного запуска
    
    # Убеждаемся, что сервер запущен
    await asyncio.sleep(2)  # Даем время на инициализацию
    
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Запуск поллинга...")
    me = await bot.get_me()
    logger.info(f"Бот запущен: @{me.username} (ID: {me.id})")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())