"""
front_system.py
================
الواجهة الأمامية: بوت تيليجرام عادي (AsyncTeleBot) مقيّد بالكامل على
OWNER_ID فقط — أي رسالة أو زر من أي حساب آخر يُرفض برسالة "غير مرخّص".

يعرض كتالوج الهدايا، يطلب تحديد المستلم (لنفسي / لغيري)، يجمع الإعدادات
(إخفاء الاسم + تعليق)، ثم يمرر كل شيء إلى backend_system لتنفيذ عملية
الإرسال الفعلية من رصيد الحساب المضيف (Userbot).

آلة الحالة لكل مستخدم محفوظة في SESSIONS (قاموس في الذاكرة، "الفقاعة
المؤقتة") وتُمسح بالكامل عند التأكيد أو التراجع أو أي فشل يوقف العملية.

مراحل التدفق (session["stage"]):
    choosing_gift    -> اختيار الهدية من الكتالوج
    choosing_target  -> لنفسي / لغيري
    waiting_target   -> بانتظار id أو username المستلم (فقط عند "لغيري")
    choosing_hide    -> اختيار إخفاء/إظهار الاسم
    waiting_comment  -> بانتظار تعليق نصي أو ضغط "بدون تعليق"
    confirming       -> عرض ملخّص وانتظار تأكيد/تراجع
"""

import json
import logging
import os
import time
from typing import Optional

from telebot import types
from telebot.async_telebot import AsyncTeleBot
from telethon import events

import backend_system
import coupon_system
from backend_system import GiftErrorCode

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# حالة كل مستخدم أثناء تدفق اختيار الهدية ("الفقاعة المؤقتة")
# ---------------------------------------------------------------------------
SESSIONS: dict = {}

STAGE_CHOOSING_GIFT = "choosing_gift"
STAGE_CHOOSING_TARGET = "choosing_target"
STAGE_WAITING_TARGET = "waiting_target"
STAGE_CHOOSING_HIDE = "choosing_hide"
STAGE_WAITING_COMMENT = "waiting_comment"
STAGE_CONFIRMING = "confirming"

# مراحل تدفق "القسائم" (لوحة المطوّر)
STAGE_COUPON_WAITING_CODE = "coupon_waiting_code"
STAGE_COUPON_WAITING_MAX_USES = "coupon_waiting_max_uses"
STAGE_COUPON_WAITING_TTL = "coupon_waiting_ttl"
STAGE_COUPON_SETTINGS_WAITING_COMMENT = "coupon_settings_waiting_comment"
STAGE_ADD_GIFT_WAITING_ID = "add_gift_waiting_id"
STAGE_ADD_GIFT_WAITING_EMOJI = "add_gift_waiting_emoji"
STAGE_ADD_GIFT_WAITING_PRICE = "add_gift_waiting_price"

GIFTS: list = []          # كتالوج الهدايا المحمّل في الذاكرة
GIFTS_PATH: str = ""      # مسار ملف gift.json داخل الفوليوم
OWNER_ID: int = 0
TG_CLIENT = None          # كائن TelegramClient (Telethon) الخاص بالحساب المضيف


def clear_session(user_id: int) -> None:
    SESSIONS.pop(user_id, None)


# ---------------------------------------------------------------------------
# تحميل كتالوج الهدايا
# ---------------------------------------------------------------------------
def _resolve_gifts_path(path: str) -> str:
    if path and os.path.exists(path):
        return path

    local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gifts.json")
    if os.path.exists(local_path):
        return local_path

    return path or local_path


def load_gifts_from_disk() -> list:
    resolved_path = _resolve_gifts_path(GIFTS_PATH)
    with open(resolved_path, "r", encoding="utf-8") as f:
        return json.load(f)


def reload_gifts() -> int:
    global GIFTS
    GIFTS = load_gifts_from_disk()
    logger.info("تم تحميل %d هدية من %s", len(GIFTS), GIFTS_PATH)
    return len(GIFTS)


def find_gift(local_id: int) -> Optional[dict]:
    return next((g for g in GIFTS if g["id"] == local_id), None)


# ---------------------------------------------------------------------------
# بناء لوحات الأزرار
# ---------------------------------------------------------------------------
def build_gifts_keyboard() -> types.InlineKeyboardMarkup:
    """صفان لكل صف، والباقي يتوزع تلقائياً على صفوف جديدة."""
    markup = types.InlineKeyboardMarkup(row_width=2)
    row = []
    for item in GIFTS:
        btn_kwargs = dict(
            text=f"{item['prix']}",
            callback_data=f"g:{item['id']}",
            style="primary",
        )
        if item.get("custum_gift_icon"):
            btn_kwargs["icon_custom_emoji_id"] = item["custum_gift_icon"]
        row.append(types.InlineKeyboardButton(**btn_kwargs))
        if len(row) == 2:
            markup.row(*row)
            row = []
    if row:
        markup.row(*row)
    return markup


