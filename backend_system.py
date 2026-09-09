"""
backend_system.py
==================
الواجهة الخلفية: تتعامل مباشرة مع حساب المستخدم (Userbot) عبر Telethon.

مسؤوليتها فقط:
    1. التحقق من وجود المستلم (resolve_recipient / resolve_user)
    2. تحويل تنسيقات النص/الإيموجي المميز من صيغة Bot API إلى صيغة MTProto
       (build_text_with_entities)
    3. تنفيذ عملية الشراء والإرسال الفعلية من رصيد نجوم الحساب المضيف
       (send_gift)

لا تحتوي هذه الوحدة على أي منطق خاص بواجهة البوت (front_system.py) —
فقط دوال غير متزامنة (async) تُستدعى من هناك، وترجع نتائج موحّدة
عبر GiftResult / GiftErrorCode بدل رسائل خطأ خام.
"""

import asyncio
import logging
import time
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
    Channel,
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

GiftRecipient = Union[User, Channel]


class GiftErrorCode(str, Enum):
    """أكواد فشل موحّدة تُرسَل للواجهة الأمامية لترجمتها لرسالة عربية مناسبة."""

    USER_NOT_FOUND = "USER_NOT_FOUND"
    USER_DELETED = "USER_DELETED"
    GIFT_NOT_FOUND = "GIFT_NOT_FOUND"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    PAYMENT_BUSY = "PAYMENT_BUSY"
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


_BALANCE_CACHE_SECONDS = 3.0
_BALANCE_STALE_SECONDS = 300.0
_balance_cache: Optional[tuple[float, StarsBalance]] = None
_balance_lock: Optional[asyncio.Lock] = None
_payment_lock: Optional[asyncio.Lock] = None

# واجهة البوت تحفظ target_value كرقم id فقط. أرقام القنوات المجرّدة لا تحمل
# نوع الـpeer، لذلك نحتفظ بالقناة التي تم حلّها من @username إلى حين التأكيد.
# هذا مهم حتى ينجح المسار الحالي من دون تغيير صيغة الجلسات الموجودة.
_CHANNEL_RECIPIENT_CACHE_MAX = 256
_channel_recipient_cache: dict[int, Channel] = {}


# أجزاء من نصوص أخطاء MTProto الخام التي تدل على نفاد رصيد النجوم
_INSUFFICIENT_BALANCE_HINTS = ("BALANCE_TOO_LOW", "STARS_BALANCE", "NOT_ENOUGH")

# أجزاء من نصوص أخطاء MTProto الخام التي تدل على أن الهدية غير صالحة
_GIFT_NOT_FOUND_HINTS = ("STARGIFT_INVALID", "GIFT_ID_INVALID", "STARGIFT_USAGE_LIMITED")


def _get_balance_lock() -> asyncio.Lock:
    global _balance_lock
    if _balance_lock is None:
        _balance_lock = asyncio.Lock()
    return _balance_lock


def _get_payment_lock() -> asyncio.Lock:
    global _payment_lock
    if _payment_lock is None:
        _payment_lock = asyncio.Lock()
    return _payment_lock


def invalidate_stars_balance_cache() -> None:
    global _balance_cache
    _balance_cache = None


def _cache_channel_recipient(channel: Channel) -> None:
    """يحفظ channel مؤقتاً بالـid الخام لاستعادته عند مرحلة التأكيد."""
    channel_id = int(channel.id)
    _channel_recipient_cache[channel_id] = channel
    _channel_recipient_cache.move_to_end(channel_id) if hasattr(_channel_recipient_cache, "move_to_end") else None
    while len(_channel_recipient_cache) > _CHANNEL_RECIPIENT_CACHE_MAX:
        oldest_id = next(iter(_channel_recipient_cache))
        _channel_recipient_cache.pop(oldest_id, None)


async def get_stars_balance(client: TelegramClient) -> Optional[StarsBalance]:
    """يجلب الرصيد مع دمج الطلبات المتزامنة وكاش قصير لتخفيف الضغط."""
    global _balance_cache
    now = time.monotonic()
    if _balance_cache and now - _balance_cache[0] <= _BALANCE_CACHE_SECONDS:
        return _balance_cache[1]

    async with _get_balance_lock():
        now = time.monotonic()
        if _balance_cache and now - _balance_cache[0] <= _BALANCE_CACHE_SECONDS:
            return _balance_cache[1]

        stale = _balance_cache
        try:
            status = await asyncio.wait_for(
                client(GetStarsStatusRequest(peer=InputPeerSelf())),
                timeout=20,
            )
            raw_balance = status.balance
            balance = StarsBalance(
                amount=int(getattr(raw_balance, "amount", 0)),
                nanos=int(getattr(raw_balance, "nanos", 0)),
            )
            _balance_cache = (time.monotonic(), balance)
            return balance
        except RPCError as exc:
            logger.warning("get_stars_balance: رفض Telegram طلب قراءة الرصيد: %s", exc)
        except TimeoutError:
            logger.warning("get_stars_balance: انتهت مهلة قراءة رصيد النجوم")
        except Exception:
            logger.exception("get_stars_balance: تعذر قراءة رصيد النجوم")

        if stale and time.monotonic() - stale[0] <= _BALANCE_STALE_SECONDS:
            logger.info("get_stars_balance: استخدام آخر رصيد محفوظ مؤقتاً")
            return stale[1]
        return None


