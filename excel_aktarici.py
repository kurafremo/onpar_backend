"""
On-Par Otomotiv ERP & POS - Enterprise Excel Data Migration Tool
Version: 4.0.0 (Enterprise B2B, High-Performance Bulk Insert & Data Armor)

Açıklama:
  Excel dosyasındaki (PARÇA.xlsx) ürünleri temizler, NaN/Null hatalarını önler,
  marka ve OEM kodlarını akıllıca ayrıştırır ve SQLAlchemy 'add_all' / 'bulk_insert'
  ile saniyeler içinde veritabanına aktarır.
"""

import os
import sys
import re
import time
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

import pandas as pd

# ==============================================================================
# 1. VERİTABANI BAĞLANTISI VE MODEL ENTEGRASYONU (ESNEK MİMARİ)
# ==============================================================================
# Script hem sunucu kök dizininde hem de alt klasörlerde çalıştırılabilmesi için path eklenir
CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.append(str(CURRENT_DIR))
if str(CURRENT_DIR.parent) not in sys.path:
    sys.path.append(str(CURRENT_DIR.parent))

try:
    from database import SessionLocal, engine, Base
    from models import SparePart
    DB_LOADED = True
except ImportError:
    SessionLocal = None
    engine = None
    Base = None
    SparePart = None
    DB_LOADED = False

# Excel Dosya Adı
EXCEL_FILE_PATH = "PARÇA.xlsx"

# ==============================================================================
# 2. BİLİNEN OTOMOTİV PARÇA VE ARAÇ MARKALARI SÖZLÜĞÜ (AKILLI MARKA AYRIŞTIRICI)
# ==============================================================================
KNOWN_BRANDS = [
    # Yedek Parça Üreticileri
    "BOSCH", "VALEO", "DELPHI", "DELPHİ", "MAHLE", "MANN", "LUK", "SACHS", "FERODO",
    "BREMBO", "TRW", "GATES", "DAYCO", "SKF", "FAG", "INA", "HELLA", "DENSO", "NGK",
    "BERU", "MONROE", "KYB", "FEBI", "SWAG", "MEYLE", "LEMFORDER", "FILTRON", "WIX",
    "PURFLUX", "CHAMPION", "BSG", "KALE", "DEPO", "TYC", "MAGNETI MARELLI", "MARELLI",
    "CONTINENTAL", "CONTITECH", "CORTECO", "VICTOR REINZ", "ELRING", "AISIN", "SNR",
    "NTN", "AUTODAK", "NEIMAN", "AFT", "ZENON", "ORJ", "ORJİNAL", "ORJINAL",
    # Otomobil Üreticileri (OEM)
    "FIAT", "FİAT", "RENAULT", "PEUGEOT", "CITROEN", "CİTROEN", "DACIA", "DACİA",
    "VOLKSWAGEN", "VW", "AUDI", "AUDİ", "SEAT", "SKODA", "ŞKODA", "OPEL", "FORD",
    "BMW", "MERCEDES", "TOYOTA", "HONDA", "HYUNDAI", "HYUNDAİ", "KIA", "KİA", "NISSAN", "NİSSAN"
]

def get_utc_now():
    return datetime.now(timezone.utc)

# ==============================================================================
# 3. ZIRHLI VERİ TEMİZLEME VE PARSE FONKSİYONLARI (NaN ÇÖKMELERİNİ YOK EDER)
# ==============================================================================
def clean_price(val: Any, default: float = 0.0) -> float:
    """
    Excel'deki '557,5 TL', '1.250,00 TL', NaN veya formatlı fiyatları
    asla patlamadan güvenli bir 'float' sayıya dönüştürür.
    """
    if val is None or pd.isna(val):
        return default
    if isinstance(val, (int, float)):
        if math.isnan(val) or math.isinf(val):
            return default
        return round(float(val), 2)
    
    val_str = str(val).strip()
    # Para birimi veya harfleri temizle, sadece rakam, nokta, virgül ve eksi kalsın
    val_str = re.sub(r'[^\d,.-]', '', val_str)
    if not val_str:
        return default
    
    # Türkçe format (1.250,50) veya İngilizce format (1,250.50) kontrolü
    if ',' in val_str and '.' in val_str:
        if val_str.rfind(',') > val_str.rfind('.'):
            # 1.250,50 -> 1250.50
            val_str = val_str.replace('.', '').replace(',', '.')
        else:
            # 1,250.50 -> 1250.50
            val_str = val_str.replace(',', '')
    elif ',' in val_str:
        val_str = val_str.replace(',', '.')
        
    try:
        f = float(val_str)
        return default if math.isnan(f) or math.isinf(f) else round(f, 2)
    except (ValueError, TypeError):
        return default

