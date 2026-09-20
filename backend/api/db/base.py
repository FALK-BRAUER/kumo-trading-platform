"""Declarative base for all app-data ORM models. Alembic autogenerate reads `Base.metadata`."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base — import and subclass for each app-data table."""
