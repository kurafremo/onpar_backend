import pandas as pd
import requests

# DİKKAT: Buradaki IP adresini sunucunun kendi yerel IP'si ile değiştir!
# Örnek: "http://192.168.1.48:8000/spare_parts/"
API_URL = "http://127.0.0.1:8001/parts"
DOSYA_ADI = "PARÇA.xlsx"

def aktarimi_baslat():
    print(f"[{DOSYA_ADI}] dosyası okunuyor. Lütfen bekleyin...")
    
    try:
        # Excel dosyasını oku
        df = pd.read_excel(DOSYA_ADI)
    except Exception as e:
        print(f"[SİSTEM HATASI] {parca_adi} iletilemedi. Detay: {e}")
        return

    basarili_sayisi = 0
    hatali_sayisi = 0
    
    print("Veritabanına aktarım başlıyor...\n" + "-"*40)
    
    # Satır satır dön ve veritabanına gönder
    for index, row in df.iterrows():
        # Eğer Parça Adı boşsa o satırı atla
        if pd.isna(row['PARÇA ADI']) or str(row['PARÇA ADI']).strip() == "":
            continue
            
        parca_adi = str(row['PARÇA ADI']).strip()
        markalar_raw = str(row['KULLANILDIĞI MARKALAR']).strip()
        
        # Uyumlu araçları tireden (-) bölüp temiz bir liste (Array) yapıyoruz
        uyumlu_araclar = []
        if markalar_raw and markalar_raw.lower() != 'nan':
            uyumlu_araclar = [m.strip() for m in markalar_raw.split('-') if m.strip()]
        
        # FastAPI'nin (Veritabanının) beklediği JSON şablonu
        payload = {
            "name": parca_adi,
            "compatible_vehicles": uyumlu_araclar,
            "buy_price": 0.0,       # Sıfır kuralı
            "sell_price": 0.0,      # Sıfır kuralı
            "stock_quantity": 0,    # Sıfır kuralı
            "critical_stock_level": 5
        }
        
        try:
            response = requests.post(API_URL, json=payload)
            if response.status_code in [200, 201]:
                basarili_sayisi += 1
                print(f"[EKLENDİ] {parca_adi}")
            else:
                hatali_sayisi += 1
                print(f"[HATA {response.status_code}] {parca_adi} eklenemedi!")
        except Exception as e:
            hatali_sayisi += 1
            print(f"[BAĞLANTI HATASI] {parca_adi} sunucuya iletilemedi.")
            
    print("-" * 40)
    print("AKTARİM TAMAMLANDI!")
    print(f"Başarıyla Eklenen: {basarili_sayisi} Ürün")
    print(f"Hata Alınan: {hatali_sayisi} Ürün")

if __name__ == "__main__":
    aktarimi_baslat()