async def resolve_recipient(
    client: TelegramClient,
    value: Union[str, int],
) -> Optional[GiftRecipient]:
    """
    يتحقق من مستلم الهدية عبر username أو id.

    المستلم المدعوم هو:
        - User عادي غير محذوف.
        - Channel / Supergroup تدعم Telegram استقبال الهدية لها.

    التحقق النهائي من قابلية القناة لاستقبال Star Gifts يتم عند إنشاء
    InputInvoiceStarGift لدى Telegram، لأن الخاصية تعتمد على إعدادات القناة.
    """
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("@"):
            value = value[1:]
        if value.isdigit():
            value = int(value)

    try:
        entity = await client.get_entity(value)
    except (ValueError, UsernameInvalidError, UsernameNotOccupiedError):
        # front_system يخزن Channel.id المجرّد في الجلسة. عند التأكيد قد لا
        # يستطيع Telethon استنتاج أنه Channel من الرقم وحده، فنستخدم النسخة
        # التي حُلّت سابقاً من @username.
        if isinstance(value, int):
            cached_channel = _channel_recipient_cache.get(value)
            if cached_channel is not None:
                return cached_channel
        return None
    except Exception:
        if isinstance(value, int):
            cached_channel = _channel_recipient_cache.get(value)
            if cached_channel is not None:
                logger.info("resolve_recipient: استخدام قناة محفوظة مؤقتاً للمعرف %s", value)
                return cached_channel
        logger.exception("resolve_recipient: خطأ غير متوقع أثناء التحقق من %s", value)
        return None

    if isinstance(entity, User):
        if getattr(entity, "deleted", False):
            logger.info("resolve_recipient: الحساب %s محذوف", value)
            return None
        return entity

    if isinstance(entity, Channel):
        _cache_channel_recipient(entity)
        return entity

    return None


async def resolve_user(client: TelegramClient, value: Union[str, int]) -> Optional[GiftRecipient]:
    """اسم متوافق مع الكود القديم؛ يدعم الآن المستخدم والقناة كمستلم للهدية."""
    return await resolve_recipient(client, value)


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
    recipient: GiftRecipient,
    gift_id: int,
    hide_name: bool = False,
    text_with_entities: Optional[TextWithEntities] = None,
) -> GiftResult:
    """
    ينفّذ عملية شراء وإرسال هدية فعلية من رصيد نجوم الحساب المضيف (Userbot)
    إلى مستخدم أو قناة.

    الخطوات:
        1. بناء الفاتورة (InputInvoiceStarGift) والتحقق من صلاحيتها عبر
           GetPaymentFormRequest — استدعاء لا يخصم أي نجوم، فقط يتحقق ويبني
           نموذج الدفع.
        2. تنفيذ الدفع الفعلي عبر SendStarsFormRequest — هنا فقط يتم الخصم
           والإرسال الحقيقي.
    """
    payment_lock = _get_payment_lock()
    try:
        await asyncio.wait_for(payment_lock.acquire(), timeout=15)
    except TimeoutError:
        logger.warning("send_gift: رفض الطلب لأن عملية دفع أخرى ما زالت مستمرة")
        return GiftResult(success=False, error_code=GiftErrorCode.PAYMENT_BUSY)

    try:
        result = await _send_gift_unlocked(
            client,
            recipient,
            gift_id,
            hide_name=hide_name,
            text_with_entities=text_with_entities,
        )
        if result.success:
            invalidate_stars_balance_cache()
        return result
    finally:
        payment_lock.release()


async def _send_gift_unlocked(
    client: TelegramClient,
    recipient: GiftRecipient,
    gift_id: int,
    hide_name: bool = False,
    text_with_entities: Optional[TextWithEntities] = None,
) -> GiftResult:
    """ينفذ دفعة واحدة؛ الاستدعاء الخارجي يتولى منع تداخل المدفوعات."""
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
