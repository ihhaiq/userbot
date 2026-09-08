"""
backend_system.py
==================
الواجهة الخلفية: تتعامل مباشرة مع حساب المستخدم (Userbot) عبر Telethon.

مسؤوليتها فقط:
    1. التحقق من وجود المستلم (resolve_user)
    2. تحويل تنسيقات النص/الإيموجي المميز من صيغة Bot API إلى صيغة MTProto
       (build_text_with_entities)
    3. تنفيذ عملية الشراء والإرسال الفعلية من رصيد نجوم الحساب المضيف
       (send_gift)

لا تحتوي هذه الوحدة على أي منطق خاص بواجهة البوت (front_system.py) —
فقط دوال غير متزامنة (async) تُستدعى من هناك، وترجع نتائج موحّدة
عبر GiftResult / GiftErrorCode بدل رسائل خطأ خام.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union

from telethon import TelegramClient
from telethon.errors import RPCError, UsernameInvalidError, UsernameNotOccupiedError
from telethon.tl.functions.payments import (
    GetPaymentFormRequest,
    GetStarsStatusRequest,
    SendStarsFormRequest,
)
from telethon.tl.types import (
    InputInvoiceStarGift,
    InputPeerSelf,
    TextWithEntities,
    User,
    MessageEntityBold,
    MessageEntityItalic,
    MessageEntityUnderline,
    MessageEntityStrike,
    MessageEntitySpoiler,
    MessageEntityCode,
    MessageEntityPre,
    MessageEntityTextUrl,
    MessageEntityBlockquote,
    MessageEntityCustomEmoji,
)

logger = logging.getLogger(__name__)


class GiftErrorCode(str, Enum):
    """أكواد فشل موحّدة تُرسَل للواجهة الأمامية لترجمتها لرسالة عربية مناسبة."""

    USER_NOT_FOUND = "USER_NOT_FOUND"
    USER_DELETED = "USER_DELETED"
    GIFT_NOT_FOUND = "GIFT_NOT_FOUND"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    UNKNOWN = "UNKNOWN"


@dataclass
class GiftResult:
    success: bool
    error_code: Optional[GiftErrorCode] = None
    raw_error: Optional[str] = None


@dataclass(frozen=True)
class StarsBalance:
    """رصيد نجوم الحساب كما يعيده Telegram، شاملاً الجزء الكسري إن وجد."""

    amount: int
    nanos: int = 0


# أجزاء من نصوص أخطاء MTProto الخام التي تدل على نفاد رصيد النجوم
_INSUFFICIENT_BALANCE_HINTS = ("BALANCE_TOO_LOW", "STARS_BALANCE", "NOT_ENOUGH")

# أجزاء من نصوص أخطاء MTProto الخام التي تدل على أن الهدية غير صالحة
_GIFT_NOT_FOUND_HINTS = ("STARGIFT_INVALID", "GIFT_ID_INVALID", "STARGIFT_USAGE_LIMITED")


async def get_stars_balance(client: TelegramClient) -> Optional[StarsBalance]:
    """يجلب الرصيد المتوفر من حساب اليوزربوت بدون تنفيذ أي عملية دفع."""
    try:
        status = await client(GetStarsStatusRequest(peer=InputPeerSelf()))
        balance = status.balance
        return StarsBalance(
            amount=int(getattr(balance, "amount", 0)),
            nanos=int(getattr(balance, "nanos", 0)),
        )
    except RPCError:
        logger.exception("get_stars_balance: رفض Telegram طلب قراءة الرصيد")
    except Exception:
        logger.exception("get_stars_balance: تعذر قراءة رصيد النجوم")
    return None


async def resolve_user(client: TelegramClient, value: Union[str, int]) -> Optional[User]:
    """
    يتحقق من وجود المستخدم عبر username أو user_id ويُرجع كائن User إن وُجد.

    يُرجع None في أي من الحالات التالية:
        - المستخدم غير موجود أصلاً (username غير مستخدم / id غير صالح)
        - تعذّر الوصول إليه من جهة الحساب المضيف (access_hash غير متوفر)
        - الحساب موجود لكنه محذوف (User.deleted == True)
    """
    try:
        if isinstance(value, str):
            value = value.strip()
            if value.startswith("@"):
                value = value[1:]
            if value.isdigit():
                value = int(value)

        entity = await client.get_entity(value)

        if not isinstance(entity, User):
            return None

        if getattr(entity, "deleted", False):
            logger.info("resolve_user: الحساب %s محذوف", value)
            return None

        return entity

    except (ValueError, UsernameInvalidError, UsernameNotOccupiedError):
        return None
    except Exception:
        logger.exception("resolve_user: خطأ غير متوقع أثناء التحقق من %s", value)
        return None


def build_text_with_entities(text: str, entities: list) -> TextWithEntities:
    """
    يحوّل قائمة entities بصيغة Bot API (كل عنصر dict فيه type, offset, length...)
    إلى كائن TextWithEntities المفهوم من MTProto/Telethon، ليُرفَق كتعليق مع الهدية.

    كل عنصر متوقّع بالمفاتيح:
        type, offset, length, url (لـ text_link), custom_emoji_id (لـ custom_emoji),
        language (لـ pre) — بقية المفاتيح تُتجاهل.
    """
    converted = []
    for e in entities:
        etype = e.get("type")
        offset = e["offset"]
        length = e["length"]

        if etype == "bold":
            converted.append(MessageEntityBold(offset, length))
        elif etype == "italic":
            converted.append(MessageEntityItalic(offset, length))
        elif etype == "underline":
            converted.append(MessageEntityUnderline(offset, length))
        elif etype == "strikethrough":
            converted.append(MessageEntityStrike(offset, length))
        elif etype == "spoiler":
            converted.append(MessageEntitySpoiler(offset, length))
        elif etype == "code":
            converted.append(MessageEntityCode(offset, length))
        elif etype == "pre":
            converted.append(MessageEntityPre(offset, length, language=e.get("language", "")))
        elif etype == "text_link":
            converted.append(MessageEntityTextUrl(offset, length, url=e.get("url", "")))
        elif etype == "blockquote":
            converted.append(MessageEntityBlockquote(offset, length))
        elif etype == "custom_emoji":
            converted.append(
                MessageEntityCustomEmoji(offset, length, document_id=int(e["custom_emoji_id"]))
            )
        # أي نوع غير مدعوم في MTProto (مثل mention عادي) يُتجاهل بأمان دون كسر العملية

    return TextWithEntities(text=text, entities=converted)


async def send_gift(
    client: TelegramClient,
    recipient: User,
    gift_id: int,
    hide_name: bool = False,
    text_with_entities: Optional[TextWithEntities] = None,
) -> GiftResult:
    """
    ينفّذ عملية شراء وإرسال هدية فعلية من رصيد نجوم الحساب المضيف (Userbot)
    إلى recipient.

    الخطوات:
        1. بناء الفاتورة (InputInvoiceStarGift) والتحقق من صلاحيتها عبر
           GetPaymentFormRequest — استدعاء لا يخصم أي نجوم، فقط يتحقق ويبني
           نموذج الدفع.
        2. تنفيذ الدفع الفعلي عبر SendStarsFormRequest — هنا فقط يتم الخصم
           والإرسال الحقيقي.
    """
    try:
        peer = await client.get_input_entity(recipient)
    except Exception:
        logger.exception("send_gift: تعذّر بناء input entity للمستلم")
        return GiftResult(success=False, error_code=GiftErrorCode.USER_NOT_FOUND)

    invoice = InputInvoiceStarGift(
        peer=peer,
        gift_id=gift_id,
        hide_name=hide_name,
        message=text_with_entities,
    )

    try:
        form = await client(GetPaymentFormRequest(invoice=invoice))
    except RPCError as e:
        logger.warning("send_gift: فشل GetPaymentFormRequest: %s", e)
        return GiftResult(success=False, error_code=_classify_error(e), raw_error=str(e))
    except Exception as e:
        logger.exception("send_gift: خطأ غير متوقع في GetPaymentFormRequest")
        return GiftResult(success=False, error_code=GiftErrorCode.UNKNOWN, raw_error=str(e))

    try:
        await client(SendStarsFormRequest(form_id=form.form_id, invoice=invoice))
    except RPCError as e:
        logger.warning("send_gift: فشل SendStarsFormRequest: %s", e)
        return GiftResult(success=False, error_code=_classify_error(e), raw_error=str(e))
    except Exception as e:
        logger.exception("send_gift: خطأ غير متوقع في SendStarsFormRequest")
        return GiftResult(success=False, error_code=GiftErrorCode.UNKNOWN, raw_error=str(e))

    return GiftResult(success=True)


def _classify_error(e: RPCError) -> GiftErrorCode:
    """يحوّل نص خطأ MTProto الخام إلى GiftErrorCode موحّد."""
    msg = str(e).upper()
    if any(hint in msg for hint in _INSUFFICIENT_BALANCE_HINTS):
        return GiftErrorCode.INSUFFICIENT_BALANCE
    if any(hint in msg for hint in _GIFT_NOT_FOUND_HINTS):
        return GiftErrorCode.GIFT_NOT_FOUND
    return GiftErrorCode.UNKNOWN
