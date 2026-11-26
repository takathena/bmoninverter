import json, sqlite3, threading, time, secrets, csv, io, urllib.request, urllib.parse, os
from pathlib import Path
from typing import Any, Dict, List, Optional
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Depends, Header, Request, Response, Form
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

BASE = Path(__file__).parent
DB_PATH = BASE / "data" / "spf.db"
DB_PATH.parent.mkdir(exist_ok=True)

# ================= MEMBACA DARI .ENV =================
OFFLINE_AFTER_S = int(os.getenv("OFFLINE_AFTER_S", 15))
CMD_TIMEOUT_S = int(os.getenv("CMD_TIMEOUT_S", 40))
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "growatt123")
API_KEY = os.getenv("API_KEY", "KunciRahasiaESP32Anda")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
AUTO_SOC_ENABLED = os.getenv("AUTO_SOC_ENABLED", "True").lower() in ("true", "1", "t")
SOC_LOW_THRESHOLD = int(os.getenv("SOC_LOW_THRESHOLD", 30))
SOC_HIGH_THRESHOLD = int(os.getenv("SOC_HIGH_THRESHOLD", 85))
# =====================================================

# State untuk sesi login
active_sessions = set()
last_alert_time = 0

def verify_session(token: Optional[str]):
    if not token or token not in active_sessions:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return token

def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY: raise HTTPException(status_code=401, detail="API Key invalid")
    return x_api_key

