"""Bilingual message catalog and Arabic text normalisation."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

_DIGITS = str.maketrans(
    {
        **{chr(0x0660 + i): str(i) for i in range(10)},  # Arabic-Indic ٠-٩
        **{chr(0x06F0 + i): str(i) for i in range(10)},  # Eastern Arabic-Indic ۰-۹
        "٫": ".",  # Arabic decimal separator ٫
        "٬": "",  # Arabic thousands separator ٬
        "،": ",",  # Arabic comma ،
    }
)

_TASHKEEL = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭ]")
_ALEF = re.compile("[آأإٱ]")  # آ أ إ ٱ -> ا


def normalize_digits(text: str) -> str:
    """Convert Arabic-Indic digits and separators to Western digits: '٣٦٫٥٠٠' -> '36.500'."""
    return text.translate(_DIGITS)


def normalize_arabic_name(text: str) -> str:
    """Normalise a supplier or item name for matching across spelling variants."""
    t = unicodedata.normalize("NFKC", text or "")
    t = normalize_digits(t)
    t = _TASHKEEL.sub("", t)
    t = t.replace("ـ", "")  # tatweel
    t = _ALEF.sub("ا", t)
    t = t.replace("ى", "ي")  # ى -> ي
    t = t.replace("ة", "ه")  # ة -> ه
    t = t.casefold()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


CATALOG: dict[str, dict[str, str]] = {
    "permission_denied": {
        "en": "You do not have permission to do this.",
        "ar": "ليس لديك صلاحية للقيام بهذا الإجراء.",
    },
    "already_resolved": {
        "en": "Already answered in {channel} by {user}: {option}.",
        "ar": "تمت الإجابة بالفعل عبر {channel} بواسطة {user}: {option}.",
    },
    "currency_locked": {
        "en": "The currency cannot be changed after financial records exist.",
        "ar": "لا يمكن تغيير العملة بعد وجود سجلات مالية.",
    },
    "not_found": {"en": "Not found.", "ar": "غير موجود."},
    "clock_busy": {"en": "The clock is already advancing.", "ar": "الساعة قيد التقديم بالفعل."},
    "link_prompt": {
        "en": "Please link your account: open your profile in the dashboard and send /start <code>.",
        "ar": "يرجى ربط حسابك: افتح ملفك في لوحة التحكم وأرسل ‎/start <الرمز>.",
    },
    "linked": {"en": "Linked to {user}.", "ar": "تم الربط بالمستخدم {user}."},
    "received_reading": {"en": "Received, reading…", "ar": "تم الاستلام، جارٍ القراءة…"},
    "opt_approve": {"en": "Approve", "ar": "موافقة"},
    "opt_edit": {"en": "Edit", "ar": "تعديل"},
    "opt_reject": {"en": "Reject", "ar": "رفض"},
    "opt_continue": {"en": "Continue", "ar": "متابعة"},
    "opt_cancel": {"en": "Cancel", "ar": "إلغاء"},
    "opt_ok": {"en": "OK", "ar": "حسناً"},
    "escalated": {
        "en": "{agent} could not complete “{action}” safely and stopped. Reason: {reason}",
        "ar": "لم يتمكن {agent} من إكمال «{action}» بأمان وتوقف. السبب: {reason}",
    },
    "precheck_hold": {
        "en": "I held “{action}”: {reason}. Continue anyway?",
        "ar": "أوقفت «{action}»: {reason}. هل تريد المتابعة؟",
    },
    "approval_generic": {
        "en": "Approve “{action}”? {summary}",
        "ar": "هل توافق على «{action}»؟ {summary}",
    },
}

AGENT_NAMES = {
    "stock": {"en": "Stock Agent", "ar": "وكيل المخزون"},
    "cashflow": {"en": "Cash-Flow Agent", "ar": "وكيل التدفق النقدي"},
    "accountant": {"en": "Accountant Agent", "ar": "الوكيل المحاسبي"},
    "harness": {"en": "Harness", "ar": "المشرف"},
}


def t(key: str, lang: str = "en", **values: Any) -> str:
    entry = CATALOG.get(key)
    if entry is None:
        return key
    return entry.get(lang, entry["en"]).format(**values)


def both(key: str, **values: Any) -> tuple[str, str]:
    return t(key, "en", **values), t(key, "ar", **values)


def option(key: str, catalog_key: str, effect: str) -> dict[str, str]:
    return {"key": key, "label_en": t(catalog_key, "en"), "label_ar": t(catalog_key, "ar"), "effect": effect}
