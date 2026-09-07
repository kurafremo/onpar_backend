# 1. İçinde Python kurulu olan hazır ve hafif bir Linux sistemi indir
FROM python:3.11-slim

# 2. Konteyner içinde kendimize bir çalışma klasörü oluşturalım
WORKDIR /app

# 3. Kütüphane listemizi (requirements.txt) konteynerin içine kopyala
COPY requirements.txt .

# 4. Listeyi okuyup gerekli kütüphaneleri (fastapi, uvicorn) kur
RUN pip install --no-cache-dir -r requirements.txt

# 5. Bizim yazdığımız tüm kodları (main.py vs.) konteynerin içine kopyala
COPY . .

# 6. Uygulamayı 8000 portundan dışarıya açarak çalıştır
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]