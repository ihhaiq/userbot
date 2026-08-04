"""
coupon_system.py
==================
نظام القسائم: يدير إنشاء/حذف/التحقق من "قسائم" يستخدمها أي شخص يرسل رسالة
نصية مباشرة إلى الحساب الشخصي (الخاص) لصاحب حساب اليوزربوت (وليس عبر بوت
الواجهة front_system.py).

كل قسيمة:
    code         - كود عشوائي قصير (8 خانات) يرسله المستخدم في الخاص
    gift_id      - id محلي (من gifts.json) للهدية المرتبطة بالقسيمة
    hide_name    - هل يُخفى اسم الحساب المُرسِل عن مستلم الهدية
    comment_text - نص تعليق يُرفق مع كل هدية تُرسَل عبر هذه القسيمة (أو None)
    max_uses     - أقصى عدد استخدامات إجمالي للقسيمة
    used_by      - قائمة آيديات المستخدمين الذين استخدموها (مرة واحدة لكل مستخدم)
    created_at   - وقت الإنشاء (timestamp بالثواني)
    ttl_minutes  - مدة صلاحية القسيمة بالدقائق منذ الإنشاء

بالإضافة إلى "الإعدادات" العامة (settings) التي تُطبَّق تلقائياً على أي
قسيمة جديدة تُنشأ: الهدية الافتراضية، إخفاء الاسم، ونص التعليق.

كل شيء يُخزَّن بصيغة JSON بسيطة في ملف واحد على القرص/الفوليوم — بدون
أي قاعدة بيانات خارجية، تماشياً مع أسلوب بقية المشروع (gifts.json).
"""

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

COUPONS_PATH: str = ""  # يُضبط عبر init()

# الإعدادات الافتراضية لأي قسيمة جديدة (تُعدَّل عبر لوحة "الإعدادات")
DEFAULT_SETTINGS = {
    "gift_id": None,        # id محلي من gifts.json — يجب تحديده قبل إنشاء أي قسيمة
    "hide_name": False,
    "comment_text": None,
}


def init(path: str) -> None:
    """يُستدعى مرة واحدة عند الإقلاع من main.py. ينشئ ملف القسائم إن لم يوجد."""
    global COUPONS_PATH
    COUPONS_PATH = path
    if not os.path.exists(COUPONS_PATH):
        os.makedirs(os.path.dirname(COUPONS_PATH) or ".", exist_ok=True)
        _save({"settings": dict(DEFAULT_SETTINGS), "coupons": {}})
    else:
        # حماية من ملف قديم/ناقص البنية
        data = _load()
        data.setdefault("settings", dict(DEFAULT_SETTINGS))
        data.setdefault("coupons", {})
        _save(data)


def _load() -> dict:
    with open(COUPONS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(COUPONS_PATH) or ".", exist_ok=True)
    with open(COUPONS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# الإعدادات العامة (تُطبَّق على كل قسيمة جديدة تُنشأ من هذه اللحظة فصاعداً —
# لا تؤثر على القسائم الموجودة مسبقاً)
# ---------------------------------------------------------------------------
def get_settings() -> dict:
    return _load()["settings"]


def set_setting(key: str, value) -> dict:
    data = _load()
    data["settings"][key] = value
    _save(data)
    return data["settings"]


# ---------------------------------------------------------------------------
# إدارة القسائم
# ---------------------------------------------------------------------------
def create_coupon(code: str, max_uses: int, ttl_minutes: int) -> dict:
    """ينشئ قسيمة جديدة باستخدام كود يدوي وبالإعدادات الحالية (settings)."""
    data = _load()
    settings = data["settings"]

    normalized_code = code.strip().upper()
    if not normalized_code:
        raise ValueError("رمز القسيمة不能为空")
    if normalized_code in data["coupons"]:
        raise ValueError("القسيمة موجودة مسبقاً")

    coupon = {
        "code": normalized_code,
        "gift_id": settings.get("gift_id"),
        "hide_name": settings.get("hide_name", False),
        "comment_text": settings.get("comment_text"),
        "max_uses": max_uses,
        "used_by": [],
        "created_at": time.time(),
        "ttl_minutes": ttl_minutes,
    }
    data["coupons"][normalized_code] = coupon
    _save(data)
    return coupon


def remove_coupon(code: str) -> bool:
    data = _load()
    code = code.strip().upper()
    if code in data["coupons"]:
        del data["coupons"][code]
        _save(data)
        return True
    return False


def list_coupons() -> list:
    """يُرجع كل القسائم (فعّالة ومنتهية) مرتّبة بالأحدث أولاً."""
    data = _load()
    coupons = list(data["coupons"].values())
    coupons.sort(key=lambda c: c["created_at"], reverse=True)
    return coupons


def get_coupon(code: str) -> Optional[dict]:
    data = _load()
    return data["coupons"].get(code.strip().upper())


def is_active(coupon: dict) -> bool:
    if len(coupon["used_by"]) >= coupon["max_uses"]:
        return False
    elapsed_minutes = (time.time() - coupon["created_at"]) / 60
    if elapsed_minutes > coupon["ttl_minutes"]:
        return False
    return True


def redeem(code: str, user_id: int) -> Optional[dict]:
    """
    يحاول استهلاك القسيمة لصالح user_id (استخدام واحد فقط لكل مستخدم).

    يُرجع نسخة القسيمة (dict) عند النجاح، أو None عند الفشل — غير موجودة،
    منتهية الصلاحية، مكتملة الاستخدام، أو استخدمها هذا المستخدم من قبل.
    """
    data = _load()
    code = code.strip().upper()
    coupon = data["coupons"].get(code)
    if coupon is None:
        return None
    if not is_active(coupon):
        return None
    if user_id in coupon["used_by"]:
        return None

    coupon["used_by"].append(user_id)
    _save(data)
    return coupon