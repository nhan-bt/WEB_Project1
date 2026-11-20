"""SQLite engine, SQLAlchemy sessions, and database initialization."""
import os
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "online_shop.db"
DATABASE_URL = os.getenv(
    "ONLINE_SHOP_DATABASE_URL",
    f"sqlite:///{DEFAULT_DB_PATH.as_posix()}",
)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)


if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


class Base(DeclarativeBase):
    """Base class for SQLAlchemy ORM models."""


SessionLocal = sessionmaker(
    bind=engine,
    class_=Session,
    autoflush=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency for endpoints that need a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _seed_products() -> None:
    """Insert sample products only when the product table is empty."""
    from app.models import Product

    with SessionLocal() as db:
        if db.query(Product).count() == 0:
            db.add_all([
                Product(id=1, name="Ao thun", price=150000, stock=10),
                Product(id=2, name="Quan jean", price=350000, stock=5),
                Product(id=3, name="Giay the thao", price=600000, stock=3),
            ])
            db.commit()


def init_db() -> None:
    """Create tables and seed demo products on first startup."""
    # Import models before create_all so every table is registered in metadata.
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _seed_products()


def reset_database() -> None:
    """Drop and recreate all tables; intended for automated tests only."""
    from app import models  # noqa: F401

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    _seed_products()