def build_target_keyboard() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("لنفسي", callback_data="target:self", style="primary"),
        types.InlineKeyboardButton("لغيري", callback_data="target:other", style="primary"),
    )
    return markup


def build_hide_name_keyboard() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("هل تريد إخفاء اسمك؟", callback_data="noop"))
    markup.row(
        types.InlineKeyboardButton("✅ نعم", callback_data="hide:yes", style="success"),
        types.InlineKeyboardButton("❌ لا", callback_data="hide:no", style="danger"),
    )
    return markup


def build_comment_keyboard() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("إرسال بدون تعليق", callback_data="nocomment", style="primary")
    )
    return markup


def build_confirm_keyboard() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("✅ تأكيد", callback_data="confirm", style="primary"),
        types.InlineKeyboardButton("↩️ تراجع", callback_data="cancel", style="danger"),
    )
    return markup


def build_start_keyboard() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("🎁 خذ هدية", callback_data="take_gift", style="primary"))
    markup.row(types.InlineKeyboardButton("🎟 القسائم", callback_data="coupons:menu", style="primary"))
    return markup


# ---------------------------------------------------------------------------
# لوحات "القسائم" (لوحة المطوّر)
# ---------------------------------------------------------------------------
def build_coupons_menu_keyboard() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("➕ أضف قسيمة", callback_data="coupons:add", style="primary"))
    markup.row(types.InlineKeyboardButton("🎁 إضافة هدية", callback_data="coupons:add_gift", style="primary"))
    markup.row(types.InlineKeyboardButton("➖ إزالة قسيمة", callback_data="coupons:remove", style="danger"))
    markup.row(types.InlineKeyboardButton("⚙️ الإعدادات", callback_data="coupons:settings", style="primary"))
    markup.row(types.InlineKeyboardButton("⬅️ رجوع", callback_data="coupons:back", style="danger"))
    return markup


def build_remove_coupon_keyboard() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=1)
    for c in coupon_system.list_coupons():
        markup.row(
            types.InlineKeyboardButton(
                f"🗑 {c['code']}", callback_data=f"coupons:rm:{c['code']}", style="danger"
            )
        )
    markup.row(types.InlineKeyboardButton("⬅️ رجوع", callback_data="coupons:remove_back", style="danger"))
    return markup


def build_coupon_settings_keyboard() -> types.InlineKeyboardMarkup:
    settings = coupon_system.get_settings()
    gift = find_gift(settings["gift_id"]) if settings.get("gift_id") else None
    gift_txt = f"{gift['prix']} ⭐" if gift else "غير محدَّدة ⚠️"
    hide_txt = "نعم" if settings.get("hide_name") else "لا"
    comment_txt = settings.get("comment_text") or "بدون تعليق"

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.row(types.InlineKeyboardButton(f"🎁 الهدية: {gift_txt}", callback_data="coupons:settings:gift", style="primary"))
    markup.row(types.InlineKeyboardButton(f"🙈 إخفاء الاسم: {hide_txt}", callback_data="coupons:settings:hide", style="primary"))
    markup.row(types.InlineKeyboardButton(f"💬 التعليق: {comment_txt}", callback_data="coupons:settings:comment", style="primary"))
    markup.row(types.InlineKeyboardButton("⬅️ رجوع", callback_data="coupons:menu", style="danger"))
    return markup


def build_gift_pick_keyboard(callback_prefix: str, back_callback: str) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    row = []
    for item in GIFTS:
        emoji = item.get("emoji") or item.get("icon") or "🎁"
        row.append(
            types.InlineKeyboardButton(
                f"{emoji} {item['prix']} ⭐",
                callback_data=f"{callback_prefix}:{item['id']}",
                style="primary",
            )
        )
        if len(row) == 2:
            markup.row(*row)
            row = []
    if row:
        markup.row(*row)
    markup.row(types.InlineKeyboardButton("⬅️ رجوع", callback_data=back_callback, style="danger"))
    return markup


