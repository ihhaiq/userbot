"""
generate_session.py
====================
شغّل هذا الملف مرة واحدة فقط، محلياً على جهازك (وليس على Railway)،
لتسجيل دخول حساب اليوزربوت الحقيقي وتوليد SESSION_STRING.

بعد التوليد، ضع القيمة الناتجة في .env كـ SESSION_STRING — بهذا
لن يحتاج backend_system.py لأي تسجيل دخول تفاعلي (رقم هاتف / كود)
أثناء التشغيل الفعلي على السيرفر.

الاستخدام:
    python generate_session.py
"""

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = int(input("API_ID: "))
API_HASH = input("API_HASH: ")

with TelegramClient(StringSession(), API_ID, API_HASH) as client:
    print("\n=== انسخ هذا السطر إلى SESSION_STRING في ملف .env ===\n")
    print(client.session.save())
    print("\n=== لا تشارك هذه القيمة مع أي أحد — من يملكها يملك دخولاً كاملاً على الحساب ===\n")
