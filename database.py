from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Laptobumuzdaki güncel konfigürasyonla tam uyumlu bağlantı adresi
SQLALCHEMY_DATABASE_URL = "postgresql://onpar_user:onpar_password@veritabani:5432/onpar_db"

engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()