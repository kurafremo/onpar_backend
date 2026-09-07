from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import List, Optional
from datetime import datetime, timedelta
from pydantic import BaseModel, field_validator, ValidationInfo, ConfigDict
from passlib.context import CryptContext
from jose import JWTError, jwt

import re
import os
import google.generativeai as genai
import models
import random
import time
from database import engine, get_db, SessionLocal
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware

# --- ÇEVRE DEĞİŞKENLERİNİ (GİZLİ KASA) YÜKLE ---
load_dotenv()

# GÜVENLİK DOKUNUŞU: os.environ kullanıyoruz. .env okunamazsa sistem anında çöker, yedeği yoktur!
SECRET_KEY = os.environ["SECRET_KEY"]
ALGORITHM = os.environ.get("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", 720))
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# --- 1. UYGULAMAYI VE GÜVENLİĞİ BAŞLAT ---
app = FastAPI(title="On-Par Yedek Parça Profesyonel API", version="3.0.0")

# CORS GÜVENLİĞİ: Sadece kendi domainlerine izin ver
origins = [
    "https://torosoto.com",
    "https://www.torosoto.com",
    "https://api.torosoto.com"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login") 

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

# --- 2. VERİTABANI TABLOLARINI OLUŞTUR ---
models.Base.metadata.create_all(bind=engine)

# --- BAŞLANGIÇ (SEED) KULLANICISI OLUŞTURMA ---
def create_default_admin():
    db = SessionLocal()
    try:
        admin_user = db.query(models.User).filter(models.User.username == "Patron").first()
        if not admin_user:
            hashed_pin = get_password_hash("1234")
            new_admin = models.User(username="Patron", pin_hash=hashed_pin, role="admin")
            db.add(new_admin)
            db.commit()
            print("Varsayılan Admin (Patron) oluşturuldu. PIN: 1234")
    finally:
        db.close()

def seed_katalog_verileri():
    db = SessionLocal()
    try:
        parca_sayisi = db.query(models.SparePart).count()
        if parca_sayisi == 0:
            ornek_parcalar = [
                models.SparePart(
                    name="Bosch Rot Kolu - Ön Sağ", oem_code="1K0423812J", barcode="8690000000012",
                    brand="Bosch", buy_price=350.0, sell_price=500.0, stock_quantity=45,
                    critical_stock_level=10, compatible_vehicles="Volkswagen Golf,Audi A3"
                ),
                models.SparePart(
                    name="Bosch Rot Başı - Ön Sol", oem_code="1K0423811J", barcode="8690000000029",
                    brand="Bosch", buy_price=400.0, sell_price=600.0, stock_quantity=30,
                    critical_stock_level=5, compatible_vehicles="Volkswagen Golf,Seat Leon"
                )
            ]
            db.add_all(ornek_parcalar)
            print("Örnek Yedek Parça Kataloğu otomatik yüklendi!")

        tedarikci_sayisi = db.query(models.Supplier).count()
        if tedarikci_sayisi == 0:
            ornek_tedarikci = models.Supplier(
                name="Toros Toptan Otomotiv A.Ş.", contact_person="Yusuf Yılmaz", phone="0212 555 44 33",
                address="İkitelli Organize Sanayi Bölgesi", tax_number="1234567890", notes="Ana rot grubu tedarikçimiz."
            )
            db.add(ornek_tedarikci)
            print("Varsayılan Tedarikçi otomatik yüklendi!")
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"Tohumlama esnasında bir hata oluştu: {e}")
    finally:
        db.close()

create_default_admin()
seed_katalog_verileri()

# --- ŞEMALAR ---
class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    username: Optional[str] = None

class SparePartBase(BaseModel):
    name: str
    oem_code: Optional[str] = None
    barcode: Optional[str] = None
    brand: Optional[str] = None
    supplier_id: Optional[str] = None
    compatible_vehicles: Optional[List[str]] = []
    buy_price: float
    sell_price: float
    stock_quantity: int = 0
    critical_stock_level: int = 5

    @field_validator('compatible_vehicles', mode='before')
    @classmethod
    def parse_vehicles(cls, v):
        if isinstance(v, str):
            v = v.strip("{}")  
            return [x.strip().strip('"').strip("'") for x in v.split(',')] if v else []
        return v or []

class SparePartCreate(SparePartBase):
    pass

class SparePartResponse(SparePartBase):
    id: int
    created_at: datetime
    updated_at: Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)

class SaleRecordBase(BaseModel):
    part_id: str
    part_name: Optional[str] = None
    quantity: int = 1
    unit_price: float
    total_price: float
    buyer_name: Optional[str] = None
    company_name: Optional[str] = None
    vehicle_plate: Optional[str] = None
    vehicle_brand_model: Optional[str] = None
    notes: Optional[str] = None

