# SPF Gateway (v1.4) - Growatt Multi-Inverter Monitoring

Sistem monitoring dan kontrol jarak jauh (IoT) untuk inverter **Growatt SPF** (Off-Grid) menggunakan ESP32 + Modul RS485 (MAX485), dengan server Python (FastAPI) yang berjalan di dalam Docker.

- **ESP32** membaca Modbus RTU dari inverter dan mengirimkannya ke server via WiFi.
- **Server** (FastAPI + SQLite) menyimpan histori, menyajikan dashboard, dan mengantre perintah kontrol.
- **Dashboard** mendukung **Multi-Inverter** (bisa memantau lebih dari satu ESP32/inverter sekaligus).
- **Keamanan**: Dashboard dilindungi Basic Auth, API dilindungi API Key.
- **Keandalan**: ESP32 dilengkapi Watchdog, Auto-Reconnect, dan Offline Buffer.

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

Pastikan folder proyek Anda rapi seperti ini sebelum menjalankan Docker:

```text
spf-gateway/
├── data/                  # (Dibuat otomatis oleh Docker) Berisi spf.db
├── docs/                  # Dokumentasi versi sebelumnya (v1.1 - v1.4)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── server.py
├── dashboard.html
├── main.py                # Kode untuk ESP32 (MicroPython)
└── README.md
```
*(Catatan: Hapus file `spf.db` jika ada di folder utama. Biarkan Docker membuatnya di dalam folder `data/`).*

---

## 🚀 1. Deployment Sisi Server (Docker)

Anda hanya perlu melakukan ini sekali. Pastikan **Docker Desktop** sudah terinstal dan menyala di laptop/PC Anda.

1.  Buka terminal/CMD, masuk ke folder proyek (`spf-gateway`).
2.  Jalankan perintah build dan start:
    ```bash
    docker-compose up -d --build
    ```
3.  Buka browser dan akses: `http://localhost:8000`
4.  Login dengan:
    *   **Username:** `admin`
    *   **Password:** `growatt123`
5.  **Perintah Docker Penting:**
    *   Melihat log server: `docker-compose logs -f`
    *   Mematikan server: `docker-compose down`
    *   Restart server: `docker-compose restart`

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
Buka file `main.py` dan sesuaikan variabel di bagian paling atas:
```python
WIFI_SSID = "NAMA_WIFI_ANDA"
WIFI_PASS = "PASSWORD_WIFI_ANDA"
SERVER_URL = "http://192.168.1.10:8000/api/ingest" # Ganti IP dengan IP Laptop Anda
API_KEY = "KunciRahasiaESP32Anda"                  # Harus sama dengan di server.py
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
*   Untuk mengaktifkan notifikasi Telegram, isi `TELEGRAM_TOKEN` dan `TELEGRAM_CHAT_ID` di dalam file `server.py`, lalu jalankan `docker-compose up -d --build` kembali.

---

## 🛠️ Troubleshooting

| Gejala | Kemungkinan Penyebab | Solusi |
| :--- | :--- | :--- |
| **Dashboard minta password terus** | Password salah atau sesi browser habis. | Masukkan `admin` / `growatt123`. Refresh halaman. |
| **ESP32 log: `Gagal kirim ke server: 401`** | API Key tidak cocok. | Samakan string `API_KEY` di `main.py` dan `server.py`. |
| **Inverter tidak menjawab** | Kabel A/B terbalik, atau salah port RS485. | Tukar kabel A dan B. Pastikan kabel di port yang benar. |
| **Setingan gagal (Log: `gagal`)** | Inverter sibuk / EEPROM belum siap. | Klik "Terapkan" sekali lagi. Sistem sudah punya *retry* otomatis. |
| **Grafik tidak muncul** | Laptop tidak terhubung internet (Chart.js CDN). | Hubungkan laptop ke internet atau unduh Chart.js secara lokal. |
| **Dropdown Inverter kosong** | ESP32 belum mengirim data atau API Key salah. | Cek log ESP32, pastikan `API_KEY` cocok dan server menyala. |
| **Data hilang saat Docker restart** | Volume tidak terpasang dengan benar. | Pastikan folder `data/` ada di folder yang sama dengan `docker-compose.yml`. |

---
**Catatan Rilis:**
*   **v1.4:** Skalabilitas Multi-Inverter & Docker.
*   **v1.3:** Otomatisasi SOC & Notifikasi Telegram.
*   **v1.2:** Keamanan API Key & Basic Auth.
*   **v1.1:** Keandalan Watchdog, Auto-Reconnect, & Retry.
```
