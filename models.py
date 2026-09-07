from sqlalchemy import Column, Integer, String, Float, DateTime
from sqlalchemy.sql import func
from database import Base

class SparePart(Base):
    __tablename__ = "spare_parts"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True, nullable=False)
    oem_code = Column(String, index=True)
    barcode = Column(String, index=True)
    brand = Column(String, index=True)
    supplier_id = Column(String)
    compatible_vehicles = Column(String)
    buy_price = Column(Float, nullable=False)
    sell_price = Column(Float, nullable=False)
    stock_quantity = Column(Integer, default=0)
    critical_stock_level = Column(Integer, default=5)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class SaleRecord(Base):
    __tablename__ = "sales"

    id = Column(Integer, primary_key=True, index=True)
    part_id = Column(String, nullable=False)
    part_name = Column(String)
    quantity = Column(Integer, default=1)
    unit_price = Column(Float, nullable=False)
    total_price = Column(Float, nullable=False)
    buyer_name = Column(String)
    company_name = Column(String)
    vehicle_plate = Column(String)
    vehicle_brand_model = Column(String)
    notes = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Supplier(Base):
    __tablename__ = "suppliers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    contact_person = Column(String)
    phone = Column(String)
    address = Column(String)
    tax_number = Column(String)
    notes = Column(String)

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    pin_hash = Column(String, nullable=False)
    role = Column(String, default="employee")
    created_at = Column(DateTime(timezone=True), server_default=func.now())