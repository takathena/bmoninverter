# SPF Gateway (v1.4) - Growatt Multi-Inverter Monitoring

Sistem monitoring dan kontrol jarak jauh (IoT) untuk inverter **Growatt SPF** (Off-Grid) menggunakan ESP32 + Modul RS485 (MAX485), dengan server Python (FastAPI) yang berjalan di dalam Docker.

- **ESP32** membaca Modbus RTU dari inverter dan mengirimkannya ke server via WiFi.
- **Server** (FastAPI + SQLite) menyimpan histori, menyajikan dashboard, dan mengantre perintah kontrol.
- **Dashboard** mendukung **Multi-Inverter** (bisa memantau lebih dari satu ESP32/inverter sekaligus).
- **Keamanan**: Dashboard dilindungi Basic Auth, API dilindungi API Key.
- **Keandalan**: ESP32 dilengkapi Watchdog, Auto-Reconnect, dan Offline Buffer.
- **Konfigurasi Fleksibel**: Semua pengaturan sensitif (password, API Key, dll) disimpan di file `.env`.

## 📐 Arsitektur Sistem

```text
[ Inverter 1 ] ──RS485──> [ ESP32 A ] (device_id: "esp32_rumah")
                                  │
                                  │ HTTP POST (Data + API Key)
                                  ▼
[ Inverter 2 ] ──RS485──> [ ESP32 B ] (device_id: "esp32_gudang")
                                  │
                                  │ HTTP POST (Data + API Key)
                                  ▼
                       [ WiFi Router / LAN ]
                                  │
                                  ▼
+---------------------------------------------------------+
|                  [ Docker Container ]                   |
|  [ FastAPI Server ]                                     |
|  - Validasi API Key                                     |
|  - Simpan histori (SQLite) berdasarkan device_id        |
|  - Otomatisasi SOC & Notifikasi Telegram                |
|                                                         |
|  [ SQLite (data/spf.db) ] <── (Volume Mounted ke Host)  |
+---------------------------------------------------------+
                                  ▲
                                  │ HTTP GET (polling)
                                  │
                       [ Browser (Dashboard) ]
                       - Login (Basic Auth)
                       - Dropdown pilih Inverter
```

## 📂 Struktur Folder

Pastikan folder proyek Anda rapi seperti ini:

```text
spf-gateway/
├── data/                  # (Dibuat otomatis oleh Docker) Berisi spf.db
├── docs/                  # Dokumentasi versi sebelumnya (v1.1 - v1.4)
├── .env                   # File konfigurasi rahasia (WAJIB DIBUAT)
├── .dockerignore
├── .gitignore
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── server.py
├── dashboard.html
├── main.py                # Kode untuk ESP32 (MicroPython)
└── README.md
```
*(Catatan: Pastikan tidak ada file `spf.db` di folder utama. Database akan otomatis dibuat di dalam folder `data/`).*

---

## 🚀 1. Deployment Sisi Server (Docker)

### A. Buat File Konfigurasi `.env`
Ini adalah langkah **paling penting**. Buat file baru bernama `.env` (titik di depannya wajib) di folder yang sama dengan `docker-compose.yml`. Isi dengan konfigurasi Anda:

```env
# ================= KEAMANAN =================
DASHBOARD_USER=admin
DASHBOARD_PASS=PasswordRahasiaAnda
API_KEY=KunciRahasiaESP32Anda

# ================= TELEGRAM (Opsional) =================
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=

# ================= OTOMATISASI SOC =================
AUTO_SOC_ENABLED=True
SOC_LOW_THRESHOLD=30
SOC_HIGH_THRESHOLD=85

# ================= PENGATURAN SISTEM =================
OFFLINE_AFTER_S=15
CMD_TIMEOUT_S=40
SERVER_PORT=8000
```

### B. Jalankan Docker
Pastikan **Docker Desktop** sudah terinstal dan menyala. Buka terminal/CMD, masuk ke folder proyek, lalu jalankan:

```bash
docker-compose up -d --build
```

### C. Akses Dashboard
1. Buka browser dan akses: `http://localhost:8000`
2. Login menggunakan **Username** dan **Password** yang sudah Anda tulis di file `.env` tadi.

**Perintah Docker Penting:**
*   Melihat log server: `docker-compose logs -f`
*   Mematikan server: `docker-compose down`
*   Restart server (jika ganti settingan di `.env`): `docker-compose restart`

---

## 🔌 2. Deployment Sisi ESP32

