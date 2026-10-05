"""
On-Par Otomotiv ERP & POS - Database Models
Version: 4.0.0 (Enterprise B2B, Soft-Delete & Audit Log Destekli)
"""

from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base
from datetime import datetime, timezone

def get_utc_now():
    return datetime.now(timezone.utc)

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(100), unique=True, index=True, nullable=True)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(20), default="çalışan", nullable=False)  # 'patron', 'çalışan', 'depocu'
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=get_utc_now)

    # İlişkiler
    sales = relationship("SaleRecord", back_populates="user")
    device_tokens = relationship("DeviceToken", back_populates="user", cascade="all, delete-orphan")
    audit_logs = relationship("AuditLog", back_populates="user")


class Supplier(Base):
    __tablename__ = "suppliers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), unique=True, index=True, nullable=False)
    contact_person = Column(String(100), nullable=True)
    phone = Column(String(30), nullable=True)
    email = Column(String(100), nullable=True)
    address = Column(Text, nullable=True)
    tax_number = Column(String(20), nullable=True)  # VKN / TCKN
    tax_office = Column(String(100), nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=get_utc_now)
    updated_at = Column(DateTime(timezone=True), default=get_utc_now, onupdate=get_utc_now)

    # Tedarikçinin parçaları
    parts = relationship("SparePart", back_populates="supplier")


class SparePart(Base):
    __tablename__ = "spare_parts"

    id = Column(Integer, primary_key=True, index=True)
    barcode = Column(String(50), unique=True, index=True, nullable=True)
    name = Column(String(200), index=True, nullable=False)
    brand = Column(String(100), index=True, nullable=True)       # Örn: Bosch, Valeo, Renault
    model = Column(String(100), nullable=True)                   # Araç modeli / Motor kodu
    oem_code = Column(String(100), index=True, nullable=True)    # Orijinal parça numarası
    stock_quantity = Column(Integer, default=0, nullable=False)
    buy_price = Column(Float, default=0.0, nullable=False)
    sell_price = Column(Float, default=0.0, nullable=False)
    location = Column(String(50), nullable=True)                 # Raf / Kutu no (Örn: A-12-3)
    min_stock_alert = Column(Integer, default=5, nullable=False) # Kritik stok eşiği
        
    # Tedarikçi bağlantısı
    supplier_id = Column(Integer, ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True)
    supplier = relationship("Supplier", back_populates="parts")

    # Enterprise Soft Delete Koruması
    is_deleted = Column(Boolean, default=False, index=True)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=get_utc_now)
    updated_at = Column(DateTime(timezone=True), default=get_utc_now, onupdate=get_utc_now)

    # Satış kayıtları bağlantısı
    sales = relationship("SaleRecord", back_populates="spare_part")


class SaleRecord(Base):
    __tablename__ = "sale_records"

    id = Column(Integer, primary_key=True, index=True)
    spare_part_id = Column(Integer, ForeignKey("spare_parts.id", ondelete="SET NULL"), nullable=True)
    part_name = Column(String(200), nullable=False)
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Float, nullable=False)
    total_price = Column(Float, nullable=False)
    sale_date = Column(DateTime(timezone=True), default=get_utc_now, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    payment_method = Column(String(30), default="Nakit")  # Nakit, Kredi Kartı, Havale/EFT, Açık Hesap
    note = Column(Text, nullable=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)

    # Soft Delete
    is_deleted = Column(Boolean, default=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    # İlişkiler
    spare_part = relationship("SparePart", back_populates="sales")
    user = relationship("User", back_populates="sales")
    customer = relationship("Customer", back_populates="sales")


class DeviceToken(Base):
    __tablename__ = "device_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token = Column(String(500), unique=True, index=True, nullable=False)
    platform = Column(String(20), default="android")  # android, ios, web
    created_at = Column(DateTime(timezone=True), default=get_utc_now)
    updated_at = Column(DateTime(timezone=True), default=get_utc_now, onupdate=get_utc_now)

    user = relationship("User", back_populates="device_tokens")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action = Column(String(50), nullable=False, index=True)   # CREATE, UPDATE, DELETE, SALE, DUPLICATE_CLEANUP
    entity_name = Column(String(50), nullable=False)          # spare_parts, sales, suppliers, auth
    entity_id = Column(Integer, nullable=True)
    details = Column(Text, nullable=True)                     # Değişiklik özeti veya JSON verisi
    ip_address = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), default=get_utc_now, index=True)

    user = relationship("User", back_populates="audit_logs")

    # --- MÜŞTERİ (CARİ) VE HESAP HAREKETLERİ TABLOLARI ---

class Customer(Base):
    __tablename__ = "customers"
        
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True, nullable=False)  # Usta veya Müşteri Adı
    company_name = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    tax_number = Column(String, nullable=True)
    tax_office = Column(String, nullable=True)
    address = Column(String, nullable=True)
        
    # Kilit Nokta: Müşterinin bize olan toplam borcu (Veresiye bakiyesi)
    balance = Column(Float, default=0.0) 
        
    notes = Column(String, nullable=True)
    is_deleted = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    # İLİŞKİLER (Sihirli kısım)
    sales = relationship("SaleRecord", back_populates="customer")
    transactions = relationship("CustomerTransaction", back_populates="customer", cascade="all, delete-orphan")

class CustomerTransaction(Base):
    __tablename__ = "customer_transactions"
        
    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"))
        
    # 'BORCLANDIRMA' (Veresiye satış yapıldığında) 
    # 'TAHSILAT' (Usta gelip borcunu ödediğinde)
    # 'IADE' (Ürün iade edilip borçtan düşüldüğünde)
    transaction_type = Column(String, nullable=False)
        
    amount = Column(Float, default=0.0)
    description = Column(String, nullable=True) # Örn: "Nakit tahsilat yapıldı" veya "Fiş No: 1234 Satış"
        
    # Eğer bu hareket bir satıştan geliyorsa satış ID'sini tutalım
    sale_id = Column(Integer, ForeignKey("sale_records.id", ondelete="SET NULL"), nullable=True)
        
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # İLİŞKİLER
    customer = relationship("Customer", back_populates="transactions")
    sale_record = relationship("SaleRecord")