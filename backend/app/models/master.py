"""Master data shared by the agents: items, recipes, suppliers."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Boolean, Date, Enum, Float, ForeignKey, Integer, Numeric, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import Base, Money, TenantMixin, money_col

QTY = Numeric(18, 4)


class Item(TenantMixin, Base):
    __tablename__ = "item"

    name_en: Mapped[str] = mapped_column(String(200))
    name_ar: Mapped[str] = mapped_column(String(200), default="")
    unit: Mapped[str] = mapped_column(String(20))
    category: Mapped[str] = mapped_column(String(80), default="")
    is_ingredient: Mapped[bool] = mapped_column(Boolean, default=False)
    is_sold: Mapped[bool] = mapped_column(Boolean, default=False)
    shelf_life_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reorder_point: Mapped[Decimal | None] = mapped_column(QTY, nullable=True)
    safety_stock: Mapped[Decimal] = mapped_column(QTY, default=Decimal(0))
    storage_capacity: Mapped[Decimal | None] = mapped_column(QTY, nullable=True)
    preferred_supplier_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("supplier.id"), nullable=True)
    is_critical: Mapped[bool] = mapped_column(Boolean, default=False)
    unit_cost: Mapped[Money] = money_col("unit_cost")
    # Sold items: selling price per unit, used by the demo feed and margin reports.
    sale_price: Mapped[Money] = money_col("sale_price")
    margin_class: Mapped[str] = mapped_column(Enum("high", "normal", "low", native_enum=False), default="normal")
    forecast_method: Mapped[str] = mapped_column(
        Enum("holt_winters", "same_weekday_avg", native_enum=False), default="holt_winters"
    )


class RecipeLine(TenantMixin, Base):
    __tablename__ = "recipe_line"

    sold_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("item.id"), index=True)
    ingredient_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("item.id"))
    quantity: Mapped[Decimal] = mapped_column(QTY)
    unit: Mapped[str] = mapped_column(String(20))


class Supplier(TenantMixin, Base):
    __tablename__ = "supplier"

    name_en: Mapped[str] = mapped_column(String(200))
    name_ar: Mapped[str] = mapped_column(String(200), default="")
    vat_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    telegram: Mapped[str | None] = mapped_column(String(80), nullable=True)
    stated_lead_time_days: Mapped[int] = mapped_column(Integer, default=1)
    observed_lead_time_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=30)
    # Percent discount for paying within the discount window, if offered.
    early_payment_discount_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    early_payment_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reliability_score: Mapped[float] = mapped_column(Float, default=1.0)
    date_format_hint: Mapped[str | None] = mapped_column(Enum("DMY", "MDY", native_enum=False), nullable=True)


class SupplierAlias(TenantMixin, Base):
    __tablename__ = "supplier_alias"

    supplier_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("supplier.id"), index=True)
    alias_text: Mapped[str] = mapped_column(String(200))
    normalised_text: Mapped[str] = mapped_column(String(200), index=True)
    language: Mapped[str] = mapped_column(Enum("en", "ar", native_enum=False))


class SupplierPrice(TenantMixin, Base):
    __tablename__ = "supplier_price"

    supplier_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("supplier.id"), index=True)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("item.id"), index=True)
    pack_size: Mapped[Decimal] = mapped_column(QTY, default=Decimal(1))
    unit: Mapped[str] = mapped_column(String(20))
    min_order_qty: Mapped[Decimal] = mapped_column(QTY, default=Decimal(1))
    # Price per stock unit (`unit`).
    price: Mapped[Money] = money_col("price")
    valid_from: Mapped[date] = mapped_column(Date)
