"""Import every model module so Base.metadata is complete (Alembic, create_all)."""

from app.db.types import Base
from app.models import (  # noqa: F401
    books,
    clock,
    events,
    finance_master,
    harness,
    master,
    purchasing,
    stock_ops,
    tenancy,
)

metadata = Base.metadata
