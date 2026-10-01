"""Telegram: a photo captioned `/delivery <PO number>` is attached to that PO's delivery (T063)."""

from __future__ import annotations

from sqlalchemy import select

from app.approvals.telegram_bot import register_upload
from app.core.files import save_upload
from app.db.engine import write_session
from app.models.purchasing import Delivery, PurchaseOrder
from app.models.tenancy import User


async def delivery_photo(user: User, data: bytes, mime: str, name: str, caption: str) -> str:
    parts = caption.split()
    number = parts[1].upper() if len(parts) > 1 else ""
    ref = await save_upload(user.business_id, data, mime, name, user.id)
    async with write_session() as s:
        po = (await s.execute(select(PurchaseOrder).where(PurchaseOrder.business_id == user.business_id,
                                                          PurchaseOrder.number == number))).scalar_one_or_none()
        if po is None:
            return (f"Photo saved, but I could not find order {number or '(none)'}. Use /delivery PO-0001."
                    if user.language == "en" else f"تم حفظ الصورة، لكن لم أجد الأمر {number}. استخدم ‎/delivery PO-0001.")
        latest = (await s.execute(select(Delivery).where(Delivery.po_id == po.id).order_by(Delivery.created_at.desc())
                                  .limit(1))).scalar_one_or_none()
        if latest is not None and latest.photo_file_id is None:
            latest.photo_file_id = ref.id
            return (f"Photo attached to the delivery for {po.number}." if user.language == "en"
                    else f"أُرفقت الصورة بتوريد {po.number}.")
        po.notes = {**po.notes, "pending_photo_file_id": str(ref.id)}
    return (f"Photo saved for {po.number}. Please confirm the delivered quantities in the dashboard."
            if user.language == "en" else f"حُفظت الصورة لـ {po.number}. يرجى تأكيد الكميات المستلمة من لوحة التحكم.")


def register() -> None:
    register_upload("delivery", "staff", delivery_photo)
