# ESP32 (MicroPython) v1.4 (Multi-Inverter)
import network, time, json, machine, gc
from machine import UART, Pin

try:
    import urequests as requests
except ImportError:
    import requests

# ================== ISI BAGIAN INI ==================
WIFI_SSID = "NAMA_WIFI"
WIFI_PASS = "PASSWORD_WIFI"
SERVER_URL = "http://192.168.1.10:8000/api/ingest" 
API_KEY = "KunciRahasiaESP32Anda"
DEVICE_ID = "esp32_rumah"  # <--- BERI NAMA UNIK UNTUK SETIAP ESP32
SLAVE_CANDIDATES = [1, 11]
POLL_MS = 3000
# =====================================================

BAUD = 9600
MIN_GAP_MS = 900
HOLDING_EVERY = 5
HTTP_TIMEOUT = 5

WRITE_ALLOWED = {
    1: (0, 2), 2: (0, 2), 3: (0, 23), 4: (0, 23), 5: (0, 23), 6: (0, 23),
    22: (0, 1), 34: (10, 130), 35: (500, 580), 36: (500, 560), 37: (444, 514),
}

uart = UART(2, baudrate=BAUD, tx=17, rx=16, bits=8, parity=None, stop=1)
de_re = Pin(4, Pin.OUT, value=0)
slave_id = SLAVE_CANDIDATES[0]
_last_cmd = time.ticks_ms()
wdt = machine.WDT(timeout=60000)
offline_buffer = []

def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8): crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc

def _txrx(req, resp_len, timeout_ms=700):
    global _last_cmd
    wait = MIN_GAP_MS - time.ticks_diff(time.ticks_ms(), _last_cmd)
    if wait > 0: time.sleep_ms(wait)
    while uart.any(): uart.read()
    frame = req + crc16(req).to_bytes(2, "little")
    de_re.value(1)
    uart.write(frame)
    try: uart.flush()
    except AttributeError: time.sleep_ms(len(frame) * 2 + 5)
    time.sleep_ms(2)
    de_re.value(0)
    buf = b""
    t0 = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t0) < timeout_ms:
        if uart.any():
            buf += uart.read() or b""
            if len(buf) >= resp_len: break
            if len(buf) >= 5 and buf[1] & 0x80: break
        time.sleep_ms(5)
    _last_cmd = time.ticks_ms()
    if len(buf) < 5: raise OSError("timeout")
    if crc16(buf[:-2]) != int.from_bytes(buf[-2:], "little"): raise OSError("CRC salah")
    if buf[1] & 0x80: raise OSError("exception modbus %d" % buf[2])
    return buf

def read_regs(fc, addr, count):
    req = bytes([slave_id, fc, addr >> 8, addr & 0xFF, count >> 8, count & 0xFF])
    buf = _txrx(req, 5 + 2 * count)
    return [(buf[3 + 2 * i] << 8) | buf[4 + 2 * i] for i in range(count)]

def read_retry(fc, addr, count, tries=2):
    for i in range(tries):
        try: return read_regs(fc, addr, count)
        except OSError:
            if i == tries - 1: raise

def write_reg(addr, val):
    req = bytes([slave_id, 6, addr >> 8, addr & 0xFF, val >> 8, val & 0xFF])
    _txrx(req, 8)

def s16(v): return v - 65536 if v >= 32768 else v
def u32(h, l): return (h << 16) | l
def s32(h, l):
    v = u32(h, l)
    return v - (1 << 32) if v & 0x80000000 else v

def read_input_all():
    ir = read_retry(4, 0, 45)
    try: ir += read_retry(4, 45, 45)
    except OSError: ir += [0] * 45
    return ir

def decode(g):
    r = lambda x, d=1: round(x, d)
    return {
        "status": g[0], "pv1_v": r(g[1]*0.1), "pv2_v": r(g[2]*0.1), "pv1_w": r(u32(g[3],g[4])*0.1),
        "pv2_w": r(u32(g[5],g[6])*0.1), "pv1_a": r(g[7]*0.1), "pv2_a": r(g[8]*0.1),
        "out_w": r(u32(g[9],g[10])*0.1), "out_va": r(u32(g[11],g[12])*0.1), "acchr_w": r(u32(g[13],g[14])*0.1),
        "bat_v": r(g[17]*0.01, 2), "soc": g[18], "bus_v": r(g[19]*0.1), "grid_v": r(g[20]*0.1),
        "grid_hz": r(g[21]*0.01, 2), "out_v": r(g[22]*0.1), "out_hz": r(g[23]*0.01, 2),
        "inv_temp": r(g[25]*0.1), "dcdc_temp": r(g[26]*0.1), "load_pct": r(g[27]*0.1),
        "buck1_temp": r(g[32]*0.1), "buck2_temp": r(g[33]*0.1), "out_a": r(s16(g[34])*0.1),
        "inv_a": r(s16(g[35])*0.1), "acin_w": r(s32(g[36],g[37])*0.1), "acin_va": r(u32(g[38],g[39])*0.1),
        "fault": g[40], "warning": g[41], "fault_val": g[42], "warning_val": g[43],
        "epv1_today": r(u32(g[48],g[49])*0.1), "epv1_total": r(u32(g[50],g[51])*0.1),
        "epv2_today": r(u32(g[52],g[53])*0.1), "epv2_total": r(u32(g[54],g[55])*0.1),
        "acchr_kwh_today": r(u32(g[56],g[57])*0.1), "bat_dis_kwh_today": r(u32(g[60],g[61])*0.1),
        "acdis_kwh_today": r(u32(g[64],g[65])*0.1), "acchr_a": r(g[68]*0.1),
        "bat_w": r(s32(g[77],g[78])*0.1), "overcharge": g[80], "mppt_fan": g[81], "inv_fan": g[82],
    }

