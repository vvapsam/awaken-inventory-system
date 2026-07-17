import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Railway provides DATABASE_URL. Locally, fall back to a dev database.
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres@/awaken?host=/tmp&port=5433",
)

# Railway sometimes gives a "postgres://" URL; SQLAlchemy wants "postgresql://".
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
