# SPF Gateway (prototype)

Gateway monitoring dan pengaturan inverter **Growatt SPF** (off-grid) memakai ESP32 + modul MAX485, dengan dashboard web di laptop.

- **ESP32** membaca inverter lewat Modbus RTU (RS485), lalu mengirim datanya ke server lewat WiFi.
- **Server** (Python/FastAPI) menyimpan riwayat dan menyajikan dashboard.
- **Dashboard** menampilkan data live, grafik riwayat, dan memungkinkan mengubah setingan inverter.

> Status: prototype. Peta register sudah dicocokkan dengan dokumen protokol Growatt, tetapi kode belum dites lama di lapangan. Jangan dipakai tanpa pengawasan.

## Arsitektur

```
Inverter ──RS485──► MAX485 ──► ESP32 ──WiFi/HTTP──► Server (laptop) ──► Dashboard (browser)
                                  ▲                      │
                                  └── perintah ubah setingan (dibalas di tiap kiriman data)
```

ESP32 yang selalu memulai koneksi (POST ke server), jadi laptop tidak perlu bisa dihubungi dari ESP32. Perintah ubah setingan diantre di server dan diambil ESP32 pada kiriman berikutnya (sekitar 3-6 detik).

## Struktur folder

```
spf-gateway/
├── README.md
├── esp32/
│   └── main.py            # MicroPython: baca inverter, kirim ke server, tulis setingan
└── server/
    ├── server.py          # FastAPI + SQLite
    ├── dashboard.html     # tampilan dashboard
    └── requirements.txt
```

## Kebutuhan

- ESP32 dengan firmware **MicroPython**
- Modul **MAX485** (TTL ke RS485)
- Resistor 1k dan 2k (pembagi voltase untuk jalur RO)
- Kabel RJ45 untuk port RS485 inverter
- Laptop dengan Python 3.9+ dan satu jaringan WiFi dengan ESP32

## Wiring

| MAX485 | ESP32 |
|---|---|
| VCC | 5V |
| GND | GND |
| DI | P17 (TX2) |
| RO | P16 (RX2), lewat pembagi voltase |
| DE + RE | P4 (kedua pin disambung jadi satu) |
| A, B | A, B di port RS485 inverter |

Pembagi voltase untuk RO (output RO = 5 V, pin ESP32 maksimal 3,3 V):

```
MAX485 RO ──[ 1k ]──┬──► P16 ESP32
                    │
                  [ 2k ]
                    │
                   GND
```

**Pin RJ45 mana yang A dan B jangan ditebak.** Cek gambar pinout di repo referensi (lihat bagian Referensi) atau manual inverter sebelum menyambung. Kalau data tidak masuk, tukar A dan B.

## Menjalankan

### 1. Server (laptop)

```bash
cd server
pip install -r requirements.txt
python server.py
```

Buka `http://localhost:8000`. Server mendengarkan di port 8000 untuk semua antarmuka. Kalau ESP32 tidak bisa terhubung, cek firewall laptop untuk port 8000.

Cari IP laptop di jaringan WiFi (misalnya `192.168.1.10`).

### 2. ESP32

1. Edit bagian atas `esp32/main.py`:

   ```python
   WIFI_SSID = "NAMA_WIFI"
   WIFI_PASS = "PASSWORD_WIFI"
   SERVER_URL = "http://192.168.1.10:8000/api/ingest"   # IP laptop
   SLAVE_CANDIDATES = [1, 11]                           # alamat Modbus yang dicoba
   ```

2. Upload ke ESP32 sebagai `main.py` (Thonny atau `mpremote`), lalu reset board.
3. Lihat log di Shell. Kalau berhasil, muncul "WiFi OK" dan "Inverter menjawab di alamat ...".

Dashboard akan menunjukkan "ESP32 online" begitu data pertama masuk.

## Dashboard

- **Kartu data live:** panel surya (voltase, ampere, watt, energi hari ini), baterai (SOC, voltase, watt charge/discharge), beban, PLN, suhu, dan kode gangguan.
- **Grafik riwayat:** watt panel, watt beban, watt baterai, dan SOC. Pilihan 30 menit sampai 24 jam. Grafik memakai Chart.js dari CDN, jadi laptop perlu internet.
- **Pengaturan inverter:** ubah setingan lalu tekan **Terapkan**. Setingan bertanda "hati-hati" meminta konfirmasi dulu.
- **Log perubahan:** nilai lama, nilai baru, dan hasilnya (ok, gagal, timeout).

