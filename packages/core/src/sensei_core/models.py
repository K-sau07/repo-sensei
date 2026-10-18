"""Import point for every ORM model so Alembic autogenerate sees the full schema.

Models are added here as the build steps introduce them.
"""

from __future__ import annotations

from sensei_core.db.base import Base

__all__ = ["Base"]
