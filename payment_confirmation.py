"""
payment_confirmation.py
=======================
Owner-only confirmation flow for Telegram Stars invoice links.

The module NEVER submits the Stars payment itself. It only:
1) parses an official Telegram invoice deep link,
2) reads the payment form through MTProto to show trustworthy details,
3) sends a confirmation message whose button opens the same Telegram invoice.
"""

import asyncio
import logging
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from telebot import types
from telethon.errors import RPCError
from telethon.tl.functions.payments import GetPaymentFormRequest
from telethon.tl.types import InputInvoiceSlug

import backend_system

logger = logging.getLogger(__name__)

_ALLOWED_HOSTS = {
    "t.me",
    "www.t.me",
    "telegram.me",
    "www.telegram.me",
    "telegram.dog",
    "www.telegram.dog",
}


def extract_invoice_slug(raw_link: str) -> Optional[str]:
    """Extract a Telegram invoice slug from official t.me/tg:// invoice links."""
    value = (raw_link or "").strip().strip("<>")
    if not value:
        return None

    lowered = value.lower()
    if lowered.startswith(
        (
            "t.me/",
            "www.t.me/",
            "telegram.me/",
            "www.telegram.me/",
            "telegram.dog/",
            "www.telegram.dog/",
        )
    ):
        value = "https://" + value

    try:
        parsed = urlparse(value)
    except ValueError:
        return None

    slug: Optional[str] = None

    if parsed.scheme.lower() in {"http", "https"}:
        if (parsed.hostname or "").lower() not in _ALLOWED_HOSTS:
            return None

        path = unquote(parsed.path or "").strip("/")
        if path.startswith("$"):
            slug = path[1:]
        elif path.lower().startswith("invoice/"):
            slug = path.split("/", 1)[1]

    elif parsed.scheme.lower() == "tg":
        target = (parsed.netloc or parsed.path or "").strip("/").lower()
        if target != "invoice":
            return None
        slug = parse_qs(parsed.query).get("slug", [None])[0]

    if slug is None:
        return None

    slug = unquote(slug).strip()
    if not slug or any(ch.isspace() for ch in slug) or "/" in slug:
        return None
    return slug


def canonical_invoice_url(slug: str) -> str:
    return f"https://t.me/${slug}"


def invoice_total(form) -> Optional[int]:
    invoice = getattr(form, "invoice", None)
    prices = getattr(invoice, "prices", None)
    if not prices:
        return None

    try:
        return sum(int(getattr(item, "amount", 0)) for item in prices)
    except (TypeError, ValueError):
        return None


def build_confirmation_keyboard(slug: str) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(
        types.InlineKeyboardButton(
            "✅ متابعة للدفع",
            url=canonical_invoice_url(slug),
        ),
        types.InlineKeyboardButton(
            "❌ إلغاء",
            callback_data="pay:cancel",
        ),
    )
    return markup


def _format_balance(balance) -> Optional[str]:
    if balance is None:
        return None
    amount = int(getattr(balance, "amount", 0))
    nanos = int(getattr(balance, "nanos", 0))
    if nanos:
        fraction = f"{nanos:09d}".rstrip("0")
        return f"{amount}.{fraction}"
    return str(amount)


async def inspect_invoice(client, slug: str) -> tuple[Optional[object], Optional[str]]:
    """Read-only MTProto request. No Stars are spent here."""
    try:
        form = await asyncio.wait_for(
            client(GetPaymentFormRequest(invoice=InputInvoiceSlug(slug=slug))),
            timeout=20,
        )
        return form, None
    except TimeoutError:
        return None, "❌ انتهت مهلة قراءة الفاتورة من Telegram. حاول مرة ثانية."
    except RPCError as exc:
        text = str(exc).upper()
        if "SLUG_INVALID" in text or "INVOICE_INVALID" in text or "BOT_INVOICE_INVALID" in text:
            return None, "❌ رابط الدفع غير صالح أو لم تعد الفاتورة متاحة."
        logger.warning("/pay inspect failed: %s", exc)
        return None, f"❌ تعذر قراءة الفاتورة: {exc}"
    except Exception:
        logger.exception("/pay inspect failed unexpectedly")
        return None, "❌ حدث خطأ غير متوقع أثناء قراءة الفاتورة."


def setup(bot, telethon_client, owner_ids) -> None:
    owners = {int(user_id) for user_id in owner_ids}

    @bot.message_handler(commands=["pay"])
    async def cmd_pay(message):
        if message.from_user is None or message.from_user.id not in owners:
            await bot.reply_to(message, "🚫 لست مرخصاً لاستخدام هذا البوت.")
            return

        text = getattr(message, "text", "") or ""
        parts = text.strip().split(maxsplit=1)
        if len(parts) != 2:
            await bot.reply_to(
                message,
                "الاستخدام:\n/pay https://t.me/$رابط_الفاتورة",
            )
            return

        slug = extract_invoice_slug(parts[1])
        if not slug:
            await bot.reply_to(
                message,
                "❌ رابط الدفع غير صالح.\nمثال:\n/pay https://t.me/$...",
            )
            return

        status = await bot.reply_to(message, "⏳ جارٍ فحص فاتورة النجوم...")
        form, error = await inspect_invoice(telethon_client, slug)
        if error:
            await bot.edit_message_text(
                error,
                chat_id=status.chat.id,
                message_id=status.message_id,
            )
            return

        invoice = getattr(form, "invoice", None)
        currency = str(getattr(invoice, "currency", "") or "").upper()
        if currency != "XTR":
            await bot.edit_message_text(
                "❌ هذه الفاتورة ليست فاتورة Telegram Stars (XTR).",
                chat_id=status.chat.id,
                message_id=status.message_id,
            )
            return

        title = str(getattr(form, "title", "") or "").strip() or "بدون عنوان"
        description = str(getattr(form, "description", "") or "").strip()
        amount = invoice_total(form)
        balance = await backend_system.get_stars_balance(telethon_client)
        balance_text = _format_balance(balance)

        lines = [
            "💳 تأكيد الدفع",
            "",
            f"الطلب: {title}",
        ]
        if description:
            short_description = description if len(description) <= 250 else description[:247] + "..."
            lines.append(f"الوصف: {short_description}")
        if amount is not None:
            lines.append(f"المبلغ: {amount} ⭐")
        if balance_text is not None:
            lines.append(f"رصيد الحساب: {balance_text} ⭐")
        lines.extend(
            [
                "",
                "اضغط «متابعة للدفع» لفتح فاتورة Telegram وإكمال الدفع.",
            ]
        )

        await bot.edit_message_text(
            "\n".join(lines),
            chat_id=status.chat.id,
            message_id=status.message_id,
            reply_markup=build_confirmation_keyboard(slug),
        )

    @bot.callback_query_handler(func=lambda call: call.data == "pay:cancel")
    async def cb_cancel_pay(call):
        if call.from_user is None or call.from_user.id not in owners:
            await bot.answer_callback_query(call.id, "🚫 غير مرخص.", show_alert=True)
            return

        await bot.answer_callback_query(call.id, "تم إلغاء الطلب.")
        try:
            await bot.edit_message_text(
                "❌ تم إلغاء طلب الدفع.",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
            )
        except Exception:
            logger.debug("Unable to edit cancelled /pay message", exc_info=True)