class SaleRecordResponse(SaleRecordBase):
    id: int
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class SupplierBase(BaseModel):
    name: str
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    tax_number: Optional[str] = None
    notes: Optional[str] = None

class SupplierResponse(SupplierBase):
    id: int
    model_config = ConfigDict(from_attributes=True)

class UserCreate(BaseModel):
    username: str
    pin: str
    master_key: Optional[str] = None

    @field_validator('pin')
    @classmethod
    def validate_password_strength(cls, v: str, info: ValidationInfo) -> str:
        pattern = r"^(?=.*[a-zA-Z])(?=.*\d).{6,}$"
        if not re.match(pattern, v):
            raise ValueError('Şifre en az 6 karakter olmalı; hem harf hem de rakam içermelidir.')
        return v

class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    model_config = ConfigDict(from_attributes=True)
    
class PromptRequest(BaseModel):
    prompt: str


# --- KİMLİK DOĞRULAMA BAĞIMLILIĞI ---
def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Kimlik doğrulanamadı, geçersiz yaka kartı!",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        token_data = TokenData(username=username)
    except JWTError:
        raise credentials_exception
    user = db.query(models.User).filter(models.User.username == token_data.username).first()
    if user is None:
        raise credentials_exception
    return user


# --- ROTALAR ---
@app.get("/")
def read_root():
    return {"status": "success", "message": "Torosoto Backend Zırhlı ve Ayakta!"}

@app.post("/login", response_model=Token)
def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.pin_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Hatalı kullanıcı adı veya PIN",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username, "role": user.role}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}