def send_telegram(msg: str):
    global last_alert_time
    if time.time() - last_alert_time < 300 or not TELEGRAM_TOKEN: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        urllib.request.urlopen(urllib.request.Request(url, data=urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": msg}).encode()), timeout=5)
        last_alert_time = time.time()
    except Exception as e: print("Telegram error:", e)

SETTINGS: List[Dict[str, Any]] = [
    {"reg": 1, "key": "output_cfg", "label": "Prioritas Output", "type": "enum", "writable": True, "options": {0: "Baterai", 1: "Panel", 2: "PLN"}},
    {"reg": 2, "key": "charge_cfg", "label": "Sumber Charge", "type": "enum", "writable": True, "options": {0: "Panel", 1: "Panel + PLN", 2: "Panel Saja"}},
    {"reg": 3, "key": "uti_out_start", "label": "Jam Mulai PLN", "type": "number", "writable": True, "min": 0, "max": 23, "scale": 1, "unit": "Jam"},
    {"reg": 4, "key": "uti_out_end", "label": "Jam Selesai PLN", "type": "number", "writable": True, "min": 0, "max": 23, "scale": 1, "unit": "Jam"},
    {"reg": 34, "key": "max_chg_a", "label": "Arus Charge Maks", "type": "number", "writable": True, "min": 10, "max": 130, "scale": 1, "unit": "A", "danger": True},
    {"reg": 35, "key": "bulk_v", "label": "Tegangan Bulk", "type": "number", "writable": True, "min": 50.0, "max": 58.0, "scale": 0.1, "unit": "V", "danger": True},
    {"reg": 36, "key": "float_v", "label": "Tegangan Float", "type": "number", "writable": True, "min": 50.0, "max": 56.0, "scale": 0.1, "unit": "V", "danger": True},
]
SETTINGS_BY_REG = {s["reg"]: s for s in SETTINGS}

lock = threading.Lock()
states: Dict[str, Dict[str, Any]] = {}
commands: Dict[int, Dict[str, Any]] = {}
next_cmd_id = 1

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS readings (device_id TEXT, ts INTEGER, pv_w REAL, load_w REAL, bat_v REAL, soc REAL, bat_w REAL, grid_v REAL, inv_temp REAL)")
db.execute("CREATE TABLE IF NOT EXISTS audit (device_id TEXT, ts INTEGER, reg INTEGER, label TEXT, old TEXT, new TEXT, status TEXT, detail TEXT)")
db.commit()

def audit(dev_id: str, reg: int, label: str, old: Any, new: Any, status: str, detail: str = ""):
    with lock:
        db.execute("INSERT INTO audit VALUES (?,?,?,?,?,?,?,?)", (dev_id, int(time.time()), reg, label, str(old), str(new), status, detail))
        db.commit()

app = FastAPI(title="SPF Gateway v1.5")

class Ingest(BaseModel):
    device: str
    slave: Optional[int] = None
    ok: bool = False
    error: Optional[str] = None
    live: Optional[Dict[str, Any]] = None
    holding: Optional[Dict[str, int]] = None
    results: List[Dict[str, Any]] = []

class SettingIn(BaseModel):
    device_id: str
    reg: int
    value: float

def fmt_value(s, raw):
    if raw is None: return None
    if s["type"] == "enum": return s["options"].get(raw, f"? ({raw})")
    v = raw * s.get("scale", 1)
    return round(v, 1) if s.get("scale", 1) != 1 else int(v)

def process_automation(dev_id: str, L: dict, h: dict):
    if not AUTO_SOC_ENABLED: return
    soc, current_cfg = L.get("soc"), h.get(2)
    if soc is None or current_cfg is None: return
    for c in commands.values():
        if c["device_id"] == dev_id and c["reg"] == 2 and c["status"] in ("menunggu", "dikirim"): return

    global next_cmd_id
    target = None
    if soc < SOC_LOW_THRESHOLD and current_cfg != 1:
        target = 1
        send_telegram(f"[{dev_id}] Baterai rendah ({soc}%). Mode charge -> PV + PLN.")
    elif soc > SOC_HIGH_THRESHOLD and current_cfg != 2:
        target = 2
        send_telegram(f"[{dev_id}] Baterai penuh ({soc}%). Mode charge -> PV Saja.")

    if target is not None:
        cid = next_cmd_id; next_cmd_id += 1
        s = SETTINGS_BY_REG[2]
        commands[cid] = {"id": cid, "device_id": dev_id, "reg": 2, "label": s["label"], "value": target, "old_raw": current_cfg, "status": "menunggu", "detail": "Auto-SOC", "created_at": time.time(), "shown": fmt_value(s, target), "old_shown": fmt_value(s, current_cfg)}
        audit(dev_id, 2, s["label"], fmt_value(s, current_cfg), fmt_value(s, target), "otomatis", "Trigger SOC")

# --- AUTENTIKASI & HALAMAN ---

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: int = 0):
    return (BASE / "login.html").read_text(encoding="utf-8").replace("{{error}}", "block" if error else "none")

@app.post("/api/login")
def do_login(response: Response, username: str = Form(...), password: str = Form(...)):
    if username == DASHBOARD_USER and password == DASHBOARD_PASS:
        token = secrets.token_hex(16)
        active_sessions.add(token)
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(key="session_token", value=token, httponly=True, samesite="lax")
        return response
    return RedirectResponse(url="/login?error=1", status_code=303)

@app.get("/api/logout")
def logout(response: Response, session_token: Optional[str] = None):
    if session_token and session_token in active_sessions:
        active_sessions.remove(session_token)
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session_token")
    return response

@app.get("/", response_class=HTMLResponse)
def index(request: Request, session_token: Optional[str] = None):
    if not session_token or session_token not in active_sessions:
        return RedirectResponse(url="/login")
    return (BASE / "dashboard.html").read_text(encoding="utf-8")

# --- API ENDPOINTS ---

@app.post("/api/ingest", dependencies=[Depends(verify_api_key)])
def ingest(p: Ingest):
    now = time.time()
    out = []
    dev_id = p.device

    with lock:
        if dev_id not in states: states[dev_id] = {"live": None, "holding": {}, "last_seen": 0.0, "slave": None, "ok": False, "error": None}
        states[dev_id].update(last_seen=now, slave=p.slave, ok=p.ok, error=p.error)
        if p.live: states[dev_id]["live"] = p.live
        if p.holding: states[dev_id]["holding"] = {int(k): v for k, v in p.holding.items()}

        for r in p.results:
            c = commands.get(r.get("id"))
            if not c or c["device_id"] != dev_id: continue
            c["status"] = "ok" if r.get("ok") else "gagal"
            c["detail"] = r.get("error") or f"Balasan: {r.get('readback')}"
            s = SETTINGS_BY_REG.get(c["reg"], {})
            audit(dev_id, c["reg"], s.get("label", ""), fmt_value(s, c.get("old_raw")), fmt_value(s, c["value"]), c["status"], c["detail"])

        if p.ok and p.live:
            L = p.live
            db.execute("INSERT INTO readings VALUES (?,?,?,?,?,?,?,?,?)", (dev_id, int(now), L.get("pv1_w",0)+L.get("pv2_w",0), L.get("out_w"), L.get("bat_v"), L.get("soc"), L.get("bat_w"), L.get("grid_v"), L.get("inv_temp")))
            db.commit()
            if L.get("fault", 0) > 0: send_telegram(f"[{dev_id}] FAULT! Kode: {L.get('fault')}")
            process_automation(dev_id, L, states[dev_id]["holding"])

        for c in commands.values():
            if c["device_id"] != dev_id: continue
            if c["status"] == "menunggu":
                c["status"] = "dikirim"; c["sent_at"] = now
                out.append({"id": c["id"], "reg": c["reg"], "value": c["value"]})
            elif c["status"] == "dikirim" and now - c["sent_at"] > CMD_TIMEOUT_S:
                c["status"] = "timeout"; c["detail"] = "Timeout"
    return {"commands": out}

@app.get("/api/devices", dependencies=[Depends(verify_session)])
def get_devices():
    with lock:
        return [{"id": k, "online": (time.time() - v["last_seen"]) < OFFLINE_AFTER_S if v["last_seen"] else False} for k, v in states.items()]

@app.get("/api/latest", dependencies=[Depends(verify_session)])
def latest(device_id: str):
    with lock:
        s = states.get(device_id, {})
        age = time.time() - s.get("last_seen", 0) if s.get("last_seen") else None
        h = s.get("holding", {})
        settings = [{**st, "raw": h.get(st["reg"]), "display": fmt_value(st, h.get(st["reg"]))} for st in SETTINGS]
        return {"online": age is not None and age < OFFLINE_AFTER_S, "age_s": round(age, 1) if age is not None else None, "inverter_ok": s.get("ok", False), "error": s.get("error"), "live": s.get("live"), "settings": settings, "auto_soc": AUTO_SOC_ENABLED}

@app.get("/api/history", dependencies=[Depends(verify_session)])
def history(device_id: str, minutes: int = Query(60, ge=1, le=1440)):
    since = int(time.time()) - minutes * 60
    with lock:
        rows = db.execute("SELECT ts,pv_w,load_w,bat_v,soc,bat_w,grid_v,inv_temp FROM readings WHERE device_id=? AND ts>=? ORDER BY ts", (device_id, since)).fetchall()
    step = max(1, len(rows) // 600)
    return [dict(zip(["ts", "pv_w", "load_w", "bat_v", "soc", "bat_w", "grid_v", "inv_temp"], r)) for r in rows[::step]]

@app.get("/api/export", dependencies=[Depends(verify_session)])
def export_csv(device_id: str, minutes: int = Query(1440, ge=1, le=43200)):
    since = int(time.time()) - minutes * 60
    with lock:
        rows = db.execute("SELECT ts,pv_w,load_w,bat_v,soc,bat_w,grid_v,inv_temp FROM readings WHERE device_id=? AND ts>=? ORDER BY ts", (device_id, since)).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Waktu", "PV (W)", "Beban (W)", "Baterai (V)", "SOC (%)", "Baterai (W)", "PLN (V)", "Suhu"])
    for r in rows: writer.writerow([time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r[0]))] + list(r[1:]))
    response = StreamingResponse(iter([output.getvalue()]), media_type="text/csv")
    response.headers["Content-Disposition"] = f"attachment; filename=histori_{device_id}.csv"
    return response