def wifi_connect():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print("Konek WiFi...")
        wlan.connect(WIFI_SSID, WIFI_PASS)
        t0 = time.ticks_ms()
        while not wlan.isconnected():
            if time.ticks_diff(time.ticks_ms(), t0) > 15000:
                print("WiFi gagal tersambung."); return wlan
            time.sleep_ms(500); wdt.feed()
    print("WiFi OK:", wlan.ifconfig()[0])
    return wlan

def detect_slave():
    global slave_id
    for sid in SLAVE_CANDIDATES:
        slave_id = sid
        try:
            read_regs(4, 0, 1)
            print("Inverter menjawab di alamat", sid); return True
        except OSError as e: print("Alamat", sid, "gagal:", e)
    slave_id = SLAVE_CANDIDATES[0]
    return False

def post(payload):
    headers = {"Content-Type": "application/json", "X-API-Key": API_KEY}
    r = requests.post(SERVER_URL, data=json.dumps(payload), headers=headers, timeout=HTTP_TIMEOUT)
    try: return r.json()
    finally: r.close()

def run_cmd(c):
    res = {"id": c.get("id"), "reg": c.get("reg"), "value": c.get("value"), "ok": False, "readback": None, "error": None}
    try:
        reg, val = int(c["reg"]), int(c["value"])
        if reg not in WRITE_ALLOWED: raise ValueError("register tidak diizinkan")
        lo, hi = WRITE_ALLOWED[reg]
        if not lo <= val <= hi: raise ValueError("nilai di luar rentang %d-%d" % (lo, hi))
        
        write_reg(reg, val)
        time.sleep_ms(1000)
        
        success = False
        for attempt in range(3):
            rb = read_retry(3, reg, 1)[0]
            res["readback"] = rb
            if rb == val: success = True; break
            print(f"  Readback mismatch (Coba {attempt+1}/3): minta {val}, dapat {rb}")
            time.sleep_ms(500)
            
        res["ok"] = success
        if not res["ok"]: res["error"] = "inverter tidak menerima setelah 3x coba (nilai kembali %d)" % rb
    except Exception as e:
        res["error"] = str(e)
    print("Perintah:", res)
    return res

def main():
    global offline_buffer
    wlan = wifi_connect()
    found = detect_slave()
    results = []
    n = 0
    force_hr = True

    while True:
        wdt.feed()
        gc.collect() 
        if not wlan.isconnected():
            print("WiFi terputus! Mencoba menyambung ulang...")
            wlan = wifi_connect()
            if not wlan.isconnected(): time.sleep(5); continue

        t0 = time.ticks_ms()
        # Tambahkan DEVICE_ID ke payload
        payload = {"device": DEVICE_ID, "slave": slave_id, "ok": False}
        try:
            payload["live"] = decode(read_input_all()); payload["ok"] = True
        except OSError as e:
            payload["error"] = str(e)
            if not found: found = detect_slave()

        if payload["ok"] and (force_hr or n % HOLDING_EVERY == 0):
            try:
                hr = read_retry(3, 0, 45)
                payload["holding"] = {str(i): v for i, v in enumerate(hr)}
                force_hr = False
            except OSError as e: print("Gagal baca holding:", e)

        payload["results"] = results

        try:
            resp = post(payload)
            results = [] 
            while offline_buffer:
                old_payload = offline_buffer.pop(0)
                try: post(old_payload); print("Berhasil mengirim data offline tertunda.")
                except: offline_buffer.insert(0, old_payload); break
            
            for c in resp.get("commands", []):
                results.append(run_cmd(c))
                force_hr = True
        except Exception as e:
            print("Gagal kirim ke server:", e)
            if len(offline_buffer) < 10: offline_buffer.append(payload)

        n += 1
        left = POLL_MS - time.ticks_diff(time.ticks_ms(), t0)
        if left > 0: time.sleep_ms(left)

main()
