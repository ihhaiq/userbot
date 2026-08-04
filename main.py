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
    """يضمن أن ملف الهدايا المستخدم من قبل البوت يحتوي على أحدث نسخة من
    gifts.json المحلي. إذا كان الملف الهدف غير موجود، يُنشأ من الملف المحلي.
    وإذا كان الملف المحلي أحدث من الهدف، يتم مزامنته أيضاً."""
    default_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gifts.json")

    if os.path.exists(volume_path):
        if os.path.exists(default_path):
            try:
                if os.path.getmtime(default_path) > os.path.getmtime(volume_path):
                    shutil.copy(default_path, volume_path)
                    logger.info("تمت مزامنة ملف الهدايا المحلي إلى: %s", volume_path)
            except OSError:
                pass
        logger.info("gift.json موجود مسبقاً في الفوليوم: %s", volume_path)
        return

    os.makedirs(os.path.dirname(volume_path) or ".", exist_ok=True)
    shutil.copy(default_path, volume_path)
    logger.info("لم يوجد الملف، تم نسخ النسخة الافتراضية إلى: %s", volume_path)


async def main() -> None:
    load_dotenv_if_present()

    bot_token = os.environ["BOT_TOKEN"]
    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    owner_id = int(os.environ["OWNER_ID"])
    session_string = os.environ["SESSION_STRING"]
    default_gifts_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gifts.json")
    gifts_volume_path = os.environ.get("GIFTS_VOLUME_PATH", default_gifts_path)
    coupons_volume_path = os.environ.get("COUPONS_VOLUME_PATH", "coupons.json")

    ensure_gifts_file(gifts_volume_path)

    telethon_client = TelegramClient(StringSession(session_string), api_id, api_hash)
    bot = AsyncTeleBot(bot_token)

    front_system.setup(bot, telethon_client, gifts_volume_path, owner_id, coupons_volume_path)

    async with telethon_client:
        me = await telethon_client.get_me()
        logger.info("تم الاتصال بالحساب المضيف: %s", getattr(me, "username", me.id))
        logger.info("بدء تشغيل بوت الواجهة...")
        # skip_pending=True: يتجاهل أي رسائل/تحديثات وصلت قبل بدء هذا التشغيل
        await bot.infinity_polling(skip_pending=True)


if __name__ == "__main__":
    asyncio.run(main())