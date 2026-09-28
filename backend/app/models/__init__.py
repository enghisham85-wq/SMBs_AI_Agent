"""Import every model module so Base.metadata is complete (Alembic, create_all)."""

from app.db.types import Base
from app.models import clock, events, finance_master, harness, master, tenancy  # noqa: F401

metadata = Base.metadata