@app.post("/users", response_model=UserResponse, status_code=201)
def create_user(user: UserCreate, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Sadece Patron yeni çalışan ekleyebilir!")
        
    db_user = db.query(models.User).filter(models.User.username == user.username).first()
    if db_user:
        raise HTTPException(status_code=400, detail="Bu kullanıcı adı zaten kullanılıyor")
    
    assigned_role = "admin" if user.master_key == "TOROS2026" else "employee"
    hashed_pin = get_password_hash(user.pin)
    new_user = models.User(username=user.username, pin_hash=hashed_pin, role=assigned_role)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user

@app.get("/users", response_model=List[UserResponse])
def get_users(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    return db.query(models.User).all()

@app.get("/spare_parts", response_model=List[SparePartResponse])
def get_spare_parts(q: Optional[str] = None, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    if q:
        search_query = f"%{q.lower()}%"
        parts = db.query(models.SparePart).filter(
            or_(
                models.SparePart.name.ilike(search_query),
                models.SparePart.oem_code.ilike(search_query),
                models.SparePart.barcode.ilike(search_query)
            )
        ).limit(20).all()
    else:
        parts = db.query(models.SparePart).limit(50).all()
    return parts

@app.post("/parts", response_model=SparePartResponse, status_code=201)
def create_spare_part(part: SparePartCreate, db: Session = Depends(get_db)):
    try:
        if not part.barcode or part.barcode.strip() == "":
            unique_id = int(time.time())
            random_suffix = random.randint(1000, 9999)
            part.barcode = f"ONP-{unique_id}-{random_suffix}"
            
        existing_part = db.query(models.SparePart).filter(models.SparePart.barcode == part.barcode).first()
        if existing_part:
            raise HTTPException(status_code=400, detail="Bu barkod veya ürün sistemde zaten kayıtlı!")

        part_data = part.model_dump()
        
        if isinstance(part_data.get('compatible_vehicles'), list):
            part_data['compatible_vehicles'] = ",".join(part_data['compatible_vehicles'])

        db_part = models.SparePart(**part_data)
        db.add(db_part)
        db.commit()
        db.refresh(db_part)
        return db_part
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
        
@app.post("/sales", response_model=SaleRecordResponse, status_code=201)
def record_sale(sale: SaleRecordBase, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    db_part = db.query(models.SparePart).filter(models.SparePart.id == int(sale.part_id)).first()
    if not db_part or db_part.stock_quantity < sale.quantity:
        raise HTTPException(status_code=400, detail="Parça bulunamadı veya yetersiz stok!")
    db_part.stock_quantity -= sale.quantity
    db_sale = models.SaleRecord(**sale.model_dump())
    db.add(db_sale)
    db.commit()
    db.refresh(db_sale)
    return db_sale

@app.get("/sales/history", response_model=List[SaleRecordResponse])
def get_sales_history(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    return db.query(models.SaleRecord).order_by(models.SaleRecord.created_at.desc()).limit(100).all()

@app.delete("/sales/{sale_id}")
def delete_sale(sale_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Sadece Patron satış iptal edebilir!")
    db_sale = db.query(models.SaleRecord).filter(models.SaleRecord.id == sale_id).first()
    if not db_sale:
        raise HTTPException(status_code=404, detail="Satış kaydı bulunamadı")
    
    db_part = db.query(models.SparePart).filter(models.SparePart.id == int(db_sale.part_id)).first()
    if db_part:
        db_part.stock_quantity += db_sale.quantity
    
    db.delete(db_sale)
    db.commit()
    return {"message": "Satış iptal edildi ve stok iade edildi"}

@app.put("/sales/{sale_id}", response_model=SaleRecordResponse)
def update_sale(sale_id: int, updated_sale: SaleRecordBase, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    db_sale = db.query(models.SaleRecord).filter(models.SaleRecord.id == sale_id).first()
    if not db_sale:
        raise HTTPException(status_code=404, detail="Satış kaydı bulunamadı")
    
    old_part = db.query(models.SparePart).filter(models.SparePart.id == int(db_sale.part_id)).first()
    if old_part:
        old_part.stock_quantity += db_sale.quantity
        
    new_part = db.query(models.SparePart).filter(models.SparePart.id == int(updated_sale.part_id)).first()
    if not new_part or new_part.stock_quantity < updated_sale.quantity:
        if old_part:
            old_part.stock_quantity -= db_sale.quantity
        raise HTTPException(status_code=400, detail="Güncellenen ürün için yeterli stok yok!")
        
    new_part.stock_quantity -= updated_sale.quantity
    
    db_sale.part_id = updated_sale.part_id
    db_sale.part_name = updated_sale.part_name
    db_sale.quantity = updated_sale.quantity
    db_sale.unit_price = updated_sale.unit_price
    db_sale.total_price = updated_sale.total_price
    db_sale.buyer_name = updated_sale.buyer_name
    db_sale.company_name = updated_sale.company_name
    db_sale.vehicle_plate = updated_sale.vehicle_plate
    db_sale.vehicle_brand_model = updated_sale.vehicle_brand_model
    db_sale.notes = updated_sale.notes
    
    db.commit()
    db.refresh(db_sale)
    return db_sale

@app.get("/suppliers", response_model=List[SupplierResponse])
def get_suppliers(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    return db.query(models.Supplier).all()

@app.post("/suppliers", status_code=201)
def create_supplier(supplier: SupplierBase, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    db_supplier = models.Supplier(**supplier.model_dump())
    db.add(db_supplier)
    db.commit()
    return {"message": "Tedarikçi eklendi"}

@app.put("/suppliers/{supplier_id}")
def update_supplier(supplier_id: int, supplier: SupplierBase, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    db_supplier = db.query(models.Supplier).filter(models.Supplier.id == supplier_id).first()
    if not db_supplier:
        raise HTTPException(status_code=404, detail="Tedarikçi bulunamadı")
    for key, value in supplier.model_dump().items():
        setattr(db_supplier, key, value)
    db.commit()
    return {"message": "Tedarikçi güncellendi"}

@app.delete("/suppliers/{supplier_id}")
def delete_supplier(supplier_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    db_supplier = db.query(models.Supplier).filter(models.Supplier.id == supplier_id).first()
    if not db_supplier:
        raise HTTPException(status_code=404, detail="Tedarikçi bulunamadı")
    db.delete(db_supplier)
    db.commit()
    return {"message": "Tedarikçi silindi"}

@app.post("/sales/bulk", status_code=201)
def record_bulk_sale(sales: List[SaleRecordBase], db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    try:
        for sale in sales:
            db_part = db.query(models.SparePart).filter(models.SparePart.id == int(sale.part_id)).first()
            if not db_part or db_part.stock_quantity < sale.quantity:
                raise HTTPException(status_code=400, detail=f"{sale.part_name} için yeterli stok yok!")
            
            db_part.stock_quantity -= sale.quantity
            db_sale = models.SaleRecord(**sale.model_dump())
            db.add(db_sale)
        
        db.commit()
        return {"message": "Toplu satış başarılı"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/generate_report")
def generate_report(req: PromptRequest, current_user: models.User = Depends(get_current_user)):
    try:
        # GÜVENLİK DOKUNUŞU: Gemini şifresi artık gizli kasadan (.env) geliyor!
        if not GEMINI_API_KEY:
            raise HTTPException(status_code=500, detail="Gemini API Anahtarı sunucuda bulunamadı!")
            
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-2.5-flash')
        
        response = model.generate_content(req.prompt)
        return {"report": response.text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Yapay Zeka Hatası: {str(e)}")