"""
front_system.py
================
الواجهة الأمامية: بوت تيليجرام عادي (AsyncTeleBot) مقيّد بالكامل على
OWNER_IDS فقط — أي رسالة أو زر من حساب غير موجود بالقائمة يُرفض برسالة "غير مرخّص".

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
import re

import aiohttp
from typing import Optional

from telebot import types
from telebot.async_telebot import AsyncTeleBot
from telethon import events

import backend_system
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

STAGE_ADD_GIFT_WAITING_DATA = "add_gift_waiting_data"

GIFTS: list = []          # كتالوج الهدايا المحمّل في الذاكرة
GIFTS_PATH: str = ""      # مسار ملف gift.json داخل الفوليوم
OWNER_IDS: set[int] = set()
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


def remove_gift_by_id(gift_id: int) -> bool:
    resolved_path = _resolve_gifts_path(GIFTS_PATH)
    try:
        with open(resolved_path, "r", encoding="utf-8") as f:
            gifts = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        gifts = []

    original_len = len(gifts)
    gifts = [
        g
        for g in gifts
        if not (
            g.get("id") == gift_id
            or g.get("gift_id") == gift_id
            or g.get("gift_id") == str(gift_id)
        )
    ]

    if len(gifts) == original_len:
        return False

    with open(resolved_path, "w", encoding="utf-8") as f:
        json.dump(gifts, f, ensure_ascii=False, indent=2)

    reload_gifts()
    return True


def _save_gifts(gifts: list) -> None:
    resolved_path = _resolve_gifts_path(GIFTS_PATH)
    with open(resolved_path, "w", encoding="utf-8") as f:
        json.dump(gifts, f, ensure_ascii=False, indent=2)
    reload_gifts()


def _next_local_gift_id(gifts: list) -> int:
    """معرف داخلي صغير للأزرار، منفصل عن gift_id الحقيقي في Telegram."""
    ids = []
    for gift in gifts:
        try:
            ids.append(int(gift.get("id")))
        except (TypeError, ValueError):
            continue
    return max(ids, default=0) + 1


def _slice_utf16(text: str, offset: int, length: int) -> str:
    """Bot API يحسب entity offsets بوحدات UTF-16، لذلك لا نستخدم slicing العادي."""
    raw = text.encode("utf-16-le")
    start = max(0, int(offset)) * 2
    end = start + max(0, int(length)) * 2
    try:
        return raw[start:end].decode("utf-16-le").strip()
    except UnicodeDecodeError:
        return ""


def _utf16_len(text: str) -> int:
    return len((text or "").encode("utf-16-le")) // 2


def _message_text_and_entities(message):
    """يدعم الرسائل النصية والكابتشن، ويعيد النص مع الـentities المقابلة له."""
    text = getattr(message, "text", None)
    if text is not None:
        return text, (getattr(message, "entities", None) or [])

    caption = getattr(message, "caption", None)
    if caption is not None:
        return caption, (getattr(message, "caption_entities", None) or [])

    return "", []


def _custom_emoji_entities(message) -> list[dict]:
    """يجمع كل Custom Emoji مع موقعه الحقيقي داخل النص بوحدات UTF-16."""
    text, entities = _message_text_and_entities(message)
    found = []
    for entity in entities:
        if getattr(entity, "type", None) != "custom_emoji":
            continue
        custom_id = getattr(entity, "custom_emoji_id", None)
        if not custom_id:
            continue
        offset = int(getattr(entity, "offset", 0) or 0)
        length = int(getattr(entity, "length", 0) or 0)
        found.append({
            "custom_emoji_id": str(custom_id),
            "emoji": _slice_utf16(text, offset, length) or "🎁",
            "start": offset,
            "end": offset + length,
        })
    return found


def _line_records(text: str) -> list[tuple[str, int, int]]:
    """يرجع الأسطر غير الفارغة مع بداية/نهاية كل سطر بوحدات UTF-16."""
    records = []
    cursor = 0
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        line_len = _utf16_len(line)
        if line.strip():
            records.append((line, cursor, cursor + line_len))
        cursor += _utf16_len(raw_line)

    # splitlines() يرجع [] للنص بدون سطر جديد في بعض الحالات الخاصة
    if not records and text.strip():
        records.append((text, 0, _utf16_len(text)))
    return records


def _pick_gift_custom_emoji(
    line_text: str,
    line_start: int,
    line_end: int,
    custom_entities: list[dict],
) -> tuple[Optional[str], Optional[str]]:
    """
    يختار Custom Emoji الخاص بالهدية داخل هذا السطر فقط.

    إذا توجد عدة Custom Emoji نتجاهل الباقي ونفضّل الإيموجي الأقرب قبل السعر،
    ثم الأقرب قبل gift_id، ثم أول Custom Emoji في السطر كـfallback.
    """
    candidates = [
        item for item in custom_entities
        if item["start"] < line_end and item["end"] > line_start
    ]
    if not candidates:
        return None, None

    candidates.sort(key=lambda item: item["start"])

    # أفضل مرساة هي بداية رقم السعر نفسه، وليس رمز النجمة؛ لأن النجمة قد تكون
    # Custom Emoji أيضاً. نبحث عن أول رقم قصير، ثم نأخذ أقرب Custom Emoji قبله.
    price_number_anchor = None
    for match in re.finditer(r"\d+", line_text):
        try:
            value = int(match.group(0))
        except ValueError:
            continue
        if value < 10**9:
            price_number_anchor = match
            break

    id_anchor = re.search(r"\d{10,}", line_text)

    boundary = None
    if price_number_anchor:
        boundary = line_start + _utf16_len(line_text[:price_number_anchor.start()])
    elif id_anchor:
        boundary = line_start + _utf16_len(line_text[:id_anchor.start()])

    if boundary is not None:
        before = [item for item in candidates if item["start"] < boundary]
        if before:
            # الأقرب مباشرة للسعر/المعرف هو غالباً إيموجي الهدية، أما الزخارف السابقة فتُتجاهل.
            chosen = max(before, key=lambda item: item["start"])
            return chosen["custom_emoji_id"], chosen["emoji"]

    chosen = candidates[0]
    return chosen["custom_emoji_id"], chosen["emoji"]


def _parse_gift_record(
    line_text: str,
    line_start: int,
    line_end: int,
    custom_entities: list[dict],
) -> tuple[Optional[dict], Optional[str]]:
    """يحلل سطر هدية واحد ويأخذ فقط Custom Emoji المرتبط بهذا السطر."""
    text = line_text.strip()
    if not text:
        return None, None

    # تجاهل الأسطر التي لا تبدو كسطر هدية أصلاً، مهم عند تحويل رسائل تحتوي شرحاً إضافياً.
    if not re.search(r"\d{10,}", text):
        return None, None

    custom_emoji_id, emoji_text = _pick_gift_custom_emoji(
        line_text,
        line_start,
        line_end,
        custom_entities,
    )
    if not custom_emoji_id:
        return None, "❌ ما لقيت Premium / Custom Emoji خاص بالهدية في هذا السطر."

    exact_price_match = re.search(r"(\d+)\s*⭐(?:️)?", text)
    exact_id_match = re.search(r"(?:—|–|-)\s*`?\s*(\d{10,})\s*`?\s*$", text)

    id_match = re.search(
        r"(?:gift[_\s-]*id|id|الآيدي|الايدي|المعرف)\s*[:=\-]?\s*(\d{10,})",
        text,
        flags=re.IGNORECASE,
    )
    price_match = re.search(
        r"(?:price|prix|السعر)\s*[:=\-]?\s*(\d+)",
        text,
        flags=re.IGNORECASE,
    )

    gift_id = int(exact_id_match.group(1)) if exact_id_match else (int(id_match.group(1)) if id_match else None)
    price = int(exact_price_match.group(1)) if exact_price_match else (int(price_match.group(1)) if price_match else None)

    numbers = [int(value) for value in re.findall(r"\d+", text)]
    if gift_id is None:
        gift_id = next((value for value in numbers if value >= 10**9), None)

    if price is None:
        remaining = [value for value in numbers if value != gift_id]
        if remaining:
            price = remaining[-1]

    if gift_id is None:
        return None, "❌ ما قدرت أحدد gift_id في هذا السطر."
    if price is None or price <= 0:
        return None, "❌ ما قدرت أحدد السعر في هذا السطر."

    return {
        "gift_id": str(gift_id),
        "custom_emoji_id": custom_emoji_id,
        "emoji": emoji_text or "🎁",
        "price": price,
    }, None


def parse_gift_inputs(message) -> tuple[list[dict], list[str]]:
    """
    يقبل هدية واحدة أو عدة هدايا في الرسالة نفسها.

    كل سطر يحتوي gift_id طويل يُعامل كهدية مستقلة، وأي Custom Emoji إضافي
    خارج السطر أو أبعد عن السعر يتم تجاهله تلقائياً.
    """
    text, _ = _message_text_and_entities(message)
    text = text or ""
    if not text.strip():
        return [], ["❌ أرسل البيانات كنص يحتوي الآيدي والإيموجي البريميوم والسعر."]

    custom_entities = _custom_emoji_entities(message)
    gifts = []
    errors = []

    for line_text, line_start, line_end in _line_records(text):
        gift_data, error = _parse_gift_record(
            line_text,
            line_start,
            line_end,
            custom_entities,
        )
        if gift_data:
            gifts.append(gift_data)
        elif error:
            errors.append(error)

    # توافق مع الرسائل القديمة ذات السطر الواحد أو النص غير المقسّم بشكل واضح.
    if not gifts and not errors and re.search(r"\d{10,}", text):
        gift_data, error = _parse_gift_record(
            text,
            0,
            _utf16_len(text),
            custom_entities,
        )
        if gift_data:
            gifts.append(gift_data)
        elif error:
            errors.append(error)

    if not gifts and not errors:
        errors.append("❌ ما لقيت سطر هدية صالح. كل هدية لازم تحتوي gift_id طويل.")

    return gifts, errors


def parse_gift_input(message) -> tuple[Optional[dict], Optional[str]]:
    """توافق خلفي: يعيد أول هدية فقط لمن يستدعي الدالة القديمة."""
    gifts, errors = parse_gift_inputs(message)
    if gifts:
        return gifts[0], None
    return None, (errors[0] if errors else "❌ تعذر قراءة الهدية.")

def upsert_gift(gift_data: dict) -> str:
    """يضيف هدية جديدة أو يحدث نفس gift_id إذا كان موجوداً، ويرجع added/updated."""
    resolved_path = _resolve_gifts_path(GIFTS_PATH)
    try:
        with open(resolved_path, "r", encoding="utf-8") as f:
            gifts = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        gifts = []

    existing = next(
        (gift for gift in gifts if str(gift.get("gift_id")) == str(gift_data["gift_id"])),
        None,
    )

    if existing is None:
        gifts.append({
            "id": _next_local_gift_id(gifts),
            "gift_id": str(gift_data["gift_id"]),
            "custum_gift_icon": str(gift_data["custom_emoji_id"]),
            "emoji": gift_data.get("emoji") or "🎁",
            "prix": int(gift_data["price"]),
        })
        action = "added"
    else:
        existing["gift_id"] = str(gift_data["gift_id"])
        existing["custum_gift_icon"] = str(gift_data["custom_emoji_id"])
        existing.pop("custom_gift_icon", None)
        existing["emoji"] = gift_data.get("emoji") or existing.get("emoji") or "🎁"
        existing["prix"] = int(gift_data["price"])
        action = "updated"

    _save_gifts(gifts)
    return action


def get_auto_gift() -> Optional[dict]:
    """يرجع الهدية المحددة كهدية تلقائية، أو None إذا لم يتم تحديد واحدة."""
    return next((gift for gift in GIFTS if gift.get("auto") is True), None)


def set_auto_gift(local_id: int) -> bool:
    """يحدد هدية واحدة فقط كهدية تلقائية ويحفظ الاختيار داخل gifts.json."""
    resolved_path = _resolve_gifts_path(GIFTS_PATH)
    try:
        with open(resolved_path, "r", encoding="utf-8") as f:
            gifts = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return False

    found = False
    for gift in gifts:
        is_target = gift.get("id") == local_id
        if is_target:
            found = True
            gift["auto"] = True
        else:
            gift.pop("auto", None)

    if not found:
        return False

    _save_gifts(gifts)
    return True


def _normalize_emoji(value: str) -> str:
    """يطبع الإيموجي بصيغة ثابتة للمقارنة (يتجاهل variation selectors والمسافات)."""
    return (value or "").replace("\ufe0f", "").replace("\ufe0e", "").strip()


def find_gift_by_emoji(value: str) -> Optional[dict]:
    wanted = _normalize_emoji(value)
    if not wanted:
        return None
    return next(
        (gift for gift in GIFTS if _normalize_emoji(str(gift.get("emoji") or "")) == wanted),
        None,
    )


def find_gift_by_custom_emoji_id(document_id: int) -> Optional[dict]:
    wanted = str(document_id)
    return next(
        (
            gift
            for gift in GIFTS
            if str(gift.get("custum_gift_icon") or "") == wanted
            or str(gift.get("custom_gift_icon") or "") == wanted
        ),
        None,
    )


# ---------------------------------------------------------------------------
# بناء لوحات الأزرار
# ---------------------------------------------------------------------------
def build_gifts_keyboard() -> types.InlineKeyboardMarkup:
    """صفان لكل صف، والباقي يتوزع تلقائياً على صفوف جديدة."""
    markup = types.InlineKeyboardMarkup(row_width=2)
    row = []
    for item in GIFTS:
        has_custom_icon = bool(item.get("custum_gift_icon"))
        normal_emoji = item.get("emoji") or ""
        button_text = f"{item['prix']}" if has_custom_icon else f"{normal_emoji} {item['prix']}".strip()
        btn_kwargs = dict(
            text=button_text,
            callback_data=f"g:{item['id']}",
            style="primary",
        )
        if has_custom_icon:
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
    markup.row(types.InlineKeyboardButton("⚙️ إدارة الهدايا", callback_data="gifts:menu", style="primary"))
    return markup


def build_gift_management_keyboard() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("🎁 إضافة/تحديث هدية", callback_data="gifts:add", style="primary"),
        types.InlineKeyboardButton("⭐ هدية تلقائية", callback_data="gifts:auto", style="primary"),
    )
    markup.row(types.InlineKeyboardButton("⬅️ رجوع", callback_data="gifts:back", style="danger"))
    return markup


def build_add_gifts_keyboard() -> types.InlineKeyboardMarkup:
    """لوحة جلسة إضافة عدة هدايا؛ الجلسة لا تنتهي إلا بضغط «إنهاء»."""
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("✅ إنهاء", callback_data="gifts:add:finish", style="success")
    )
    return markup


def format_add_gifts_prompt(added: int = 0, updated: int = 0, last_notice: Optional[str] = None) -> str:
    processed = added + updated
    lines = [
        "🎁 أرسل أو حوّل رسائل الهدايا واحدة بعد الأخرى.",
        "",
        "الصيغة:",
        "🧸 50⭐️— `5974210632977745012`",
        "",
        "• 🧸 لازم يكون Premium / Custom Emoji",
        "• 50 هو السعر بالنجوم",
        "• الرقم الأخير هو gift_id",
        "• نفس gift_id يُحدَّث بدل ما يتكرر",
        "• تقدر ترسل عدة هدايا برسالة وحدة: كل هدية بسطر مستقل",
        "• أي Custom Emoji إضافي بالسطر يتم تجاهله؛ البوت يأخذ إيموجي الهدية الأقرب للسعر",
        "",
        f"تمت معالجة: {processed} هدية",
        f"• مضافة: {added}",
        f"• محدثة: {updated}",
    ]
    if last_notice:
        lines.extend(["", last_notice])
    lines.extend(["", "بعد ما تخلص اضغط ✅ إنهاء."])
    return "\n".join(lines)


def _gift_rich_emoji(gift: dict):
    custom_id = gift.get("custum_gift_icon") or gift.get("custom_gift_icon")
    if custom_id:
        return {
            "type": "custom_emoji",
            "custom_emoji_id": str(custom_id),
            "alternative_text": str(gift.get("emoji") or "🎁"),
        }
    return str(gift.get("emoji") or "🎁")


def build_gift_management_rich_message(notice: Optional[str] = None) -> dict:
    """يبني شاشة إدارة الهدايا كـ Rich Message مع زر إزالة داخل كل صف."""
    blocks = []
    if notice:
        blocks.append({"type": "paragraph", "text": notice})

    blocks.append({"type": "heading", "text": "⚙️ إدارة الهدايا", "size": 3})

    auto_gift = get_auto_gift()
    if auto_gift:
        blocks.append({
            "type": "paragraph",
            "text": [
                "الهدية التلقائية: ",
                _gift_rich_emoji(auto_gift),
                f"  {auto_gift.get('prix', 0)}⭐",
            ],
        })
    else:
        blocks.append({"type": "paragraph", "text": "الهدية التلقائية: غير محددة"})

    if GIFTS:
        cells = [[
            {"text": "إزالة", "is_header": True, "align": "center", "valign": "middle"},
            {"text": "الهدية", "is_header": True, "align": "center", "valign": "middle"},
            {"text": "الآيدي", "is_header": True, "align": "center", "valign": "middle"},
        ]]

        for gift in GIFTS:
            local_id = int(gift["id"])
            cells.append([
                {
                    "text": {
                        "type": "button",
                        "button": {
                            "text": "إزالة",
                            "style": "danger",
                            "callback_data": f"gifts:remove:{local_id}",
                        },
                    },
                    "align": "center",
                    "valign": "middle",
                },
                {
                    "text": _gift_rich_emoji(gift),
                    "align": "center",
                    "valign": "middle",
                },
                {
                    "text": {
                        "type": "code",
                        "text": str(gift.get("gift_id") or "—"),
                    },
                    "align": "center",
                    "valign": "middle",
                },
            ])

        blocks.append({
            "type": "table",
            "cells": cells,
            "is_bordered": True,
            "is_striped": True,
            "is_compact": True,
            "caption": f"الهدايا الموجودة: {len(GIFTS)}",
        })
    else:
        blocks.append({"type": "paragraph", "text": "لا توجد هدايا مضافة حالياً."})

    return {"blocks": blocks, "is_rtl": True}


async def _bot_api_json(bot: AsyncTeleBot, method: str, payload: dict):
    """استدعاء Bot API مباشر للميزات الأحدث من نسخة pyTelegramBotAPI المثبتة."""
    url = f"https://api.telegram.org/bot{bot.token}/{method}"
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload) as response:
            data = await response.json(content_type=None)
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data.get('description') or data}")
    return data.get("result")


async def edit_gift_management_message(
    bot: AsyncTeleBot,
    chat_id: int,
    message_id: int,
    notice: Optional[str] = None,
) -> None:
    await _bot_api_json(
        bot,
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "rich_message": build_gift_management_rich_message(notice),
            "reply_markup": build_gift_management_keyboard().to_dict(),
        },
    )


async def send_gift_management_message(
    bot: AsyncTeleBot,
    chat_id: int,
    notice: Optional[str] = None,
) -> None:
    await _bot_api_json(
        bot,
        "sendRichMessage",
        {
            "chat_id": chat_id,
            "rich_message": build_gift_management_rich_message(notice),
            "reply_markup": build_gift_management_keyboard().to_dict(),
        },
    )


def build_auto_gift_keyboard() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    current = get_auto_gift()
    current_id = current.get("id") if current else None
    row = []
    for item in GIFTS:
        has_custom_icon = bool(item.get("custum_gift_icon"))
        emoji = item.get("emoji") or ("" if has_custom_icon else "🎁")
        prefix = "✅ " if item.get("id") == current_id else ""
        btn_kwargs = {
            "text": f"{prefix}{emoji} {item['prix']}⭐".strip(),
            "callback_data": f"gifts:auto:set:{item['id']}",
            "style": "success" if item.get("id") == current_id else "primary",
        }
        if has_custom_icon:
            btn_kwargs["icon_custom_emoji_id"] = item["custum_gift_icon"]
        row.append(types.InlineKeyboardButton(**btn_kwargs))
        if len(row) == 2:
            markup.row(*row)
            row = []
    if row:
        markup.row(*row)
    markup.row(types.InlineKeyboardButton("⬅️ رجوع", callback_data="gifts:auto:back", style="danger"))
    return markup


def get_quick_send_gift() -> Optional[dict]:
    """يرجع الهدية التلقائية المحددة من لوحة إدارة الهدايا."""
    return get_auto_gift()


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
# قيود الوصول — البوت مقيّد بالكامل على OWNER_IDS
# ---------------------------------------------------------------------------
UNAUTHORIZED_TEXT = "🚫 لست مرخصاً لاستخدام هذا البوت."


def owner_only_message(func):
    async def wrapped(message, *args, **kwargs):
        if message.from_user is None or message.from_user.id not in OWNER_IDS:
            await _bot_ref.reply_to(message, UNAUTHORIZED_TEXT)
            return
        return await func(message, *args, **kwargs)
    return wrapped


def owner_only_callback(func):
    async def wrapped(call, *args, **kwargs):
        if call.from_user is None or call.from_user.id not in OWNER_IDS:
            await _bot_ref.answer_callback_query(call.id, UNAUTHORIZED_TEXT, show_alert=True)
            return
        return await func(call, *args, **kwargs)
    return wrapped


_bot_ref: Optional[AsyncTeleBot] = None  # مرجع للبوت تستخدمه الديكوريتورات أعلاه


# ---------------------------------------------------------------------------
# التسجيل الرئيسي — يُستدعى من main.py
# ---------------------------------------------------------------------------
def setup(bot: AsyncTeleBot, telethon_client, gifts_path: str, owner_ids) -> None:
    global GIFTS_PATH, OWNER_IDS, TG_CLIENT, _bot_ref
    GIFTS_PATH = _resolve_gifts_path(gifts_path)
    OWNER_IDS = {int(user_id) for user_id in owner_ids}
    TG_CLIENT = telethon_client
    _bot_ref = bot
    reload_gifts()

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

    # --- هدية @username -------------------------------------------------
    @bot.message_handler(
        func=lambda message: (
            getattr(message.chat, "type", None) == "private"
            and isinstance(getattr(message, "text", None), str)
            and (message.text.strip() == "هدية" or message.text.strip().startswith("هدية "))
        ),
        content_types=["text"],
    )
    @owner_only_message
    async def cmd_gift_to_user(message):
        parts = message.text.strip().split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            await bot.reply_to(message, "الاستخدام: هدية @username")
            return

        raw_target = parts[1].strip()
        resolved = await backend_system.resolve_user(TG_CLIENT, raw_target)
        if resolved is None:
            await bot.reply_to(message, "❌ لم يتم العثور على هذا المستخدم.")
            return

        user_id = message.from_user.id
        clear_session(user_id)
        sent = await bot.send_message(
            message.chat.id,
            f"اختر الهدية التي تريد إرسالها إلى {raw_target}:",
            reply_markup=build_gifts_keyboard(),
        )
        SESSIONS[user_id] = {
            "stage": STAGE_CHOOSING_GIFT,
            "message_id": sent.message_id,
            "target_mode": "other",
            "target_value": resolved.id,
            "target_display": raw_target,
            "target_locked": True,
        }

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
        if session.get("target_locked"):
            session["stage"] = STAGE_CHOOSING_HIDE
            await bot.edit_message_text(
                "هل تريد إخفاء اسمك عن المستلم؟",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=build_hide_name_keyboard(),
            )
        else:
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
    # إدارة كتالوج الهدايا
    # =====================================================================
    @bot.callback_query_handler(func=lambda call: call.data == "gifts:menu")
    @owner_only_callback
    async def cb_gifts_menu(call):
        clear_session(call.from_user.id)
        await edit_gift_management_message(
            bot,
            call.message.chat.id,
            call.message.message_id,
        )
        await bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "gifts:back")
    @owner_only_callback
    async def cb_gifts_back(call):
        clear_session(call.from_user.id)
        await bot.edit_message_text(
            "أهلاً بك! اضغط الزر أدناه للحصول على هدية.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_start_keyboard(),
        )
        await bot.answer_callback_query(call.id)

    # --- تحديد الهدية التلقائية ---------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "gifts:auto")
    @owner_only_callback
    async def cb_gifts_auto(call):
        if not GIFTS:
            await bot.answer_callback_query(call.id, "لا توجد هدايا في الكتالوج.", show_alert=True)
            return
        await bot.edit_message_text(
            "⭐ اختر الهدية التي تريد إرسالها عند كتابة «ارسال» بدون إيموجي:",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_auto_gift_keyboard(),
        )
        await bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("gifts:auto:set:"))
    @owner_only_callback
    async def cb_gifts_auto_set(call):
        try:
            local_id = int(call.data.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await bot.answer_callback_query(call.id, "معرف الهدية غير صالح.", show_alert=True)
            return

        gift = find_gift(local_id)
        if gift is None or not set_auto_gift(local_id):
            await bot.answer_callback_query(call.id, "لم يتم العثور على الهدية.", show_alert=True)
            return

        gift = find_gift(local_id) or gift
        await bot.edit_message_text(
            f"✅ تم تحديد {gift.get('emoji') or '🎁'} ({gift['prix']}⭐) كهدية تلقائية.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_auto_gift_keyboard(),
        )
        await bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "gifts:auto:back")
    @owner_only_callback
    async def cb_gifts_auto_back(call):
        await edit_gift_management_message(
            bot,
            call.message.chat.id,
            call.message.message_id,
        )
        await bot.answer_callback_query(call.id)

    # --- إضافة/إزالة هدية ---------------------------------------------------
    @bot.callback_query_handler(func=lambda call: call.data == "gifts:add")
    @owner_only_callback
    async def cb_gifts_add(call):
        user_id = call.from_user.id
        SESSIONS[user_id] = {
            "stage": STAGE_ADD_GIFT_WAITING_DATA,
            "message_id": call.message.message_id,
            "chat_id": call.message.chat.id,
            "added_count": 0,
            "updated_count": 0,
        }
        await bot.edit_message_text(
            format_add_gifts_prompt(),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=build_add_gifts_keyboard(),
        )
        await bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data == "gifts:add:finish")
    @owner_only_callback
    async def cb_gifts_add_finish(call):
        user_id = call.from_user.id
        session = SESSIONS.get(user_id)
        if not session or session.get("stage") != STAGE_ADD_GIFT_WAITING_DATA:
            await bot.answer_callback_query(call.id, "جلسة الإضافة منتهية بالفعل.")
            return

        added = int(session.get("added_count", 0))
        updated = int(session.get("updated_count", 0))
        clear_session(user_id)

        await edit_gift_management_message(
            bot,
            call.message.chat.id,
            call.message.message_id,
            notice=f"✅ انتهت الإضافة — {added} مضافة، {updated} محدثة.",
        )
        await bot.answer_callback_query(call.id, "تم إنهاء إضافة الهدايا")

    @bot.callback_query_handler(func=lambda call: call.data.startswith("gifts:remove:"))
    @owner_only_callback
    async def cb_gifts_remove_row(call):
        try:
            local_id = int(call.data.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            await bot.answer_callback_query(call.id, "معرف الهدية غير صالح.", show_alert=True)
            return

        gift = find_gift(local_id)
        if gift is None:
            await bot.answer_callback_query(call.id, "هذه الهدية لم تعد موجودة.", show_alert=True)
            return

        if not remove_gift_by_id(local_id):
            await bot.answer_callback_query(call.id, "تعذر حذف الهدية.", show_alert=True)
            return

        await edit_gift_management_message(
            bot,
            call.message.chat.id,
            call.message.message_id,
            notice="✅ تم حذف الهدية.",
        )
        await bot.answer_callback_query(call.id, "تم حذف الهدية")

    @bot.message_handler(
        func=lambda message: (
            SESSIONS.get(message.from_user.id, {}).get("stage") == STAGE_ADD_GIFT_WAITING_DATA
            and SESSIONS.get(message.from_user.id, {}).get("chat_id") == message.chat.id
        ),
        content_types=["text"],
    )
    @owner_only_message
    async def handle_add_gift_data(message):
        user_id = message.from_user.id
        session = SESSIONS[user_id]
        gift_items, errors = parse_gift_inputs(message)

        if not gift_items:
            error_text = errors[0] if errors else "❌ ما لقيت هدية صالحة في الرسالة."
            try:
                await bot.edit_message_text(
                    format_add_gifts_prompt(
                        int(session.get("added_count", 0)),
                        int(session.get("updated_count", 0)),
                        last_notice=error_text + " جرّب الرسالة التالية.",
                    ),
                    chat_id=message.chat.id,
                    message_id=session["message_id"],
                    reply_markup=build_add_gifts_keyboard(),
                )
            except Exception:
                await bot.reply_to(
                    message,
                    error_text + "\n\nمثال: 🧸 50⭐️— `5974210632977745012`",
                )
            return

        added_now = 0
        updated_now = 0
        for gift_data in gift_items:
            action = upsert_gift(gift_data)
            if action == "added":
                session["added_count"] = int(session.get("added_count", 0)) + 1
                added_now += 1
            else:
                session["updated_count"] = int(session.get("updated_count", 0)) + 1
                updated_now += 1

        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass

        parts = []
        if added_now:
            parts.append(f"مضافة {added_now}")
        if updated_now:
            parts.append(f"محدثة {updated_now}")
        notice = f"✅ تمت معالجة {len(gift_items)} هدية"
        if parts:
            notice += " (" + "، ".join(parts) + ")"
        if errors:
            notice += f" — تم تجاهل {len(errors)} سطر غير صالح"

        await bot.edit_message_text(
            format_add_gifts_prompt(
                int(session.get("added_count", 0)),
                int(session.get("updated_count", 0)),
                last_notice=notice,
            ),
            chat_id=message.chat.id,
            message_id=session["message_id"],
            reply_markup=build_add_gifts_keyboard(),
        )



    # =====================================================================
    # أمر «ارسال» من حساب اليوزربوت داخل أي محادثة خاصة.
    # - «ارسال»             -> الهدية التلقائية المحددة من إدارة الهدايا.
    # - «ارسال 🎁»          -> يطابق الإيموجي النصي المحفوظ مع الهدية.
    # - «ارسال <custom>»    -> يدعم أيضاً Custom Emoji عبر document_id.
    # يحذف رسالة الأمر قبل تنفيذ الإرسال حتى لا تظهر للطرف الآخر.
    # =====================================================================
    _me_cache = {"id": None}

    @telethon_client.on(events.NewMessage(outgoing=True))
    async def on_quick_send_command(event):
        raw_text = (event.raw_text or "").strip()
        if not event.is_private or not (raw_text == "ارسال" or raw_text.startswith("ارسال ")):
            return

        if _me_cache["id"] is None:
            me = await telethon_client.get_me()
            _me_cache["id"] = me.id

        chat = await event.get_chat()
        target_id = getattr(chat, "id", None)
        if not target_id or target_id == _me_cache["id"]:
            return

        requested_emoji = raw_text[len("ارسال"):].strip()
        gift = None

        if requested_emoji:
            # أولاً: إذا المستخدم أرسل Custom Emoji، نطابق document_id مباشرة
            # مع custum_gift_icon الموجود في gifts.json.
            for entity in (getattr(event.message, "entities", None) or []):
                document_id = getattr(entity, "document_id", None)
                if document_id:
                    gift = find_gift_by_custom_emoji_id(document_id)
                    if gift is not None:
                        break

            # ثانياً: الإيموجي النصي العادي المحفوظ في حقل emoji.
            if gift is None:
                gift = find_gift_by_emoji(requested_emoji)
        else:
            gift = get_quick_send_gift()

        try:
            await event.delete()
        except Exception:
            logger.exception("تعذّر حذف رسالة أمر ارسال")

        if gift is None:
            if requested_emoji:
                available = " ".join(
                    str(item.get("emoji"))
                    for item in GIFTS
                    if item.get("emoji")
                ) or "لا توجد إيموجيات نصية مسجلة حالياً"
                await telethon_client.send_message(
                    "me",
                    f"❌ ما لقيت هدية مطابقة للإيموجي: {requested_emoji}\n"
                    f"المتاح: {available}",
                )
            else:
                await telethon_client.send_message(
                    "me",
                    "❌ ما محدد هدية تلقائية. افتح البوت > إدارة الهدايا > هدية تلقائية وحدد واحدة.",
                )
            return

        recipient = await backend_system.resolve_user(telethon_client, target_id)
        if recipient is None:
            await telethon_client.send_message(
                "me",
                f"❌ تعذّر تنفيذ أمر «ارسال»: لم أستطع تحديد مستلم المحادثة ({target_id}).",
            )
            return

        result = await backend_system.send_gift(
            telethon_client,
            recipient,
            int(gift["gift_id"]),
            hide_name=False,
            text_with_entities=None,
        )

        if not result.success:
            await telethon_client.send_message(
                "me",
                "❌ فشل أمر «ارسال» إلى "
                f"{getattr(chat, 'username', None) or target_id}: "
                + _error_message(result.error_code),
            )

