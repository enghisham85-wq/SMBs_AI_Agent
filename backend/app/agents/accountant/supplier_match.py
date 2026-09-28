"""Supplier matching across Arabic and English names (T073, research R7)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import normalize_arabic_name, normalize_digits
from app.models.master import Supplier, SupplierAlias

FUZZY_THRESHOLD = 0.9


@dataclass
class SupplierMatch:
    supplier_id: uuid.UUID | None
    method: str  # vat_number | alias | fuzzy | none
    score: float
    candidates: list[tuple[uuid.UUID, str, float]]


async def match(s: AsyncSession, business_id: uuid.UUID, names: list[str | None], vat_number: str | None) -> SupplierMatch:
    """VAT number first, then an exact normalised alias (Arabic or English), then fuzzy >= 0.9."""
    if vat_number:
        vat = normalize_digits(vat_number).replace(" ", "").replace("-", "")
        sup = (await s.execute(select(Supplier).where(Supplier.business_id == business_id,
                                                      Supplier.vat_number == vat))).scalar_one_or_none()
        if sup is not None:
            return SupplierMatch(sup.id, "vat_number", 1.0, [(sup.id, sup.name_en, 1.0)])
    aliases = (await s.execute(select(SupplierAlias).where(SupplierAlias.business_id == business_id))).scalars().all()
    norm = [normalize_arabic_name(n) for n in names if n]
    for a in aliases:
        if a.normalised_text in norm:
            return SupplierMatch(a.supplier_id, "alias", 1.0, [(a.supplier_id, a.alias_text, 1.0)])
    scored: dict[uuid.UUID, tuple[str, float]] = {}
    for a in aliases:
        best = max((SequenceMatcher(None, a.normalised_text, n).ratio() for n in norm), default=0.0)
        if best > scored.get(a.supplier_id, ("", 0.0))[1]:
            scored[a.supplier_id] = (a.alias_text, best)
    ranked = sorted(((sid, name, sc) for sid, (name, sc) in scored.items()), key=lambda t: -t[2])
    if ranked and ranked[0][2] >= FUZZY_THRESHOLD:
        return SupplierMatch(ranked[0][0], "fuzzy", ranked[0][2], ranked[:3])
    return SupplierMatch(None, "none", ranked[0][2] if ranked else 0.0, ranked[:3])


def add_alias(s: AsyncSession, business_id: uuid.UUID, supplier_id: uuid.UUID, text: str) -> None:
    """Remember a new spelling after the owner confirms which supplier it is."""
    lang = "ar" if any("؀" <= ch <= "ۿ" for ch in text) else "en"
    s.add(SupplierAlias(business_id=business_id, supplier_id=supplier_id, alias_text=text,
                        normalised_text=normalize_arabic_name(text), language=lang))