@app.post("/api/settings", dependencies=[Depends(verify_session)])
def change_setting(body: SettingIn):
    global next_cmd_id
    s = SETTINGS_BY_REG.get(body.reg)
    if not s or not s["writable"]: raise HTTPException(400, "Register tidak diizinkan")
    if s["type"] == "enum":
        raw = int(body.value)
        if raw not in s["options"]: raise HTTPException(400, "Pilihan invalid")
    else:
        if not s["min"] <= body.value <= s["max"]: raise HTTPException(400, "Di luar batas")
        raw = int(round(body.value / s["scale"]))

    with lock:
        s_state = states.get(body.device_id, {})
        if not s_state.get("last_seen") or time.time() - s_state["last_seen"] > OFFLINE_AFTER_S: raise HTTPException(409, "Device offline")
        for c in commands.values():
            if c["device_id"] == body.device_id and c["reg"] == body.reg and c["status"] in ("menunggu", "dikirim"): raise HTTPException(409, "Masih ada antrean")
        cid = next_cmd_id; next_cmd_id += 1
        commands[cid] = {"id": cid, "device_id": body.device_id, "reg": body.reg, "label": s["label"], "value": raw, "old_raw": s_state["holding"].get(body.reg), "status": "menunggu", "detail": "Manual", "created_at": time.time(), "shown": fmt_value(s, raw), "old_shown": fmt_value(s, s_state["holding"].get(body.reg))}
    return {"id": cid}

@app.get("/api/commands", dependencies=[Depends(verify_session)])
def list_commands(device_id: str):
    with lock:
        cur = sorted([c for c in commands.values() if c["device_id"] == device_id], key=lambda c: -c["id"])[:15]
        hist = db.execute("SELECT ts,label,old,new,status,detail FROM audit WHERE device_id=? ORDER BY ts DESC LIMIT 30", (device_id,)).fetchall()
    return {"current": cur, "audit": [dict(zip(["ts", "label", "old", "new", "status", "detail"], r)) for r in hist]}

@app.post("/api/automation", dependencies=[Depends(verify_session)])
def toggle_auto(enabled: bool = Query(...)):
    global AUTO_SOC_ENABLED
    AUTO_SOC_ENABLED = enabled
    return {"auto_soc": AUTO_SOC_ENABLED}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)