### A. Persiapan Hardware (Wiring MAX485 ke ESP32)
*   `VCC` -> 5V ESP32
*   `GND` -> GND ESP32
*   `DI` -> P17 (TX2)
*   `RO` -> P16 (RX2) **[Wajib pakai Pembagi Tegangan: Resistor 1k & 2k]**
*   `DE + RE` -> P4 (Disambung jadi satu)
*   `A, B` -> Port RS485 Inverter (Tukar A/B jika gagal konek)

### B. Konfigurasi Kode (`main.py`)
Buka file `main.py` di folder ESP32 dan sesuaikan variabel di bagian paling atas:
```python
WIFI_SSID = "NAMA_WIFI_ANDA"
WIFI_PASS = "PASSWORD_WIFI_ANDA"
SERVER_URL = "http://192.168.1.10:8000/api/ingest" # Ganti IP dengan IP Laptop Anda
API_KEY = "KunciRahasiaESP32Anda"                  # HARUS SAMA dengan API_KEY di file .env
DEVICE_ID = "esp32_rumah"                          # Beri nama unik untuk inverter ini
```

### C. Upload ke ESP32
1.  Buka **Thonny** atau **mpremote**.
2.  Upload file `main.py` ke ESP32.
3.  Tekan tombol **Reset** pada ESP32.
4.  Lihat Serial Monitor. Jika berhasil, akan muncul:
    *   `WiFi OK: 192.168.x.x`
    *   `Inverter menjawab di alamat 1` (atau 11)

---

## ⚙️ 3. Cara Menggunakan Dashboard

### Multi-Inverter
Di pojok kiri atas dashboard, terdapat *dropdown* **Inverter**. 
*   Jika Anda menyalakan ESP32 kedua (misal dengan `DEVICE_ID = "esp32_gudang"`), nama tersebut akan otomatis muncul di dropdown tanpa perlu me-restart server.
*   Pilih nama inverter pada dropdown untuk melihat data, grafik, dan settingan khusus inverter tersebut.

### Kontrol & Setingan
Anda bisa mengubah mode pengisian baterai (PV/PLN), jam operasional PLN, dan batas tegangan baterai. 
*   Perubahan akan dikirim ke ESP32 pada siklus berikutnya (3-6 detik).
*   Setelah ditulis, ESP32 akan membaca ulang untuk memastikan perintah berhasil (Read-back).
*   Log perubahan (berhasil/gagal) dapat dilihat di tabel bagian bawah.

### Auto-SOC & Notifikasi Telegram
*   Klik tombol **Auto-SOC** di pojok kanan atas untuk mengaktifkan otomatisasi baterai (jika SOC < 30% paksa pakai PLN, jika > 85% paksa pakai PV saja).
*   Untuk mengaktifkan notifikasi Telegram, isi `TELEGRAM_TOKEN` dan `TELEGRAM_CHAT_ID` di dalam file `.env`, lalu jalankan `docker-compose restart`.

---

## 🛠️ Troubleshooting

| Gejala | Kemungkinan Penyebab | Solusi |
| :--- | :--- | :--- |
| **Dashboard minta password terus** | Password salah atau sesi browser habis. | Cek kembali Username/Password di file `.env`. Refresh halaman. |
| **ESP32 log: `Gagal kirim ke server: 401`** | API Key tidak cocok. | Samakan string `API_KEY` di `main.py` ESP32 dan file `.env` di server. |
| **Inverter tidak menjawab** | Kabel A/B terbalik, atau salah port RS485. | Tukar kabel A dan B. Pastikan kabel di port yang benar. |
| **Setingan gagal (Log: `gagal`)** | Inverter sibuk / EEPROM belum siap. | Klik "Terapkan" sekali lagi. Sistem sudah punya *retry* otomatis. |
| **Grafik tidak muncul** | Laptop tidak terhubung internet (Chart.js CDN). | Hubungkan laptop ke internet atau unduh Chart.js secara lokal. |
| **Data hilang saat Docker restart** | Volume tidak terpasang dengan benar. | Pastikan folder `data/` ada di folder yang sama dengan `docker-compose.yml`. |
| **Server gagal menyala (Port bentrok)** | Port 8000 dipakai aplikasi lain. | Ubah `SERVER_PORT=8001` di file `.env`, lalu jalankan `docker-compose up -d` ulang. |

---
**Catatan Rilis:**
*   **v1.4:** Skalabilitas Multi-Inverter, Docker, & Konfigurasi `.env`.
*   **v1.3:** Otomatisasi SOC & Notifikasi Telegram.
*   **v1.2:** Keamanan API Key & Basic Auth.
*   **v1.1:** Keandalan Watchdog, Auto-Reconnect, & Retry.
