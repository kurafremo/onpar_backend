"""
On-Par Otomotiv ERP & POS - Enterprise Backend API
Version: 4.0.0 (Zırhlı, B2B Enterprise, AI Destekli & Push Entegrasyonlu)
"""

from fastapi import FastAPI, Depends, HTTPException, status, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, func, desc
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel, field_validator, ValidationInfo, ConfigDict, Field
from passlib.context import CryptContext
from jose import JWTError, jwt

import re
import os
import json
import time
import logging
import google.generativeai as genai

import models
from database import engine, get_db, SessionLocal
from dotenv import load_dotenv

# --- LOGGING KURULUMU ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("on_par_api")

# --- 1. ÇEVRE DEĞİŞKENLERİ ---
load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "ONPAR_ENTERPRISE_SECRET_KEY_SUPER_SECURE_2026_CHANGE_ME")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 60 * 24))  # 1 Gün
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    logger.info("Google Gemini AI basariyla entegre edildi.")
else:
    logger.warning("DIKKAT: GEMINI_API_KEY bulunamadi. AI ozellikleri devre disi kalabilir.")

# Veritabanı tablolarını oluştur
models.Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="On-Par Otomotiv ERP API",
    version="4.0.0",
    description="Enterprise Seviye Otomotiv Yedek Parça ve Stok Yönetim Sistemi"
)

# --- 2. CORS GÜVENLİK AYARLARI ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Mobil ve yerel ağ erişimi için
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 3. ŞİFRELEME VE JWT ALTYAPISI ---
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def sanitize_search_query(query: Optional[str]) -> Optional[str]:
    """SQL LIKE Injection ve wildcard suistimalini önler."""
    if not query:
        return None
    cleaned = re.sub(r"[%_\\]", "", query).strip()
    return cleaned if cleaned else None

# --- 4. AUDIT LOG (DENETİM İZİ) YARDIMCISI ---
def log_audit_trail(
    db: Session,
    action: str,
    entity_name: str,
    user_id: Optional[int] = None,
    entity_id: Optional[int] = None,
    details: Optional[Any] = None,
    ip_address: Optional[str] = None
):
    try:
        details_str = json.dumps(details, ensure_ascii=False) if isinstance(details, (dict, list)) else str(details)
        log_entry = models.AuditLog(
            user_id=user_id,
            action=action,
            entity_name=entity_name,
            entity_id=entity_id,
            details=details_str,
            ip_address=ip_address
        )
        db.add(log_entry)
        db.commit()
    except Exception as e:
        logger.error(f"Audit log kaydetme hatasi: {e}")
        db.rollback()

# --- 5. RATE LIMITER (BRUTE-FORCE KALKANI) ---
FAILED_LOGIN_ATTEMPTS: Dict[str, Dict[str, Any]] = {}
RATE_LIMIT_LOGIN_MAX = 5
RATE_LIMIT_LOGIN_WINDOW = 300  # 5 dakika

def get_client_ip(request: Request) -> str:
    # Ters vekil sunucusu arkasında güvenli IP alımı
    return request.client.host if request.client else "unknown"

# --- 6. PUSH BİLDİRİM SERVİSİ ---
def dispatch_push_alert(db: Session, title: str, body: str, data: Optional[dict] = None):
    """Kayıtlı tüm yönetici ve cihaz tokenlarına push bildirimi simüle eder/gönderir."""
    tokens = db.query(models.DeviceToken).all()
    if not tokens:
        logger.info(f"Bildirim gonderilecek cihaz tokeni bulunamadi: [{title}] {body}")
        return

    logger.info(f"==> [PUSH NOTIFICATION] {len(tokens)} cihaza gonderiliyor: '{title}' - '{body}'")
    # Gerçek FCM / OneSignal entegrasyonu için HTTP isteği buraya eklenir
    # FCM Örnek: firebase_admin.messaging.send_multicast(...)

# --- 7. PYDANTIC ŞEMALARI ---
class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=4)
    email: Optional[str] = None
    role: str = "çalışan"

class UserResponse(BaseModel):
    id: int
    username: str
    email: Optional[str] = None
    role: str
    is_active: bool
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)

class Token(BaseModel):
    access_token: str
    token_type: str
    user_info: UserResponse

class SparePartCreate(BaseModel):
    barcode: Optional[str] = None
    name: str = Field(..., min_length=2, max_length=200)
    brand: Optional[str] = None
    model: Optional[str] = None
    oem_code: Optional[str] = None
    stock_quantity: int = Field(0, ge=0)
    buy_price: float = Field(0.0, ge=0.0)
    sell_price: float = Field(0.0, ge=0.0)
    location: Optional[str] = None
    min_stock_alert: int = Field(5, ge=0)
    supplier_id: Optional[int] = None

class SparePartResponse(SparePartCreate):
    id: int
    is_deleted: bool = False
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)

class SaleCreate(BaseModel):
    spare_part_id: int
    quantity: int = Field(..., gt=0)
    unit_price: Optional[float] = None  # Sunucu güvenliği için opsiyonel, DB'den alınır
    customer_id: Optional[int] = None
    payment_method: str = "Nakit"
    note: Optional[str] = None

class SaleBulkCreate(BaseModel):
    items: List[SaleCreate]

class SaleResponse(BaseModel):
    id: int
    spare_part_id: Optional[int]
    part_name: str
    quantity: int
    unit_price: float
    total_price: float
    sale_date: datetime
    payment_method: str
    note: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

class SupplierCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=150)
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    tax_number: Optional[str] = None
    tax_office: Optional[str] = None
    note: Optional[str] = None

class SupplierResponse(SupplierCreate):
    id: int
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)

class DeviceTokenCreate(BaseModel):
    token: str = Field(..., min_length=10)
    platform: str = "android"

class SmartBarcodeRequest(BaseModel):
    barcode: str
    prompt_hint: Optional[str] = None

    # --- MÜŞTERİ (CARİ) ŞEMALARI ---