def clean_int(val: Any, default: int = 0) -> int:
    """
    Excel'deki stok ve kritik seviye hücrelerini güvenli int yapar.
    """
    if val is None or pd.isna(val):
        return default
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return default
        return int(val)
    f = clean_price(val, default=float(default))
    return int(f)

def clean_str(val: Any, max_len: Optional[int] = None, default: Optional[str] = None) -> Optional[str]:
    """
    Hücreleri güvenli string yapar. Boş veya 'nan' ise None veya default döner.
    Veritabanı kolon uzunluk sınırını (max_len) koruyarak PostgreSQL taşma hatalarını engeller.
    """
    if val is None or pd.isna(val):
        return default
    s = str(val).strip()
    if not s or s.lower() in ('nan', 'none', 'null', '<na>'):
        return default
    if max_len and len(s) > max_len:
        s = s[:max_len].strip()
    return s

def extract_brand_and_oem(name: str, models_str: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """
    Parça adından ve araç bilgisinden Marka ve OEM kodunu otomatik ayrıştırır.
    Örnek: '254039 VALEO KRANK SENSÖRÜ' -> Marka: 'VALEO', OEM: '254039'
    """
    brand = "Diğer / Markasız"
    oem_code = None
    
    if not name:
        return brand, oem_code
    
    tokens = name.split()
    
    # 1. OEM Kod Ayrıştırma: İlk kelime rakam içeriyorsa ve 4+ hane ise OEM kodudur
    if tokens:
        first_token = tokens[0].strip().replace('(', '').replace(')', '')
        if any(c.isdigit() for c in first_token) and len(first_token) >= 4:
            oem_code = clean_str(first_token, max_len=100)
            
    # 2. Marka Ayrıştırma: İsimdeki bilinen marka eşleşmesi
    name_upper = name.upper()
    for b in KNOWN_BRANDS:
        pattern = r'\b' + re.escape(b) + r'\b'
        if re.search(pattern, name_upper):
            brand = "ORJİNAL" if b in ("ORJ", "ORJİNAL", "ORJINAL") else b
            break
            
    # 3. İsimde marka yoksa 'Kullanıldığı Markalar' sütunundan kontrol et
    if brand == "Diğer / Markasız" and models_str:
        models_upper = models_str.upper()
        for b in KNOWN_BRANDS:
            pattern = r'\b' + re.escape(b) + r'\b'
            if re.search(pattern, models_upper):
                brand = b
                break
                
    return brand, oem_code

# ==============================================================================
# 4. EXCEL KOLONLARI OTOMATİK EŞLEŞTİRİCİ
# ==============================================================================
def find_column(df_columns: List[str], candidates: List[str]) -> Optional[str]:
    """Kolon isimlerindeki boşluk ve büyük/küçük harf duyarlılığını kaldırarak doğru kolonu bulur."""
    cleaned_candidates = [c.upper().replace(' ', '').replace('_', '') for c in candidates]
    for col in df_columns:
        norm = str(col).upper().replace(' ', '').replace('_', '')
        if norm in cleaned_candidates:
            return col
    return None

# ==============================================================================
# 5. ENTERPRISE TOPLU AKTARIM MOTORU (BULK INSERT ENGINE)
# ==============================================================================
def excel_aktarimi_calistir(file_path: str = EXCEL_FILE_PATH, batch_size: int = 500):
    start_time = time.time()
    
    print("=" * 65)
    print("  ON-PAR OTOMOTİV B2B ERP - ENTERPRISE DATA MIGRATION ENGINE")
    print("  Version: 4.0.0 (High-Speed Bulk Insert & Data Armor)")
    print("=" * 65)
    
    if not DB_LOADED:
        print("\n[KRİTİK HATA] 'database.py' veya 'models.py' yüklenemedi!")
        print("Lütfen bu scripti FastAPI projenizin kök dizininde çalıştırın.")
        return

    # 1. Dosya Kontrolü
    p = Path(file_path)
    if not p.exists():
        alt_paths = [Path(CURRENT_DIR, file_path), Path(CURRENT_DIR.parent, file_path)]
        found = False
        for alt in alt_paths:
            if alt.exists():
                p = alt
                found = True
                break
        if not found:
            print(f"\n[HATA] Excel dosyası bulunamadı: '{file_path}'")
            print("Lütfen dosyanın script ile aynı klasörde olduğundan emin olun.")
            return

    print(f"\n[1/4] Excel Dosyası Okunuyor: '{p.name}'...")
    try:
        df = pd.read_excel(p)
        print(f"      -> Okunan Ham Satır Sayısı: {len(df):,}")
    except Exception as e:
        print(f"[DOSYA OKUMA HATASI] Excel okunamadı: {e}")
        return

    # 2. Kolon Eşleştirme
    cols = df.columns.tolist()
    col_name = find_column(cols, ['PARÇA ADI', 'PARCA ADI', 'ÜRÜN ADI', 'URUN ADI', 'NAME', 'PARCA'])
    col_models = find_column(cols, ['KULLANILDIĞI MARKALAR', 'KULLANILDIGI MARKALAR', 'MODEL', 'UYUMLU ARAÇLAR', 'ARAÇLAR'])
    col_stock = find_column(cols, ['ADET', 'STOK', 'STOK MİKTARI', 'STOK ADET', 'STOCK', 'QUANTITY'])
    col_buy = find_column(cols, ['GELİŞ FİYATI', 'GELIS FIYATI', 'ALIŞ FİYATI', 'ALIS FIYATI', 'BUY_PRICE'])
    col_sell = find_column(cols, ['SATIŞ FİYATI', 'SATIS FIYATI', 'FİYAT', 'FIYAT', 'SELL_PRICE'])
    col_barcode = find_column(cols, ['BARKOD', 'BARCODE', 'BARKOD NO'])
    col_oem = find_column(cols, ['OEM', 'OEM KODU', 'OEM_CODE', 'ORJİNAL NO', 'ORJINAL NO'])
    col_location = find_column(cols, ['RAF', 'KONUM', 'LOCATION', 'RAF NO', 'KUTU'])
    col_brand = find_column(cols, ['MARKA', 'BRAND'])

    if not col_name:
        print("\n[HATA] Excel'de 'PARÇA ADI' kolonu tespit edilemedi!")
        print(f"Mevcut Kolonlar: {cols}")
        return

    print(f"\n[2/4] Kolonlar Eşleştirildi:")
    print(f"      - Parça Adı     : '{col_name}'")
    print(f"      - Uyumlu Araçlar: '{col_models or 'Yok (Boş geçilecek)'}'")
    print(f"      - Stok Miktarı  : '{col_stock or 'Yok (0 atanacak)'}'")
    print(f"      - Geliş Fiyatı  : '{col_buy or 'Yok (0.0 atanacak)'}'")
    print(f"      - Satış Fiyatı  : '{col_sell or 'Yok (0.0 atanacak)'}'")

    print("\n[3/4] Veriler Temizleniyor ve Modelleniyor...")
    
    spare_parts_to_insert: List[Dict[str, Any]] = []
    seen_barcodes = set()
    skipped_empty_count = 0
    now = get_utc_now()

    for idx, row in df.iterrows():
        raw_name = row.get(col_name)
        cleaned_name = clean_str(raw_name, max_len=200)
        
        # Parça adı boş olan satırları atla
        if not cleaned_name:
            skipped_empty_count += 1
            continue

        raw_models = row.get(col_models) if col_models else None
        cleaned_models = clean_str(raw_models, max_len=100)

        # Marka ve OEM kodunu belirle
        excel_brand = clean_str(row.get(col_brand), max_len=100) if col_brand else None
        excel_oem = clean_str(row.get(col_oem), max_len=100) if col_oem else None

        detected_brand, detected_oem = extract_brand_and_oem(cleaned_name, cleaned_models)
        final_brand = excel_brand or detected_brand
        final_oem = excel_oem or detected_oem

        # Fiyat ve Stok Değerleri (NaN Korumalı)
        buy_price = clean_price(row.get(col_buy)) if col_buy else 0.0
        sell_price = clean_price(row.get(col_sell)) if col_sell else 0.0
        stock_qty = clean_int(row.get(col_stock)) if col_stock else 0
        location = clean_str(row.get(col_location), max_len=50) if col_location else None

        # Barkod ve Unique Kontrolü (Boşluklar NULL yapılır)
        barcode_raw = clean_str(row.get(col_barcode), max_len=50) if col_barcode else None
        final_barcode = None
        if barcode_raw and barcode_raw not in seen_barcodes:
            final_barcode = barcode_raw
            seen_barcodes.add(barcode_raw)

        # Enterprise Model Sözlüğü (Zorunlu alanlar ve default değerler)
        part_data = {
            "name": cleaned_name,
            "brand": final_brand,
            "model": cleaned_models,
            "oem_code": final_oem,
            "barcode": final_barcode,
            "stock_quantity": stock_qty,
            "buy_price": buy_price,
            "sell_price": sell_price,
            "location": location,
            "min_stock_alert": 5,      # Enterprise default
            "supplier_id": None,
            "is_deleted": False,       # Enterprise soft-delete koruması
            "deleted_at": None,
            "created_at": now,
            "updated_at": now,
        }
        spare_parts_to_insert.append(part_data)

    total_valid = len(spare_parts_to_insert)
    print(f"      -> Aktarıma Hazır Temiz Ürün : {total_valid:,}")
    print(f"      -> Atlanan Boş/Geçersiz Satır: {skipped_empty_count:,}")

    if total_valid == 0:
        print("\n[UYARI] Aktarılacak geçerli ürün bulunamadı. İşlem sonlandırıldı.")
        return

    # 3. Veritabanına Yüksek Performanslı Toplu Yazma (Bulk Insert)
    print(f"\n[4/4] Veritabanına Toplu Yazılıyor (Paket Boyutu: {batch_size})...")
    
    db = SessionLocal()
    inserted_count = 0
    
    try:
        # Tabloların varlığından emin ol
        Base.metadata.create_all(bind=engine)

        total_batches = math.ceil(total_valid / batch_size)
        
        for i in range(0, total_valid, batch_size):
            chunk = spare_parts_to_insert[i:i + batch_size]
            
            # ORM nesneleriyle toplu yazım (add_all)
            orm_objects = [SparePart(**data) for data in chunk]
            db.add_all(orm_objects)
            db.commit()
            
            inserted_count += len(chunk)
            current_batch = (i // batch_size) + 1
            progress_pct = int((inserted_count / total_valid) * 100)
            
            # Terminal İlerleme Çubuğu
            bar_length = 30
            filled_length = int(bar_length * inserted_count // total_valid)
            bar = '█' * filled_length + '-' * (bar_length - filled_length)
            sys.stdout.write(f"\r      [{bar}] {progress_pct}% ({inserted_count:,}/{total_valid:,}) - Paket {current_batch}/{total_batches}")
            sys.stdout.flush()

        print("\n")
    except Exception as e:
        db.rollback()
        print(f"\n\n[VERİTABANI HATASI] Toplu aktarım sırasında hata oluştu: {e}")
        print("İşlem geri alındı (Rollback yapıldı).")
        return
    finally:
        db.close()

    elapsed = time.time() - start_time
    total_stock = sum(p["stock_quantity"] for p in spare_parts_to_insert)
    total_valuation = sum(p["sell_price"] * p["stock_quantity"] for p in spare_parts_to_insert)
    distinct_brands = len(set(p["brand"] for p in spare_parts_to_insert if p["brand"]))

    # 4. Final Başarı Raporu
    print("=" * 65)
    print("  ✅ AKTARIM BAŞARIYLA TAMAMLANDI!")
    print("=" * 65)
    print(f"  📦 Toplam Aktarılan Ürün   : {inserted_count:,} adet")
    print(f"  🏷️  Tespit Edilen Marka     : {distinct_brands} farklı marka")
    print(f"  📊 Toplam Stok Adedi       : {total_stock:,} parça")
    print(f"  💰 Toplam Raf Satış Değeri : ₺{total_valuation:,.2f}")
    print(f"  ⏱️  Toplam İşlem Süresi     : {elapsed:.2f} saniye")
    print("=" * 65)
    print("Sistem artık B2B ERP ve mobil uygulama ile %100 senkronize çalışmaya hazır!\n")

if __name__ == "__main__":
    excel_aktarimi_calistir()