def format_coupons_list() -> str:
    coupons = coupon_system.list_coupons()
    if not coupons:
        return "🎟 القسائم\n\nلا توجد قسائم حالياً."

    now = time.time()
    lines = ["🎟 القسائم المتوفرة:\n"]
    for c in coupons:
        gift = find_gift(c["gift_id"])
        gift_txt = f"{gift['prix']} ⭐" if gift else "—"
        remaining_uses = max(0, c["max_uses"] - len(c["used_by"]))
        remaining_minutes = max(0.0, c["ttl_minutes"] - (now - c["created_at"]) / 60)
        status = "✅ فعّالة" if coupon_system.is_active(c) else "⛔️ منتهية"

        lines.append(
            f"• `{c['code']}` — {status}\n"
            f"  الهدية: {gift_txt}\n"
            f"  الاستخدامات المتبقية: {remaining_uses}/{c['max_uses']}\n"
            f"  الوقت المتبقي: {int(remaining_minutes)} دقيقة\n"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# استخلاص التنسيقات من رسالة المستخدم
# ---------------------------------------------------------------------------
def extract_entities(message) -> list:
    """يحوّل message.entities (كائنات telebot) إلى قوائم dict بسيطة تفهمها
    backend_system.build_text_with_entities لاحقاً."""
    result = []
    for e in (message.entities or []):
        item = {"type": e.type, "offset": e.offset, "length": e.length}
        if e.type == "text_link":
            item["url"] = e.url
        if e.type == "pre":
            item["language"] = getattr(e, "language", "")
        if e.type == "custom_emoji":
            item["custom_emoji_id"] = e.custom_emoji_id
        result.append(item)
    return result


def format_summary(session: dict) -> str:
    gift = session["gift"]
    hide_txt = "نعم" if session["hide_name"] else "لا"
    comment_txt = session.get("comment_text") or "بدون تعليق"
    target_txt = "لنفسي" if session["target_mode"] == "self" else f"لغيري ({session.get('target_display')})"
    return (
        "🎁 مراجعة الطلب\n\n"
        f"الهدية: {gift['prix']} ⭐\n"
        f"المستلم: {target_txt}\n"
        f"إخفاء الاسم: {hide_txt}\n"
        f"التعليق: {comment_txt}\n\n"
        "هل تريد المتابعة؟"
    )


def _error_message(code) -> str:
    mapping = {
        GiftErrorCode.USER_NOT_FOUND: "لم يتم العثور على المستخدم المحدَّد.",
        GiftErrorCode.USER_DELETED: "الحساب محذوف.",
        GiftErrorCode.GIFT_NOT_FOUND: "هذه الهدية لم تعد متوفرة.",
        GiftErrorCode.INSUFFICIENT_BALANCE: "رصيد النجوم في الحساب المُرسِل غير كافٍ.",
        GiftErrorCode.UNKNOWN: "حدث خطأ غير متوقع، حاول لاحقاً.",
    }
    return mapping.get(code, "حدث خطأ غير متوقع.")


# ---------------------------------------------------------------------------
# قيود الوصول — البوت مقيّد بالكامل على OWNER_ID فقط
# ---------------------------------------------------------------------------
UNAUTHORIZED_TEXT = "🚫 لست مرخصاً لاستخدام هذا البوت."


def owner_only_message(func):
    async def wrapped(message, *args, **kwargs):
        if message.from_user is None or message.from_user.id != OWNER_ID:
            await _bot_ref.reply_to(message, UNAUTHORIZED_TEXT)
            return
        return await func(message, *args, **kwargs)
    return wrapped


def owner_only_callback(func):
    async def wrapped(call, *args, **kwargs):
        if call.from_user is None or call.from_user.id != OWNER_ID:
            await _bot_ref.answer_callback_query(call.id, UNAUTHORIZED_TEXT, show_alert=True)
            return
        return await func(call, *args, **kwargs)
    return wrapped


_bot_ref: Optional[AsyncTeleBot] = None  # مرجع للبوت تستخدمه الديكوريتورات أعلاه


# ---------------------------------------------------------------------------
# التسجيل الرئيسي — يُستدعى من main.py
# ---------------------------------------------------------------------------
def setup(bot: AsyncTeleBot, telethon_client, gifts_path: str, owner_id: int, coupons_path: str) -> None:
    global GIFTS_PATH, OWNER_ID, TG_CLIENT, _bot_ref
    GIFTS_PATH = _resolve_gifts_path(gifts_path)
    OWNER_ID = owner_id
    TG_CLIENT = telethon_client
    _bot_ref = bot
    reload_gifts()
    coupon_system.init(coupons_path)

    # --- /start -------------------------------------------------------
    @bot.message_handler(commands=["start"])
    @owner_only_message
    async def cmd_start(message):
        clear_session(message.from_user.id)
        await bot.send_message(
            message.chat.id,
            "أهلاً بك! اضغط الزر أدناه للحصول على هدية.",
            reply_markup=build_start_keyboard(),
        )

    # --- /rgift (owner only) -------------------------------------------
    @bot.message_handler(commands=["rgift"])
    @owner_only_message
    async def cmd_rgift(message):
        count = reload_gifts()
        await bot.send_message(message.chat.id, f"تم تحديث كتالوج الهدايا ({count} هدية).")

    # --- بدء التدفق ------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "take_gift")
    @owner_only_callback
    async def cb_take_gift(call):
        user_id = call.from_user.id
        SESSIONS[user_id] = {"stage": STAGE_CHOOSING_GIFT, "message_id": call.message.message_id}
        await bot.edit_message_text(
            "اختر الهدية:",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_gifts_keyboard(),
        )
        await bot.answer_callback_query(call.id)

    # --- زر عنوان "هل تريد إخفاء اسمك؟" (بدون تأثير) ---------------------
    @bot.callback_query_handler(func=lambda call: call.data == "noop")
    @owner_only_callback
    async def cb_noop(call):
        await bot.answer_callback_query(call.id)

    # --- اختيار الهدية -----------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data.startswith("g:"))
    @owner_only_callback
    async def cb_choose_gift(call):
        user_id = call.from_user.id
        session = SESSIONS.get(user_id)
        if not session or session["stage"] != STAGE_CHOOSING_GIFT:
            await bot.answer_callback_query(call.id)
            return

        local_id = int(call.data.split(":", 1)[1])
        gift = find_gift(local_id)
        if not gift:
            await bot.answer_callback_query(call.id, "هذه الهدية لم تعد متاحة.")
            return

        session["gift"] = gift
        session["stage"] = STAGE_CHOOSING_TARGET
        await bot.edit_message_text(
            "لمن هذه الهدية؟",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_target_keyboard(),
        )
        await bot.answer_callback_query(call.id)

    # --- اختيار المستلم: لنفسي / لغيري -------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data in ("target:self", "target:other"))
    @owner_only_callback
    async def cb_choose_target(call):
        user_id = call.from_user.id
        session = SESSIONS.get(user_id)
        if not session or session["stage"] != STAGE_CHOOSING_TARGET:
            await bot.answer_callback_query(call.id)
            return

        if call.data == "target:self":
            session["target_mode"] = "self"
            session["target_value"] = user_id
            session["target_display"] = "نفسك"
            session["stage"] = STAGE_CHOOSING_HIDE
            await bot.edit_message_text(
                "هل تريد إخفاء اسمك عن المستلم؟",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=build_hide_name_keyboard(),
            )
        else:
            session["target_mode"] = "other"
            session["stage"] = STAGE_WAITING_TARGET
            await bot.edit_message_text(
                "أرسل الآن آيدي المستلم (id) أو معرّفه (username):",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
            )
        await bot.answer_callback_query(call.id)

    # --- استقبال id/username المستلم (فقط عند "لغيري") ----------------------
    @bot.message_handler(
        func=lambda message: SESSIONS.get(message.from_user.id, {}).get("stage") == STAGE_WAITING_TARGET,
        content_types=["text"],
    )
    @owner_only_message
    async def handle_target_input(message):
        user_id = message.from_user.id
        session = SESSIONS[user_id]
        old_message_id = session.get("message_id")

        try:
            await bot.delete_message(message.chat.id, old_message_id)
        except Exception:
            pass
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass

        raw_value = message.text.strip()
        resolved = await backend_system.resolve_user(TG_CLIENT, raw_value)

        if resolved is None:
            # مستخدم غير موجود (أو أي سبب آخر في resolve_user) -> إيقاف العملية بالكامل
            clear_session(user_id)
            await bot.send_message(
                message.chat.id,
                "❌ لم يتم العثور على هذا المستخدم. تم إلغاء العملية.",
                reply_markup=build_start_keyboard(),
            )
            return

        session["target_value"] = resolved.id
        session["target_display"] = raw_value
        session["stage"] = STAGE_CHOOSING_HIDE

        sent = await bot.send_message(
            message.chat.id,
            "هل تريد إخفاء اسمك عن المستلم؟",
            reply_markup=build_hide_name_keyboard(),
        )
        session["message_id"] = sent.message_id

    # --- اختيار إخفاء الاسم ---------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data in ("hide:yes", "hide:no"))
    @owner_only_callback
    async def cb_hide_name(call):
        user_id = call.from_user.id
        session = SESSIONS.get(user_id)
        if not session or session["stage"] != STAGE_CHOOSING_HIDE:
            await bot.answer_callback_query(call.id)
            return

        session["hide_name"] = call.data == "hide:yes"
        session["stage"] = STAGE_WAITING_COMMENT
        await bot.edit_message_text(
            "أرسل تعليقاً الآن ليُرفق مع الهدية، أو اضغط الزر أدناه للإرسال بدون تعليق.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_comment_keyboard(),
        )
        await bot.answer_callback_query(call.id)

    # --- إرسال بدون تعليق ------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "nocomment")
    @owner_only_callback
    async def cb_no_comment(call):
        await _proceed_to_confirm(
            call.from_user.id, call.message.chat.id, call.message.message_id,
            comment_text=None, comment_entities=None,
        )
        await bot.answer_callback_query(call.id)

    # --- استقبال تعليق نصي -------------------------------------------------
    @bot.message_handler(
        func=lambda message: SESSIONS.get(message.from_user.id, {}).get("stage") == STAGE_WAITING_COMMENT,
        content_types=["text"],
    )
    @owner_only_message
    async def handle_comment(message):
        user_id = message.from_user.id
        session = SESSIONS[user_id]
        old_message_id = session.get("message_id")

        try:
            await bot.delete_message(message.chat.id, old_message_id)
        except Exception:
            pass
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass

        entities = extract_entities(message)
        await _proceed_to_confirm(
            user_id, message.chat.id, None,
            comment_text=message.text, comment_entities=entities,
        )

    async def _proceed_to_confirm(user_id, chat_id, message_id, comment_text, comment_entities):
        session = SESSIONS.get(user_id)
        if not session:
            return
        session["comment_text"] = comment_text
        session["comment_entities"] = comment_entities
        session["stage"] = STAGE_CONFIRMING

        if message_id is not None:
            try:
                await bot.delete_message(chat_id, message_id)
            except Exception:
                pass

        sent = await bot.send_message(chat_id, format_summary(session), reply_markup=build_confirm_keyboard())
        session["message_id"] = sent.message_id

    # --- تراجع -------------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "cancel")
    @owner_only_callback
    async def cb_cancel(call):
        user_id = call.from_user.id
        try:
            await bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        clear_session(user_id)

        await bot.send_message(
            call.message.chat.id,
            "تم إلغاء العملية. اضغط الزر أدناه للبدء من جديد.",
            reply_markup=build_start_keyboard(),
        )
        await bot.answer_callback_query(call.id)

    # --- تأكيد وتنفيذ العملية الفعلية ---------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "confirm")
    @owner_only_callback
    async def cb_confirm(call):
        user_id = call.from_user.id
        session = SESSIONS.get(user_id)
        if not session or session["stage"] != STAGE_CONFIRMING:
            await bot.answer_callback_query(call.id)
            return

        await bot.answer_callback_query(call.id, "جارٍ التنفيذ...")

        recipient = await backend_system.resolve_user(TG_CLIENT, session["target_value"])
        if recipient is None:
            await bot.edit_message_text(
                "❌ فشلت العملية.\nالسبب: " + _error_message(GiftErrorCode.USER_NOT_FOUND),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
            )
            clear_session(user_id)
            return

        text_with_entities = None
        if session.get("comment_text"):
            text_with_entities = backend_system.build_text_with_entities(
                session["comment_text"], session.get("comment_entities") or []
            )

        result = await backend_system.send_gift(
            TG_CLIENT,
            recipient,
            int(session["gift"]["gift_id"]),
            hide_name=session["hide_name"],
            text_with_entities=text_with_entities,
        )

        if result.success:
            final_text = "🎉 تم إرسال الهدية بنجاح!"
        else:
            final_text = "❌ فشلت العملية.\nالسبب: " + _error_message(result.error_code)

        await bot.edit_message_text(
            final_text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
        )
        clear_session(user_id)

    # =====================================================================
    # القسائم (لوحة المطوّر)
    # =====================================================================

    # --- فتح لوحة القسائم -------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "coupons:menu")
    @owner_only_callback
    async def cb_coupons_menu(call):
        clear_session(call.from_user.id)
        await bot.edit_message_text(
            format_coupons_list(),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_coupons_menu_keyboard(),
            parse_mode="Markdown",
        )
        await bot.answer_callback_query(call.id)

    # --- الرجوع من لوحة القسائم إلى الشاشة الرئيسية ------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "coupons:back")
    @owner_only_callback
    async def cb_coupons_back(call):
        clear_session(call.from_user.id)
        await bot.edit_message_text(
            "أهلاً بك! اضغط الزر أدناه للحصول على هدية.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_start_keyboard(),
        )
        await bot.answer_callback_query(call.id)

    # --- إضافة قسيمة: طلب عدد الاستخدامات ثم مدة الصلاحية -------------------
    @bot.callback_query_handler(func=lambda call: call.data == "coupons:add")
    @owner_only_callback
    async def cb_coupons_add(call):
        settings = coupon_system.get_settings()
        if not settings.get("gift_id"):
            await bot.answer_callback_query(
                call.id, "⚠️ حدّد الهدية من الإعدادات أولاً.", show_alert=True
            )
            return

        user_id = call.from_user.id
        SESSIONS[user_id] = {
            "stage": STAGE_COUPON_WAITING_CODE,
            "message_id": call.message.message_id,
        }
        await bot.edit_message_text(
            "أرسل كود القسيمة الذي تريد اختياره (مثال: العراق أو ABC123):",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
        )
        await bot.answer_callback_query(call.id)

    @bot.message_handler(
        func=lambda message: SESSIONS.get(message.from_user.id, {}).get("stage") == STAGE_COUPON_WAITING_CODE,
        content_types=["text"],
    )
    @owner_only_message
    async def handle_coupon_code(message):
        user_id = message.from_user.id
        session = SESSIONS[user_id]

        code = message.text.strip()
        if not code:
            await bot.reply_to(message, "❌ أدخل كوداً صالحاً للقسيمة.")
            return

        session["code"] = code.upper()
        session["stage"] = STAGE_COUPON_WAITING_MAX_USES

        try:
            await bot.delete_message(message.chat.id, session.get("message_id"))
        except Exception:
            pass
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass

        sent = await bot.send_message(message.chat.id, "أرسل عدد الاستخدامات المسموحة للقسيمة (رقم صحيح، مثال: 20):")
        session["message_id"] = sent.message_id

    @bot.message_handler(
        func=lambda message: SESSIONS.get(message.from_user.id, {}).get("stage") == STAGE_COUPON_WAITING_MAX_USES,
        content_types=["text"],
    )
    @owner_only_message
    async def handle_coupon_max_uses(message):
        user_id = message.from_user.id
        session = SESSIONS[user_id]

        try:
            max_uses = int(message.text.strip())
            if max_uses <= 0:
                raise ValueError
        except ValueError:
            await bot.reply_to(message, "❌ أرسل رقماً صحيحاً أكبر من صفر.")
            return

        session["max_uses"] = max_uses
        session["stage"] = STAGE_COUPON_WAITING_TTL

        try:
            await bot.delete_message(message.chat.id, session.get("message_id"))
        except Exception:
            pass
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass

        sent = await bot.send_message(message.chat.id, "أرسل مدة صلاحية القسيمة بالدقائق (رقم صحيح، مثال: 15):")
        session["message_id"] = sent.message_id

    @bot.message_handler(
        func=lambda message: SESSIONS.get(message.from_user.id, {}).get("stage") == STAGE_COUPON_WAITING_TTL,
        content_types=["text"],
    )
    @owner_only_message
    async def handle_coupon_ttl(message):
        user_id = message.from_user.id
        session = SESSIONS[user_id]

        try:
            ttl_minutes = int(message.text.strip())
            if ttl_minutes <= 0:
                raise ValueError
        except ValueError:
            await bot.reply_to(message, "❌ أرسل رقماً صحيحاً أكبر من صفر.")
            return

        coupon = coupon_system.create_coupon(session["code"], session["max_uses"], ttl_minutes)

        try:
            await bot.delete_message(message.chat.id, session.get("message_id"))
        except Exception:
            pass
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
        clear_session(user_id)

        await bot.send_message(
            message.chat.id,
            f"✅ تم إنشاء القسيمة: `{coupon['code']}`\n\n" + format_coupons_list(),
            reply_markup=build_coupons_menu_keyboard(),
            parse_mode="Markdown",
        )

    # --- إزالة قسيمة --------------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "coupons:add_gift")
    @owner_only_callback
    async def cb_coupons_add_gift(call):
        user_id = call.from_user.id
        SESSIONS[user_id] = {
            "stage": STAGE_ADD_GIFT_WAITING_ID,
            "message_id": call.message.message_id,
        }
        await bot.edit_message_text(
            "أرسل معرف الهدية (gift_id) الذي تريد إضافته:",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
        )
        await bot.answer_callback_query(call.id)

    @bot.message_handler(
        func=lambda message: SESSIONS.get(message.from_user.id, {}).get("stage") == STAGE_ADD_GIFT_WAITING_ID,
        content_types=["text"],
    )
    @owner_only_message
    async def handle_add_gift_id(message):
        user_id = message.from_user.id
        session = SESSIONS[user_id]
        try:
            gift_id = int(message.text.strip())
        except ValueError:
            await bot.reply_to(message, "❌ أدخل معرفاً رقميًا صحيحًا.")
            return

        session["gift_id"] = gift_id
        session["stage"] = STAGE_ADD_GIFT_WAITING_EMOJI

        try:
            await bot.delete_message(message.chat.id, session.get("message_id"))
        except Exception:
            pass
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass

        sent = await bot.send_message(message.chat.id, "أرسل إيموجي الهدية (مثال: 🎁 أو ❤️):")
        session["message_id"] = sent.message_id

    @bot.message_handler(
        func=lambda message: SESSIONS.get(message.from_user.id, {}).get("stage") == STAGE_ADD_GIFT_WAITING_EMOJI,
        content_types=["text"],
    )
    @owner_only_message
    async def handle_add_gift_emoji(message):
        user_id = message.from_user.id
        session = SESSIONS[user_id]
        emoji = message.text.strip()
        if not emoji:
            await bot.reply_to(message, "❌ أدخل إيموجي صحيح.")
            return

        session["emoji"] = emoji
        session["stage"] = STAGE_ADD_GIFT_WAITING_PRICE

        try:
            await bot.delete_message(message.chat.id, session.get("message_id"))
        except Exception:
            pass
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass

        sent = await bot.send_message(message.chat.id, "أرسل سعر الهدية (رقم صحيح):")
        session["message_id"] = sent.message_id

    @bot.message_handler(
        func=lambda message: SESSIONS.get(message.from_user.id, {}).get("stage") == STAGE_ADD_GIFT_WAITING_PRICE,
        content_types=["text"],
    )
    @owner_only_message
    async def handle_add_gift_price(message):
        user_id = message.from_user.id
        session = SESSIONS[user_id]
        try:
            price = int(message.text.strip())
            if price <= 0:
                raise ValueError
        except ValueError:
            await bot.reply_to(message, "❌ أدخل سعرًا صحيحًا أكبر من صفر.")
            return

        gift_id = session["gift_id"]
        emoji = session["emoji"]
        new_gift = {
            "id": gift_id,
            "gift_id": str(gift_id),
            "custum_gift_icon": str(gift_id),
            "prix": price,
            "emoji": emoji,
        }

        try:
            with open(GIFTS_PATH, "r", encoding="utf-8") as f:
                gifts = json.load(f)
        except FileNotFoundError:
            gifts = []

        gifts.append(new_gift)
        with open(GIFTS_PATH, "w", encoding="utf-8") as f:
            json.dump(gifts, f, ensure_ascii=False, indent=2)

        reload_gifts()

        try:
            await bot.delete_message(message.chat.id, session.get("message_id"))
        except Exception:
            pass
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
        clear_session(user_id)

        await bot.send_message(
            message.chat.id,
            f"✅ تم إضافة الهدية بنجاح:\nالمعرف: {gift_id}\nالسعر: {price}\nالإيموجي: {emoji}",
            reply_markup=build_coupons_menu_keyboard(),
        )

    @bot.callback_query_handler(func=lambda call: call.data == "coupons:remove")
    @owner_only_callback
    async def cb_coupons_remove(call):
        if not coupon_system.list_coupons():
            await bot.answer_callback_query(call.id, "لا توجد قسائم لإزالتها.", show_alert=True)
            return
        await bot.edit_message_text(
            "اختر القسيمة التي تريد إزالتها:",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_remove_coupon_keyboard(),
        )
        await bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "coupons:remove_back")
    @owner_only_callback
    async def cb_coupons_remove_back(call):
        await bot.edit_message_text(
            format_coupons_list(),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_coupons_menu_keyboard(),
            parse_mode="Markdown",
        )
        await bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("coupons:rm:"))
    @owner_only_callback
    async def cb_coupons_rm(call):
        code = call.data.split(":", 2)[2]
        coupon_system.remove_coupon(code)
        await bot.edit_message_text(
            format_coupons_list(),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_coupons_menu_keyboard(),
            parse_mode="Markdown",
        )
        await bot.answer_callback_query(call.id, "🗑 تم حذف القسيمة.")

    # --- إعدادات القسائم -----------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "coupons:settings")
    @owner_only_callback
    async def cb_coupons_settings(call):
        clear_session(call.from_user.id)
        await bot.edit_message_text(
            "⚙️ إعدادات القسائم\n\nتُطبَّق هذه الإعدادات على كل قسيمة جديدة تُنشئها من الآن.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_coupon_settings_keyboard(),
        )
        await bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "coupons:settings:gift")
    @owner_only_callback
    async def cb_coupons_settings_gift(call):
        await bot.edit_message_text(
            "اختر الهدية التي تُرسَل تلقائياً عبر القسائم:",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_gift_pick_keyboard("coupons:settings:gift", "coupons:settings"),
        )
        await bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("coupons:settings:gift:"))
    @owner_only_callback
    async def cb_coupons_settings_gift_pick(call):
        local_id = int(call.data.rsplit(":", 1)[1])
        coupon_system.set_setting("gift_id", local_id)
        await bot.edit_message_text(
            "⚙️ إعدادات القسائم\n\nتُطبَّق هذه الإعدادات على كل قسيمة جديدة تُنشئها من الآن.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_coupon_settings_keyboard(),
        )
        await bot.answer_callback_query(call.id, "✅ تم تحديد الهدية.")

    @bot.callback_query_handler(func=lambda call: call.data == "coupons:settings:hide")
    @owner_only_callback
    async def cb_coupons_settings_hide(call):
        settings = coupon_system.get_settings()
        coupon_system.set_setting("hide_name", not settings.get("hide_name", False))
        await bot.edit_message_text(
            "⚙️ إعدادات القسائم\n\nتُطبَّق هذه الإعدادات على كل قسيمة جديدة تُنشئها من الآن.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_coupon_settings_keyboard(),
        )
        await bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "coupons:settings:comment")
    @owner_only_callback
    async def cb_coupons_settings_comment(call):
        user_id = call.from_user.id
        SESSIONS[user_id] = {
            "stage": STAGE_COUPON_SETTINGS_WAITING_COMMENT,
            "message_id": call.message.message_id,
        }
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton(
                "🚫 بدون تعليق", callback_data="coupons:settings:comment:none", style="danger"
            )
        )
        await bot.edit_message_text(
            "أرسل نص التعليق الذي يُرفق مع كل هدية تُرسَل عبر القسائم، أو اضغط الزر أدناه لإلغائه:",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup,
        )
        await bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "coupons:settings:comment:none")
    @owner_only_callback
    async def cb_coupons_settings_comment_none(call):
        clear_session(call.from_user.id)
        coupon_system.set_setting("comment_text", None)
        await bot.edit_message_text(
            "⚙️ إعدادات القسائم\n\nتُطبَّق هذه الإعدادات على كل قسيمة جديدة تُنشئها من الآن.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_coupon_settings_keyboard(),
        )
        await bot.answer_callback_query(call.id, "🚫 تم إلغاء التعليق.")

    @bot.message_handler(
        func=lambda message: SESSIONS.get(message.from_user.id, {}).get("stage")
        == STAGE_COUPON_SETTINGS_WAITING_COMMENT,
        content_types=["text"],
    )
    @owner_only_message
    async def handle_coupon_settings_comment(message):
        user_id = message.from_user.id
        session = SESSIONS[user_id]
        comment_text = getattr(message, "text", None) or ""
        if not comment_text:
            comment_text = getattr(message, "caption", None) or ""

        coupon_system.set_setting("comment_text", comment_text)

        try:
            await bot.delete_message(message.chat.id, session.get("message_id"))
        except Exception:
            pass
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
        clear_session(user_id)

        await bot.send_message(
            message.chat.id,
            "⚙️ إعدادات القسائم\n\nتُطبَّق هذه الإعدادات على كل قسيمة جديدة تُنشئها من الآن.",
            reply_markup=build_coupon_settings_keyboard(),
        )

    # =====================================================================
    # مستمع الرسائل الخاصة على حساب اليوزربوت نفسه — هنا يُطبَّق استخدام
    # القسيمة: أي شخص يرسل كود قسيمة صالح في محادثة خاصة إلى الحساب
    # المضيف يستلم الهدية المرتبطة بها تلقائياً (استخدام واحد لكل شخص).
    # =====================================================================
    _me_cache = {"id": None}

    @telethon_client.on(events.NewMessage(incoming=True))
    async def on_private_message(event):
        if not event.is_private:
            return

        if _me_cache["id"] is None:
            me = await telethon_client.get_me()
            _me_cache["id"] = me.id
        if event.sender_id == _me_cache["id"]:
            return

        text = (event.raw_text or "").strip()
        if not text:
            return

        if "\n" in text or " " in text:
            return

        coupon = coupon_system.get_coupon(text)
        if coupon is None:
            # ليست قسيمة معروفة -> تجاهل الرسالة بصمت دون أي رد
            return

        sender_id = event.sender_id

        if not coupon_system.is_active(coupon):
            await event.reply("⛔️ هذه القسيمة منتهية الصلاحية أو استُهلكت بالكامل.")
            return
        if sender_id in coupon["used_by"]:
            await event.reply("⚠️ لقد استخدمت هذه القسيمة من قبل.")
            return

        gift = find_gift(coupon["gift_id"])
        if not gift:
            await event.reply("❌ حدثت مشكلة، الهدية المرتبطة بالقسيمة لم تعد متوفرة.")
            return

        recipient = await backend_system.resolve_user(telethon_client, sender_id)
        if recipient is None:
            await event.reply("❌ تعذّر إتمام العملية.")
            return

        redeemed = coupon_system.redeem(text, sender_id)
        if redeemed is None:
            # حالة نادرة: انتهت القسيمة بين لحظة التحقق ولحظة الاستهلاك
            await event.reply("⛔️ هذه القسيمة لم تعد صالحة.")
            return

        text_with_entities = None
        if coupon.get("comment_text"):
            text_with_entities = backend_system.build_text_with_entities(coupon["comment_text"], [])

        result = await backend_system.send_gift(
            telethon_client,
            recipient,
            int(gift["gift_id"]),
            hide_name=coupon.get("hide_name", False),
            text_with_entities=text_with_entities,
        )

        if result.success:
            await event.reply("🎉 تم استلام الهدية عبر القسيمة بنجاح!")
        else:
            await event.reply("❌ فشل إرسال الهدية: " + _error_message(result.error_code))