### Setingan yang bisa diubah

| Setingan | Register |
|---|---|
| Prioritas sumber output (SBU / SOL / UTI / SUB) | 1 |
| Sumber charge baterai | 2 |
| Jam mulai dan selesai output PLN | 3, 4 |
| Jam mulai dan selesai charge dari PLN | 5, 6 |
| Buzzer | 22 |
| Ampere charge maksimum (10-100 A) | 34 |
| Voltase bulk dan float (48,0-58,4 V) | 35, 36 |

Setingan lain (tipe baterai, alamat Modbus, reset, firmware, dan sejenisnya) sengaja **tidak** bisa diubah dari dashboard. Yang lain ditampilkan hanya-baca.

## Pengaman

- Hanya register di daftar putih yang boleh ditulis, dan dicek di dua tempat (server dan ESP32).
- Rentang nilai divalidasi. Voltase bulk tidak boleh lebih kecil dari float.
- Satu perintah menunggu per register.
- Setelah menulis, ESP32 membaca ulang nilainya. Kalau tidak sama, hasilnya "gagal".
- Setiap perubahan dicatat di tabel `audit` (SQLite).
- Jeda minimal 900 ms antar perintah Modbus, sesuai dokumen Growatt (minimal 850 ms).

## Catatan model: SPF 5000 vs SPF 6000 ES Plus

Bacaan data (input register) sama untuk keduanya. Setingan (holding register) ada perbedaan:

- **Tipe baterai (reg 39):** kodenya beda, jadi ditampilkan sebagai angka mentah dengan keterangan kedua versi.
- **Voltase bulk dan float** hanya berlaku bila tipe baterai **User**.
- **Ampere charge maksimum** tidak bisa diubah bila baterai **lithium**, karena dipegang BMS.
- **Reg 37 (pindah baterai ke PLN):** satuannya ikut tipe baterai (persen untuk lithium, voltase untuk lead acid), jadi hanya ditampilkan, tidak bisa diubah.
- **Jam PLN (reg 3-6)** belum terkonfirmasi di 6000 ES Plus. Kalau ditolak, alasannya muncul di log.
- Alamat Modbus bisa 1 atau 11 (repo referensi 6000 ES Plus memakai 11).

## API server

| Endpoint | Fungsi |
|---|---|
| `POST /api/ingest` | Dipanggil ESP32: kirim data, terima perintah |
| `GET /api/latest` | Data live, status koneksi, dan setingan |
| `GET /api/history?minutes=60` | Riwayat untuk grafik |
| `POST /api/settings` | Antre perintah ubah setingan `{reg, value}` |
| `GET /api/commands` | Perintah berjalan dan log perubahan |
| `GET /` | Dashboard |

Data disimpan di `server/spf.db` (SQLite), dibuat otomatis.

## Troubleshooting

| Gejala | Yang dicek |
|---|---|
| Dashboard "belum ada data dari ESP32" | `SERVER_URL` dan IP laptop, firewall port 8000, WiFi sama |
| "ESP32 online tapi inverter tidak menjawab" | Tukar A dan B, DE dan RE disambung jadi satu ke P4, VCC ke 5V, kabel di port RS485, alamat Modbus |
| `timeout` terus | Sama seperti di atas, ditambah baud rate (9600) |
| `CRC salah` | Kabel panjang atau noise, pembagi voltase RO, GND bersama |
| Setingan kembali ke nilai lama | Dongle ShineWiFi menimpa, atau tidak berlaku untuk tipe baterai tersebut |
| Perintah status `timeout` | ESP32 offline atau lambat, ulangi |

## Batasan

- Belum ada login. Pakai hanya di jaringan lokal tepercaya.
- Server menyimpan data di memori dan SQLite lokal, tanpa backup.
- Hanya satu inverter dan satu ESP32.
- Kode server belum dites otomatis (hanya cek sintaks dan uji logika CRC).

## Referensi

- Repo konfigurasi SPF 6000 ES Plus: https://github.com/vipmenable/Growatt-SPF-6000-ES-plus (berisi `modbus.yaml`, PDF protokol OffGrid V0.14, dan gambar pinout RS485)
- Protokol Growatt OffGrid SPF5000 Modbus RS485 RTU V0.11: https://watts247.com/manuals/gw/GrowattModBusProtocol.pdf
- Integrasi Home Assistant Growatt Modbus (referensi setingan SPF ES PLUS): https://0xaha.github.io/Growatt_ModbusTCP/