class CustomerBase(BaseModel):
    name: str  # Sadece bu alan zorunlu
    company_name: Optional[str] = None
    phone: Optional[str] = None
    tax_number: Optional[str] = None
    tax_office: Optional[str] = None
    address: Optional[str] = None
    notes: Optional[str] = None

class CustomerCreate(CustomerBase):
    pass

class CustomerResponse(CustomerBase):
    id: int
    balance: float
    is_deleted: bool
    created_at: Optional[datetime] = None

class Config:
    from_attributes = True

class TransactionCreate(BaseModel):
    amount: float
    description: Optional[str] = None
    transaction_type: str = "TAHSILAT" # Varsayılan olarak para alma

# --- 8. AUTH VE RBAC BAĞIMLILIKLARI ---
def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> models.User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Oturum sureniz doldu veya gecersiz kimlik bilgisi.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(models.User).filter(models.User.username == username).first()
    if user is None or not user.is_active:
        raise credentials_exception
    return user

def require_admin(current_user: models.User = Depends(get_current_user)) -> models.User:
    if current_user.role.lower() not in ["patron", "admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bu islemi gerceklestirmek icin Patron yetkisine sahip olmalisiniz."
        )
    return current_user

# --- 9. KULLANICI & AUTH ROTLARI ---
@app.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
@app.post("/register/", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
@app.post("/api/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
@app.post("/api/register/", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(user_in: UserCreate, db: Session = Depends(get_db), req: Request = None):
    # İlk kayıt olan otomatik 'patron' olur
    user_count = db.query(models.User).count()
    assigned_role = "patron" if user_count == 0 else user_in.role

    existing = db.query(models.User).filter(models.User.username == user_in.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="Bu kullanici adi zaten kullaniliyor.")

    new_user = models.User(
        username=user_in.username,
        email=user_in.email,
        hashed_password=get_password_hash(user_in.password),
        role=assigned_role
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    log_audit_trail(db, "CREATE", "users", new_user.id, new_user.id, f"Kullanici kayit oldu: {new_user.username}", get_client_ip(req))
    return new_user

@app.post("/login", response_model=Token)
@app.post("/login/", response_model=Token)
@app.post("/api/login", response_model=Token)
@app.post("/api/login/", response_model=Token)
def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    ip = get_client_ip(request)
    now = time.time()

    # Brute-force denetimi
    if ip in FAILED_LOGIN_ATTEMPTS:
        attempt_data = FAILED_LOGIN_ATTEMPTS[ip]
        if attempt_data["count"] >= RATE_LIMIT_LOGIN_MAX:
            if now - attempt_data["last_attempt"] < RATE_LIMIT_LOGIN_WINDOW:
                remaining = int(RATE_LIMIT_LOGIN_WINDOW - (now - attempt_data["last_attempt"]))
                raise HTTPException(
                    status_code=429,
                    detail=f"Cok fazla hatali deneme yapildi. Lutfen {remaining} saniye bekleyin."
                )
            else:
                FAILED_LOGIN_ATTEMPTS[ip] = {"count": 0, "last_attempt": now}

    user = db.query(models.User).filter(models.User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        if ip not in FAILED_LOGIN_ATTEMPTS:
            FAILED_LOGIN_ATTEMPTS[ip] = {"count": 1, "last_attempt": now}
        else:
            FAILED_LOGIN_ATTEMPTS[ip]["count"] += 1
            FAILED_LOGIN_ATTEMPTS[ip]["last_attempt"] = now
        raise HTTPException(status_code=400, detail="Kullanici adi veya sifre hatali.")

    if not user.is_active:
        raise HTTPException(status_code=400, detail="Hesabiniz askiya alinmis.")

    # Başarılı girişte sayacı sıfırla
    if ip in FAILED_LOGIN_ATTEMPTS:
        del FAILED_LOGIN_ATTEMPTS[ip]

    token = create_access_token(data={"sub": user.username, "role": user.role, "id": user.id})
    return {"access_token": token, "token_type": "bearer", "user_info": user}

@app.get("/users/me", response_model=UserResponse)
@app.get("/users/me/", response_model=UserResponse)
@app.get("/api/users/me", response_model=UserResponse)
@app.get("/api/users/me/", response_model=UserResponse)
def get_me(current_user: models.User = Depends(get_current_user)):
    return current_user

# --- 10. BİLDİRİM TOKEN KAYIT ROTASI (GÖREV 2) ---
@app.post("/api/notifications/register-device", status_code=status.HTTP_200_OK)
@app.post("/api/notifications/register-device/", status_code=status.HTTP_200_OK)
def register_device_token(
    token_in: DeviceTokenCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Mobil cihaz FCM token'ını kullanıcıyla eşleştirir."""
    existing = db.query(models.DeviceToken).filter(models.DeviceToken.token == token_in.token).first()
    if existing:
        existing.user_id = current_user.id
        existing.platform = token_in.platform
        existing.updated_at = datetime.now(timezone.utc)
    else:
        new_token = models.DeviceToken(
            user_id=current_user.id,
            token=token_in.token,
            platform=token_in.platform
        )
        db.add(new_token)
    db.commit()
    return {"status": "success", "message": "Cihaz bildirimi basariyla kaydedildi."}

# --- 11. YEDEK PARÇA YÖNETİMİ ---
@app.get("/spare-parts", response_model=List[SparePartResponse])
@app.get("/spare-parts/", response_model=SparePartResponse)
@app.get("/api/spare-parts", response_model=List[SparePartResponse])
@app.get("/api/spare-parts/", response_model=List[SparePartResponse])
def list_spare_parts(
    skip: int = 0,
    limit: int = 100,
    search: Optional[str] = None,
    brand: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    query = db.query(models.SparePart).filter(models.SparePart.is_deleted == False)

    search_clean = sanitize_search_query(search)
    if search_clean:
        pattern = f"%{search_clean}%"
        query = query.filter(
            or_(
                models.SparePart.name.ilike(pattern),
                models.SparePart.barcode.ilike(pattern),
                models.SparePart.oem_code.ilike(pattern),
                models.SparePart.model.ilike(pattern)
            )
        )

    if brand:
        query = query.filter(models.SparePart.brand.ilike(f"%{brand.strip()}%"))

    return query.order_by(desc(models.SparePart.id)).offset(skip).limit(limit).all()

@app.get("/spare-parts/{barcode}", response_model=SparePartResponse)
@app.get("/spare-parts/{barcode}/", response_model=SparePartResponse)
@app.get("/api/spare-parts/{barcode}", response_model=SparePartResponse)
@app.get("/api/spare-parts/{barcode}/", response_model=SparePartResponse)
def get_spare_part_by_barcode(
    barcode: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    part = db.query(models.SparePart).filter(
        models.SparePart.barcode == barcode.strip(),
        models.SparePart.is_deleted == False
    ).first()
    if not part:
        raise HTTPException(status_code=404, detail="Yedek parca bulunamadi.")
    return part

@app.post("/spare-parts", response_model=SparePartResponse, status_code=status.HTTP_201_CREATED)
@app.post("/spare-parts/", response_model=SparePartResponse, status_code=status.HTTP_201_CREATED)
@app.post("/api/spare-parts", response_model=SparePartResponse, status_code=status.HTTP_201_CREATED)
@app.post("/api/spare-parts/", response_model=SparePartResponse, status_code=status.HTTP_201_CREATED)
def create_spare_part(
    part_in: SparePartCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    req: Request = None
):
    if part_in.barcode:
        existing = db.query(models.SparePart).filter(
            models.SparePart.barcode == part_in.barcode.strip(),
            models.SparePart.is_deleted == False
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="Bu barkoda sahip bir parca zaten mevcut.")

    new_part = models.SparePart(**part_in.model_dump())
    db.add(new_part)
    db.commit()
    db.refresh(new_part)

    log_audit_trail(db, "CREATE", "spare_parts", current_user.id, new_part.id, f"Parca eklendi: {new_part.name}", get_client_ip(req))
    return new_part

@app.put("/spare-parts/{part_id}", response_model=SparePartResponse)
@app.put("/spare-parts/{part_id}/", response_model=SparePartResponse)
@app.put("/api/spare-parts/{part_id}", response_model=SparePartResponse)
@app.put("/api/spare-parts/{part_id}/", response_model=SparePartResponse)
def update_spare_part(
    part_id: int,
    part_in: SparePartCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    req: Request = None
):
    # PROFESYONEL DOKUNUŞ: Eşzamanlılık (Race Condition) Kilidi
    # with_for_update(): Bu parça üzerinde işlem yaparken, başka bir request (istek)
    # aynı parçaya erişmeye çalışırsa, bu işlem bitip commit olana kadar veritabanı onu bekletir.
    db_part = db.query(models.SparePart).filter(
        models.SparePart.id == part_id,
        models.SparePart.is_deleted == False
    ).with_for_update().first()
    
    if not db_part:
        raise HTTPException(status_code=404, detail="Guncellenecek parca bulunamadi.")

    for field, value in part_in.model_dump(exclude_unset=True).items():
        setattr(db_part, field, value)

    db_part.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(db_part)

    log_audit_trail(db, "UPDATE", "spare_parts", current_user.id, db_part.id, f"Parca guncellendi (Atomic): {db_part.name}", get_client_ip(req))
    return db_part

@app.delete("/spare-parts/{part_id}")
@app.delete("/spare-parts/{part_id}/")
@app.delete("/api/spare-parts/{part_id}")
@app.delete("/api/spare-parts/{part_id}/")
def delete_spare_part(
    part_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    req: Request = None
):
    """Soft Delete Güvencesi: Parça veritabanından kalıcı olarak silinmez, pasifleştirilir."""
    db_part = db.query(models.SparePart).filter(
        models.SparePart.id == part_id,
        models.SparePart.is_deleted == False
    ).first()
    if not db_part:
        raise HTTPException(status_code=404, detail="Silinecek parca bulunamadi.")

# PROFESYONEL DOKUNUŞ: Silinen parçanın barkodunu boşa çıkar ki ileride aynı barkod tekrar kullanılabilsin.
    if db_part.barcode:
    db_part.barcode = f"DEL_{int(time.time())}_{db_part.barcode}"

    db_part.is_deleted = True
    db_part.deleted_at = datetime.now(timezone.utc)
    db.commit()

    log_audit_trail(db, "DELETE", "spare_parts", current_user.id, db_part.id, f"Parca soft-delete yapildi: {db_part.name}", get_client_ip(req))
    return {"status": "success", "message": f"{db_part.name} basariyla silindi (arsivlendi)."}

# --- 12. SATIŞ VE KASA YÖNETİMİ (ZIRHLI FİYAT KORUMASI) ---
@app.post("/sales", response_model=SaleResponse, status_code=status.HTTP_201_CREATED)
@app.post("/sales/", response_model=SaleResponse, status_code=status.HTTP_201_CREATED)
@app.post("/api/sales", response_model=SaleResponse, status_code=status.HTTP_201_CREATED)
@app.post("/api/sales/", response_model=SaleResponse, status_code=status.HTTP_201_CREATED)
def record_sale(
    sale_in: SaleCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    req: Request = None
):
    # STOK GÜVENLİK KİLİDİ
    db_part = db.query(models.SparePart).filter(
        models.SparePart.id == sale_in.spare_part_id,
        models.SparePart.is_deleted == False
    ).with_for_update().first()
    
    if not db_part:
        raise HTTPException(status_code=404, detail="Satisi yapilacak parca bulunamadi.")

    if db_part.stock_quantity < sale_in.quantity:
        raise HTTPException(
            status_code=400,
            detail=f"Yetersiz stok! Mevcut stok: {db_part.stock_quantity}, Talep edilen: {sale_in.quantity}"
        )

    # Fiyat veritabanından alınır
    unit_price = db_part.sell_price
    total_price = round(unit_price * sale_in.quantity, 2)

    # Stok düş
    db_part.stock_quantity -= sale_in.quantity
    db_part.updated_at = datetime.now(timezone.utc)

    # Satış fişi kaydı
    sale_record = models.SaleRecord(
        spare_part_id=db_part.id,
        part_name=db_part.name,
        quantity=sale_in.quantity,
        unit_price=unit_price,
        total_price=total_price,
        user_id=current_user.id,
        payment_method=sale_in.payment_method,
        note=sale_in.note
    )
    db.add(sale_record)
    db.commit()
    db.refresh(sale_record)

    log_audit_trail(db, "SALE", "sales", current_user.id, sale_record.id, f"Satis yapildi: {db_part.name} x {sale_in.quantity}", get_client_ip(req))

    # GÖREV 2: Bildirimler
    if db_part.stock_quantity <= 0:
        background_tasks.add_task(dispatch_push_alert, db, "⚠️ STOK TÜKENDİ!", f"'{db_part.name}' stokta bitti!")
    elif db_part.stock_quantity <= db_part.min_stock_alert:
        background_tasks.add_task(dispatch_push_alert, db, "⚠️ Kritik Stok Uyarısı", f"'{db_part.name}' kritik seviyede: {db_part.stock_quantity}")

    return sale_record


@app.post("/sales/bulk", response_model=List[SaleResponse])
@app.post("/sales/bulk/", response_model=List[SaleResponse])
@app.post("/api/sales/bulk", response_model=List[SaleResponse])
@app.post("/api/sales/bulk/", response_model=List[SaleResponse])
def record_bulk_sale(
    bulk_in: SaleBulkCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    req: Request = None
):
    created_sales = []
    total_bulk_amount = 0.0
    veresiye_borc_artisi = 0.0
    ilgili_musteri_id = None

    for item in bulk_in.items:
        # STOK KİLİDİ (Toplu satışta her satır için)
        db_part = db.query(models.SparePart).filter(
            models.SparePart.id == item.spare_part_id,
            models.SparePart.is_deleted == False
        ).with_for_update().first()
        
        if not db_part:
            db.rollback()
            raise HTTPException(status_code=404, detail=f"ID {item.spare_part_id} olan parca bulunamadi.")

        if db_part.stock_quantity < item.quantity:
            db.rollback()
            raise HTTPException(
                status_code=400,
                detail=f"'{db_part.name}' icin yetersiz stok! Mevcut: {db_part.stock_quantity}"
            )

        unit_price = db_part.sell_price
        total_price = round(unit_price * item.quantity, 2)
        total_bulk_amount += total_price

        db_part.stock_quantity -= item.quantity
        db_part.updated_at = datetime.now(timezone.utc)

        cust_id = getattr(item, 'customer_id', None)

        sale_rec = models.SaleRecord(
            spare_part_id=db_part.id,
            part_name=db_part.name,
            quantity=item.quantity,
            unit_price=unit_price,
            total_price=total_price,
            user_id=current_user.id,
            payment_method=item.payment_method,
            note=item.note,
            customer_id=cust_id
        )
        db.add(sale_rec)
        created_sales.append(sale_rec)
        
        if item.payment_method == "Açık Hesap" and cust_id is not None:
            veresiye_borc_artisi += total_price
            ilgili_musteri_id = cust_id

        if db_part.stock_quantity <= db_part.min_stock_alert:
            background_tasks.add_task(dispatch_push_alert, db, "⚠️ Kritik Stok", f"'{db_part.name}' kritik seviyede: {db_part.stock_quantity}")

    # CARİ (VERESİYE) KISMI - Müşteriyi de kilitliyoruz ki aynı anda tahsilat yapılıyorsa bakiye bozulmasın
    if veresiye_borc_artisi > 0 and ilgili_musteri_id:
        cust = db.query(models.Customer).filter(models.Customer.id == ilgili_musteri_id).with_for_update().first()
        if cust:
            cust.balance += veresiye_borc_artisi
            new_trans = models.CustomerTransaction(
                customer_id=cust.id,
                transaction_type="BORCLANDIRMA",
                amount=veresiye_borc_artisi,
                description=f"Toplu Sepet Veresiye Satışı ({len(created_sales)} Kalem Ürün)"
            )
            db.add(new_trans)

    db.commit()
    for s in created_sales:
        db.refresh(s)

    log_audit_trail(db, "SALE", "sales", current_user.id, None, f"Toplu satis yapildi: {len(created_sales)} kalem, Toplam: {total_bulk_amount} TL", get_client_ip(req) if req else "unknown")
    return created_sales

@app.get("/sales/history", response_model=List[SaleResponse])
@app.get("/sales/history/", response_model=List[SaleResponse])
@app.get("/api/sales/history", response_model=List[SaleResponse])
@app.get("/api/sales/history/", response_model=List[SaleResponse])
def get_sale_history(
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    return db.query(models.SaleRecord).filter(
        models.SaleRecord.is_deleted == False
    ).order_by(desc(models.SaleRecord.sale_date)).limit(limit).all()

@app.put("/sales/{sale_id}", response_model=SaleResponse)
@app.put("/sales/{sale_id}/", response_model=SaleResponse)
@app.put("/api/sales/{sale_id}", response_model=SaleResponse)
@app.put("/api/sales/{sale_id}/", response_model=SaleResponse)
def update_sale(
    sale_id: int,
    sale_in: SaleCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    sale = db.query(models.SaleRecord).filter(models.SaleRecord.id == sale_id).first()
    if not sale:
        raise HTTPException(status_code=404, detail="Guncellenecek satis bulunamadi.")
    
    # PROFESYONEL STOK YÖNETİMİ: Eğer satış adedi değiştirildiyse
    if sale.quantity != sale_in.quantity:
        # Eski adet ile yeni adet arasındaki farkı bul
        qty_diff = sale_in.quantity - sale.quantity
        
        part = db.query(models.SparePart).filter(models.SparePart.id == sale.spare_part_id).first()
        if part:
            # Eğer adet artırılıyorsa ve stokta yeterli ürün yoksa işlemi durdur
            if qty_diff > 0 and part.stock_quantity < qty_diff:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Yetersiz stok! Eklemek istediğiniz miktar için stokta yeterli ürün yok. Kalan stok: {part.stock_quantity}"
                )
            
            # Farkı stoktan düş (Eğer qty_diff negatifse eksi eksi artı yapar, stoğa iade eder)
            part.stock_quantity -= qty_diff
            part.updated_at = datetime.now(timezone.utc)
            
        # Satışın kendi miktarını ve yeni fiyata göre toplam tutarını güncelle
        sale.quantity = sale_in.quantity
        sale.total_price = round(sale.unit_price * sale.quantity, 2)

    # Ödeme yöntemi ve notları her halükarda güncelle
    sale.payment_method = sale_in.payment_method
    sale.note = sale_in.note
    
    db.commit()
    db.refresh(sale)
    return sale

@app.delete("/sales/{sale_id}")
@app.delete("/sales/{sale_id}/")
@app.delete("/api/sales/{sale_id}")
@app.delete("/api/sales/{sale_id}/")
def delete_sale(
    sale_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    sale = db.query(models.SaleRecord).filter(models.SaleRecord.id == sale_id).first()
    if not sale:
        raise HTTPException(status_code=404, detail="Silinecek satis bulunamadi.")
    
    # PROFESYONEL DOKUNUŞ: Satış iptal edildiğinde ürünleri stoğa iade et
    if sale.spare_part_id:
        part = db.query(models.SparePart).filter(models.SparePart.id == sale.spare_part_id).first()
        if part:
            part.stock_quantity += sale.quantity
            part.updated_at = datetime.now(timezone.utc)
    
    # Satışı iptal edilmiş (silinmiş) olarak işaretle
    sale.is_deleted = True
    db.commit()
    return {"status": "success", "message": "Satis fişi iptal edildi ve ürünler stoğa geri eklendi."}

# --- 13. TEDARİKÇİ (CARİ) YÖNETİMİ ---
@app.get("/suppliers", response_model=List[SupplierResponse])
@app.get("/suppliers/", response_model=List[SupplierResponse])
@app.get("/api/suppliers", response_model=List[SupplierResponse])
@app.get("/api/suppliers/", response_model=List[SupplierResponse])
def list_suppliers(
    search: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    q = db.query(models.Supplier)
    search_clean = sanitize_search_query(search)
    if search_clean:
        q = q.filter(
            or_(
                models.Supplier.name.ilike(f"%{search_clean}%"),
                models.Supplier.phone.ilike(f"%{search_clean}%"),
                models.Supplier.contact_person.ilike(f"%{search_clean}%")
            )
        )
    return q.order_by(models.Supplier.name).all()

@app.post("/suppliers", response_model=SupplierResponse, status_code=status.HTTP_201_CREATED)
@app.post("/suppliers/", response_model=SupplierResponse, status_code=status.HTTP_201_CREATED)
@app.post("/api/suppliers", response_model=SupplierResponse, status_code=status.HTTP_201_CREATED)
@app.post("/api/suppliers/", response_model=SupplierResponse, status_code=status.HTTP_201_CREATED)
def create_supplier(
    sup_in: SupplierCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    req: Request = None
):
    new_sup = models.Supplier(**sup_in.model_dump())
    db.add(new_sup)
    db.commit()
    db.refresh(new_sup)
    log_audit_trail(db, "CREATE", "suppliers", current_user.id, new_sup.id, f"Tedarikci eklendi: {new_sup.name}", get_client_ip(req))
    return new_sup

@app.put("/suppliers/{supplier_id}", response_model=SupplierResponse)
@app.put("/suppliers/{supplier_id}/", response_model=SupplierResponse)
@app.put("/api/suppliers/{supplier_id}", response_model=SupplierResponse)
@app.put("/api/suppliers/{supplier_id}/", response_model=SupplierResponse)
def update_supplier(
    supplier_id: int,
    sup_in: SupplierCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    req: Request = None
):
    db_sup = db.query(models.Supplier).filter(models.Supplier.id == supplier_id).first()
    if not db_sup:
        raise HTTPException(status_code=404, detail="Guncellenecek tedarikci bulunamadi.")
    
    for field, value in sup_in.model_dump(exclude_unset=True).items():
        setattr(db_sup, field, value)
    
    db_sup.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(db_sup)
    log_audit_trail(db, "UPDATE", "suppliers", current_user.id, db_sup.id, f"Tedarikci guncellendi: {db_sup.name}", get_client_ip(req))
    return db_sup

# --- TEDARİKÇİ SİLME (GÜNCELLENMİŞ) ---
@app.delete("/suppliers/{supplier_id}")
@app.delete("/suppliers/{supplier_id}/")
@app.delete("/api/suppliers/{supplier_id}")
@app.delete("/api/suppliers/{supplier_id}/")
def delete_supplier(
    supplier_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    # 1. Tedarikçiyi bul (Sadece ismini log'a ve mesaja yazdırmak için alıyoruz)
    db_sup = db.query(models.Supplier).filter(models.Supplier.id == supplier_id).first()
    if not db_sup:
        raise HTTPException(status_code=404, detail="Silinecek tedarikci bulunamadi.")
    
    supplier_name = db_sup.name
    
    try:
        from sqlalchemy import text
        
        # 2. Stok Güvenliği: Parçaları boşa çıkarıyoruz.
        # CAST ile sütunu açıkça metne (VARCHAR) çeviriyoruz ki tip hatası tamamen yok olsun.
        db.execute(
            text("UPDATE spare_parts SET supplier_id = NULL WHERE CAST(supplier_id AS VARCHAR) = :sid"),
            {"sid": str(supplier_id)}
        )
        
        # 3. KESİN ÇÖZÜM: db.delete(db_sup) KULLANMIYORUZ!
        # Arka planda o kilitlenen SELECT sorgusunun atılmasını engellemek için tedarikçiyi Saf SQL ile siliyoruz.
        db.execute(
            text("DELETE FROM suppliers WHERE id = :sid"),
            {"sid": supplier_id}
        )
        
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Veritabani silme hatasi: {str(e)}")
    
    # 4. Profesyonel Audit Log (Hata verse de sistemi çökertmez)
    try:
        ip = request.client.host if request.client else "unknown"
        log_audit_trail(db, "DELETE", "suppliers", current_user.id, supplier_id, f"Tedarikci silindi: {supplier_name}", ip)
    except Exception as log_e:
        logger.error(f"Audit log yazilamadi: {log_e}")
    
    return {"status": "success", "message": f"{supplier_name} basariyla silindi."}

# --- 14. GÖREV 1: VERİTABANI TEMİZLİĞİ VE MÜKERRER KAYIT DÜZELTME ---
@app.post("/cleanup-duplicates")
@app.post("/cleanup-duplicates/")
@app.post("/api/cleanup-duplicates")
@app.post("/api/cleanup-duplicates/")
def cleanup_duplicates(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin),
    req: Request = None
):
    """
    Excel aktarımından kaynaklanan çiftleyen (mükerrer) parçaları güvenle birleştirir.
    Barkodu veya OEM kodu aynı olan kayıtlardan ilki tutulur, diğerlerinin stokları
    ana kayda eklenir ve fazlalıklar silinir.
    """
    # Soft delete olmayan tüm parçaları tara
    all_parts = db.query(models.SparePart).filter(models.SparePart.is_deleted == False).order_by(models.SparePart.id).all()
    seen_identifiers = {}
    merged_count = 0
    deleted_ids = []

    for part in all_parts:
        # Öncelikli tanımlayıcı: Barkod veya OEM Kodu
        identifier = part.barcode.strip() if part.barcode else None
        if not identifier and part.oem_code:
            identifier = part.oem_code.strip()

        if not identifier:
            continue

        if identifier in seen_identifiers:
            primary_part = seen_identifiers[identifier]
            # Stokları ana parçada topla
            primary_part.stock_quantity += part.stock_quantity
            # Mükerrer olanı soft-delete yap
            part.is_deleted = True
            part.deleted_at = datetime.now(timezone.utc)
            deleted_ids.append(part.id)
            merged_count += 1
        else:
            seen_identifiers[identifier] = part

    db.commit()
    log_audit_trail(
        db,
        "DUPLICATE_CLEANUP",
        "spare_parts",
        current_user.id,
        None,
        f"{merged_count} mukerrer urun birlestirildi ve temizlendi.",
        get_client_ip(req)
    )

    return {
        "status": "success",
        "message": f"{merged_count} mükerrer ürün kaydı başarıyla temizlendi ve stokları birleştirildi.",
        "cleaned_records_count": merged_count,
        "cleaned_ids": deleted_ids[:50]  # İlk 50 örneği göster
    }

@app.post("/fix-unassigned-brands")
@app.post("/fix-unassigned-brands/")
@app.post("/api/fix-unassigned-brands")
@app.post("/api/fix-unassigned-brands/")
def fix_unassigned_brands(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_admin),
    req: Request = None
):
    """
    Markası boş, null veya 'Belirtilmemiş' olan ürünlerin adını tarayarak
    bilinen otomotiv/parça üreticisi markalarını otomatik olarak atar.
    """
    AUTOMOTIVE_BRANDS = [
        "Renault", "Fiat", "Ford", "Volkswagen", "BMW", "Mercedes", "Toyota", "Hyundai",
        "Peugeot", "Citroen", "Opel", "Audi", "Honda", "Nissan", "Skoda", "Seat", "Dacia",
        "Bosch", "Valeo", "Sachs", "Febi", "Delphi", "Mann", "Mahle", "Filtron", "Brembo",
        "LUK", "Monroe", "Gates", "Dayco", "NGK", "Denso", "Castrol", "Motul", "Liqui Moly"
    ]

    parts_to_fix = db.query(models.SparePart).filter(
        models.SparePart.is_deleted == False,
        or_(
            models.SparePart.brand == None,
            models.SparePart.brand == "",
            models.SparePart.brand == "Belirtilmemiş"
        )
    ).all()

    fixed_count = 0
    for part in parts_to_fix:
        part_name_upper = part.name.upper()
        for b in AUTOMOTIVE_BRANDS:
            # Kelime bazlı eşleşme (Örn: "RENAULT CLIO DEBRIYAJ" -> Renault)
            if re.search(rf"\b{re.escape(b.upper())}\b", part_name_upper):
                part.brand = b
                part.updated_at = datetime.now(timezone.utc)
                fixed_count += 1
                break

    db.commit()
    log_audit_trail(db, "UPDATE", "spare_parts", current_user.id, None, f"{fixed_count} urunun kayip markasi otomatik onarildi.", get_client_ip(req))

    return {
        "status": "success",
        "message": f"{fixed_count} ürünün markası ürün adından tespit edilerek güncellendi.",
        "fixed_count": fixed_count
    }

# --- 15. GÖREV 3: GEMINI AI AKILLI BARKOD ENTEGRASYONU ---
@app.post("/api/ai/smart-barcode")
@app.post("/api/ai/smart-barcode/")
@app.post("/ai/smart-barcode")
@app.post("/ai/smart-barcode/")
def ai_smart_barcode(
    payload: SmartBarcodeRequest,
    current_user: models.User = Depends(get_current_user)
):
    """
    Barkod ve opsiyonel ipucunu Google Gemini AI ile analiz eder.
    Parçanın adı, markası, uyumlu araçları ve OEM kodunu JSON formatında döndürür.
    """
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=503, detail="Gemini AI API Key tanimli degil.")

    prompt = f"""
    Sen otomotiv yedek parça ve oto sanayi uzmanısın.
    Aşağıdaki barkod / parça bilgisini analiz et:
    Barkod / Kod: "{payload.barcode}"
    Ek İpucu: "{payload.prompt_hint or 'Yok'}"

    Lütfen bu parçayı tespit et ve SADECE aşağıdaki JSON şemasına uygun geçerli bir JSON çıktısı ver:
    {{
        "name": "Parçanın tam ticari adı (Örn: Debriyaj Seti)",
        "brand": "Üretici veya Araç Markası (Örn: Valeo veya Renault)",
        "model": "Uyumlu Araç Modelleri (Örn: Clio 4 / Megane 3 1.5 dCi)",
        "oem_code": "Varsa OEM / Referans numarası",
        "category": "Parça Kategorisi (Örn: Motor, Fren, Filtre, Debriyaj)",
        "estimated_sell_price": 0.0,
        "description": "Kısa teknik bilgi"
    }}
    JSON dışında hiçbir açıklama veya markdown backtick (```json) ekleme.
    """

    model_candidates = ["gemini-3.5-flash-lite"]
    last_error = None

    for model_name in model_candidates:
        try:
            ai_model = genai.GenerativeModel(model_name)
            response = ai_model.generate_content(prompt)
            raw_text = response.text.strip()
            
            # Kod blokları varsa temizle
            raw_text = re.sub(r"^```json\s*", "", raw_text)
            raw_text = re.sub(r"^```\s*", "", raw_text)
            raw_text = re.sub(r"\s*```$", "", raw_text)

            parsed_data = json.loads(raw_text)
            return {"status": "success", "data": parsed_data}
        except Exception as e:
            logger.warning(f"Gemini {model_name} denemesi basarisiz: {e}")
            last_error = str(e)
            continue

    raise HTTPException(status_code=500, detail=f"AI analizi tamamlanamadi: {last_error}")

class AIReportRequest(BaseModel):
    prompt: str

@app.post("/generate_report")
@app.post("/generate_report/")
@app.post("/api/ai/generate-report")
@app.post("/api/ai/generate-report/")
def generate_ai_report(
    payload: AIReportRequest,
    current_user: models.User = Depends(get_current_user)
):
    """
    Flutter'dan gelen anlık mağaza verilerini (Stoklar, Satışlar, Tedarikçiler) alıp
    Gemini AI ile işletme raporu oluşturur.
    """
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=503, detail="Gemini AI API Key tanimli degil.")
    
    model_candidates = ["gemini-3.8-flash"]
    last_error = None

    for model_name in model_candidates:
        try:
            ai_model = genai.GenerativeModel(model_name)
            response = ai_model.generate_content(payload.prompt)
            # Flutter frontend'i {"data": "Rapor Metni"} veya doğrudan text bekleyebilir.
            # Dio loglarına göre JSON yanıtı yeterli.
            return {"status": "success", "data": response.text}
        except Exception as e:
            logger.warning(f"Gemini {model_name} rapor uretme basarisiz: {e}")
            last_error = str(e)
            continue
    
    raise HTTPException(status_code=500, detail=f"AI raporu olusturulamadi: {last_error}")

# --- 16. DASHBOARD & İSTATİSTİKLER ---
@app.get("/brands/summary")
@app.get("/brands/summary/")
@app.get("/api/brands/summary")
@app.get("/api/brands/summary/")
def get_brands_summary(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    results = db.query(
        models.SparePart.brand,
        func.count(models.SparePart.id).label("total_items"),
        func.sum(models.SparePart.stock_quantity).label("total_stock")
    ).filter(
        models.SparePart.is_deleted == False
    ).group_by(models.SparePart.brand).all()

    return [
        {
            "brand": r[0] if r[0] else "Belirtilmemiş",
            "total_items": r[1],
            "total_stock": r[2] or 0
        }
        for r in results
    ]

@app.get("/health")
@app.get("/health/")
@app.get("/api/health")
@app.get("/api/health/")
def health_check():
    return {"status": "healthy", "version": "4.0.0", "timestamp": datetime.now(timezone.utc).isoformat()}

# ==============================================================================
# 17. CARİ / VERESİYE (MÜŞTERİ HESAPLARI) YÖNETİMİ
# ==============================================================================

@app.get("/api/customers", response_model=List[CustomerResponse])
@app.get("/customers", response_model=List[CustomerResponse])
def get_all_customers(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Tüm aktif müşterileri (ustaları/firmaları) ve güncel bakiyelerini getirir"""
    return db.query(models.Customer).filter(models.Customer.is_deleted == False).all()

@app.post("/api/customers", response_model=CustomerResponse)
@app.post("/customers", response_model=CustomerResponse)
def create_customer(
    customer_in: CustomerCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Yeni bir cari hesap (Usta/Firma) açar"""
    new_cust = models.Customer(**customer_in.dict())
    db.add(new_cust)
    db.commit()
    db.refresh(new_cust)
    
    try:
        ip = "unknown" # İstek nesnesi (Request) eklenirse ip alınabilir
        log_audit_trail(db, "CREATE", "customers", current_user.id, new_cust.id, f"Yeni Cari: {new_cust.name}", ip)
    except:
        pass
        
    return new_cust

@app.post("/api/customers/{customer_id}/transaction")
@app.post("/api/customers/{customer_id}/transaction/")
@app.post("/customers/{customer_id}/transaction")
@app.post("/customers/{customer_id}/transaction/")
def add_customer_transaction(
    customer_id: int,
    trans_in: TransactionCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Cari hesaba manuel hareket ekler.
    Özellikle 'TAHSILAT' (Para Alma) işlemleri için kullanılır.
    Tahsilat yapıldığında müşterinin borcu (balance) düşer.
    """
    cust = db.query(models.Customer).filter(models.Customer.id == customer_id, models.Customer.is_deleted == False).first()
    if not cust:
        raise HTTPException(status_code=404, detail="Müşteri bulunamadı")
        
    if trans_in.amount <= 0:
        raise HTTPException(status_code=400, detail="Tutar sıfırdan büyük olmalıdır")

    # Yeni hareket kaydı
    new_trans = models.CustomerTransaction(
        customer_id=cust.id,
        transaction_type=trans_in.transaction_type,
        amount=trans_in.amount,
        description=trans_in.description
    )
    db.add(new_trans)
    
    # Bakiyeyi güncelle (Eğer tahsilat ise borç düşer, manuel borçlandırma ise borç artar)
    if trans_in.transaction_type == "TAHSILAT":
        cust.balance -= trans_in.amount
    elif trans_in.transaction_type == "BORCLANDIRMA":
        cust.balance += trans_in.amount
        
    db.commit()
    return {"status": "success", "message": f"{trans_in.amount} TL işlem kaydedildi", "new_balance": cust.balance}

# --- main.py içerisindeki mevcut get_customer_history metodu GÜNCELLENECEK ---
@app.get("/api/customers/{customer_id}/history")
@app.get("/api/customers/{customer_id}/history/")
@app.get("/customers/{customer_id}/history")
@app.get("/customers/{customer_id}/history/")
def get_customer_history(
    customer_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    cust = db.query(models.Customer).filter(models.Customer.id == customer_id).first()
    if not cust:
        raise HTTPException(status_code=404, detail="Müşteri bulunamadı")
        
    # PROFESYONEL ÇÖZÜM: 'BORCLANDIRMA' olanları (Satıştan gelenleri) getirme ki çift görünmesin.
    # Sadece TAHSILAT, IADE veya manuel atılan kayıtları getir.
    transactions = db.query(models.CustomerTransaction).filter(
        models.CustomerTransaction.customer_id == customer_id,
        models.CustomerTransaction.transaction_type != "BORCLANDIRMA"
    ).all()
    
    sales = db.query(models.SaleRecord).filter(
        models.SaleRecord.customer_id == customer_id,
        models.SaleRecord.payment_method == "Açık Hesap",
        models.SaleRecord.is_deleted == False
    ).all()
    
    history = []
    
    for t in transactions:
        history.append({
            "type": "TRANSACTION",
            "transaction_type": t.transaction_type,
            "amount": t.amount,
            "description": t.description or "Belirtilmemiş",
            "date": t.created_at
        })
        
    for s in sales:
        history.append({
            "type": "SALE",
            "sale_id": s.id,
            "part_name": s.part_name,
            "quantity": s.quantity,
            "unit_price": s.unit_price,
            "amount": s.total_price,
            "payment_method": s.payment_method,
            "date": s.sale_date or s.created_at
        })
        
    history.sort(key=lambda x: x["date"], reverse=True)
    return {"customer_name": cust.name, "current_balance": cust.balance, "history": history}

# --- YENİ EKLENECEK ROTA (Bunu get_customer_history'nin hemen altına ekle) ---
@app.put("/api/customers/{customer_id}")
@app.put("/api/customers/{customer_id}/")
@app.put("/customers/{customer_id}")
@app.put("/customers/{customer_id}/")
def update_customer(
    customer_id: int,
    customer_in: CustomerCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Müşteri (Cari) bilgilerini günceller"""
    cust = db.query(models.Customer).filter(models.Customer.id == customer_id).first()
    if not cust:
        raise HTTPException(status_code=404, detail="Müşteri bulunamadı")
        
    cust.name = customer_in.name
    cust.company_name = customer_in.company_name
    cust.phone = customer_in.phone
    cust.tax_number = customer_in.tax_number
    cust.tax_office = customer_in.tax_office
    cust.address = customer_in.address
    cust.notes = customer_in.notes
    
    db.commit()
    db.refresh(cust)
    return cust

@app.delete("/api/customers/{customer_id}")
@app.delete("/api/customers/{customer_id}/")
@app.delete("/customers/{customer_id}")
@app.delete("/customers/{customer_id}/")
def delete_customer(
    customer_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    cust = db.query(models.Customer).filter(models.Customer.id == customer_id).first()
    if not cust:
        raise HTTPException(status_code=404, detail="Müşteri bulunamadı")
    
    cust.is_deleted = True # Veritabanından tamamen uçurmuyoruz, sadece gizliyoruz (Güvenlik)
    db.commit()
    
    try:
        log_audit_trail(db, "DELETE", "customers", current_user.id, cust.id, f"Müşteri/Cari silindi: {cust.name}", "unknown")
    except:
        pass
        
    return {"status": "success", "message": "Müşteri silindi"}

@app.get("/api/audit-logs")
@app.get("/api/audit-logs/")
@app.get("/audit-logs")
@app.get("/audit-logs/")
def get_audit_logs(
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    if current_user.role not in ["admin", "patron"]:
        raise HTTPException(status_code=403, detail="Bu alanı sadece Patron görebilir.")
        
    logs = db.query(models.AuditLog).order_by(models.AuditLog.created_at.desc()).limit(limit).all()
    
    result = []
    for log in logs:
        # User tablosundan kullanıcı adını çekiyoruz
        user_name = "Sistem"
        if log.user_id:
            usr = db.query(models.User).filter(models.User.id == log.user_id).first()
            if usr:
                user_name = usr.username
                
        result.append({
            "id": log.id,
            "username": user_name,
            "action": log.action,
            "entity": log.entity_name,
            "details": log.details,
            "date": log.created_at
        })
    return result