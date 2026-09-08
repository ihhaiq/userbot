"""
main.py
========
نقطة تشغيل المشروع الكاملة:
    1. يقرأ متغيرات البيئة (من .env محلياً، أو مباشرة من بيئة Railway).
    2. يتأكد من وجود gift.json في الفوليوم — إن لم يوجد، ينسخ النسخة
       الافتراضية المرفقة مع المشروع، وإن وُجد يقرأه فقط دون تعديله.
    3. يهيئ حساب اليوزربوت (Telethon) عبر SESSION_STRING الجاهزة مسبقاً
       (بدون أي تسجيل دخول تفاعلي).
    4. يهيئ بوت الواجهة (AsyncTeleBot) ويشغّله عبر infinity_polling().
"""

import asyncio
import logging
import os
import shutil

from telethon import TelegramClient
from telethon.sessions import StringSession
from telebot.async_telebot import AsyncTeleBot

import front_system

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def load_dotenv_if_present(path: str = ".env") -> None:
    """محمّل .env بسيط بدون أي مكتبة خارجية — للتطوير المحلي فقط.
    على Railway، متغيرات البيئة تُحقن مباشرة من لوحة التحكم ولا حاجة لهذا الملف."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def ensure_gifts_file(volume_path: str) -> None:
    """ينشئ ملف الهدايا من النسخة الافتراضية عند أول تشغيل فقط.

    إذا كان الملف موجوداً مسبقاً فلا تتم الكتابة فوقه، لأن إدارة الهدايا
    والهدية التلقائية تُحفظ داخله ويجب أن تبقى بعد إعادة التشغيل/النشر.
    """
    default_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gifts.json")

    if os.path.exists(volume_path):
        logger.info("gifts.json موجود مسبقاً: %s", volume_path)
        return

    os.makedirs(os.path.dirname(volume_path) or ".", exist_ok=True)
    shutil.copy(default_path, volume_path)
    logger.info("لم يوجد الملف، تم نسخ النسخة الافتراضية إلى: %s", volume_path)


async def main() -> None:
    load_dotenv_if_present()

    bot_token = os.environ["BOT_TOKEN"]
    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    owner_ids_raw = os.environ.get("OWNER_IDS") or os.environ.get("OWNER_ID")
    if not owner_ids_raw:
        raise RuntimeError("يجب تحديد OWNER_IDS (أو OWNER_ID للتوافق القديم).")
    try:
        owner_ids = {
            int(value.strip())
            for value in owner_ids_raw.split(",")
            if value.strip()
        }
    except ValueError as exc:
        raise RuntimeError("OWNER_IDS يجب أن يحتوي أرقام Telegram مفصولة بفواصل.") from exc
    if not owner_ids:
        raise RuntimeError("OWNER_IDS لا يحتوي أي معرف صالح.")

    session_string = os.environ["SESSION_STRING"]
    default_gifts_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gifts.json")
    gifts_volume_path = os.environ.get("GIFTS_VOLUME_PATH") or default_gifts_path

    ensure_gifts_file(gifts_volume_path)

    telethon_client = TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash,
        request_retries=5,
        connection_retries=10,
        retry_delay=2,
        auto_reconnect=True,
        flood_sleep_threshold=60,
    )
    bot = AsyncTeleBot(bot_token)

    front_system.setup(bot, telethon_client, gifts_volume_path, owner_ids)

    try:
        async with telethon_client:
            me = await telethon_client.get_me()
            logger.info("تم الاتصال بالحساب المضيف: %s", getattr(me, "username", me.id))
            logger.info("بدء تشغيل بوت الواجهة...")
            await bot.infinity_polling(
                skip_pending=True,
                timeout=30,
                request_timeout=45,
                logger_level=logging.ERROR,
                allowed_updates=["message", "callback_query"],
            )
    finally:
        await front_system.close_resources()
        await bot.close_session()


if __name__ == "__main__":
    asyncio.run(main())
