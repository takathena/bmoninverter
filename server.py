"""Server prototype dashboard inverter Growatt SPF.

Jalankan:
    pip install -r requirements.txt
    python server.py
Buka: http://localhost:8000   (ESP32 kirim ke http://<IP-laptop>:8000/api/ingest)
"""
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

BASE = Path(__file__).parent
DB_PATH = BASE / "spf.db"
OFFLINE_AFTER_S = 15       # dianggap offline kalau tak ada kiriman selama ini
CMD_TIMEOUT_S = 40         # perintah tanpa hasil selama ini -> timeout

# ---------------------------------------------------------------- skema setingan
# reg = nomor holding register. scale = pengali nilai tampil -> mentah (mentah = tampil / scale)
SETTINGS: List[Dict[str, Any]] = [
    {"reg": 1, "key": "output_cfg", "label": "Prioritas sumber output", "type": "enum", "writable": True,
     "options": {0: "Baterai dulu", 1: "Panel dulu", 2: "PLN dulu"}},
    {"reg": 2, "key": "charge_cfg", "label": "Sumber charge baterai", "type": "enum", "writable": True,
     "options": {0: "Panel dulu", 1: "Panel + PLN", 2: "Panel saja"}},
    {"reg": 3, "key": "uti_out_start", "label": "Jam mulai output PLN", "type": "number", "writable": True,
     "min": 0, "max": 23, "scale": 1, "unit": "jam"},
    {"reg": 4, "key": "uti_out_end", "label": "Jam selesai output PLN", "type": "number", "writable": True,
     "min": 0, "max": 23, "scale": 1, "unit": "jam"},
    {"reg": 5, "key": "uti_chg_start", "label": "Jam mulai charge dari PLN", "type": "number", "writable": True,
     "min": 0, "max": 23, "scale": 1, "unit": "jam"},
    {"reg": 6, "key": "uti_chg_end", "label": "Jam selesai charge dari PLN", "type": "number", "writable": True,
     "min": 0, "max": 23, "scale": 1, "unit": "jam"},
    {"reg": 22, "key": "buzzer", "label": "Buzzer", "type": "enum", "writable": True,
     "options": {0: "Mati", 1: "Nyala"}},
    {"reg": 34, "key": "max_chg_a", "label": "Ampere charge maksimum", "type": "number", "writable": True,
     "min": 10, "max": 130, "scale": 1, "unit": "A", "danger": True},
    {"reg": 35, "key": "bulk_v", "label": "Voltase bulk charge", "type": "number", "writable": True,
     "min": 50.0, "max": 58.0, "scale": 0.1, "unit": "V", "danger": True},
    {"reg": 36, "key": "float_v", "label": "Voltase float charge", "type": "number", "writable": True,
     "min": 50.0, "max": 56.0, "scale": 0.1, "unit": "V", "danger": True},
    {"reg": 37, "key": "bat_low_v", "label": "Voltase baterai rendah (pindah ke PLN)", "type": "number",
     "writable": True, "min": 44.4, "max": 51.4, "scale": 0.1, "unit": "V", "danger": True},
    # --- hanya tampil (tidak bisa diubah dari dashboard)
    {"reg": 7, "key": "pv_mode", "label": "Mode input panel", "type": "enum", "writable": False,
     "options": {0: "Independent", 1: "Parallel"}},
    {"reg": 8, "key": "ac_mode", "label": "Mode input AC", "type": "enum", "writable": False,
     "options": {0: "APL (90-280 V)", 1: "UPS (170-280 V)"}},
    {"reg": 18, "key": "out_volt_type", "label": "Voltase output", "type": "enum", "writable": False,
     "options": {0: "208 V", 1: "230 V", 2: "240 V"}},
    {"reg": 19, "key": "out_freq_type", "label": "Frekuensi output", "type": "enum", "writable": False,
     "options": {0: "50 Hz", 1: "60 Hz"}},
    {"reg": 20, "key": "overload_restart", "label": "Restart saat overload", "type": "enum", "writable": False,
     "options": {0: "Ya", 1: "Tidak", 2: "Pindah ke PLN"}},
    {"reg": 21, "key": "overtemp_restart", "label": "Restart saat overtemp", "type": "enum", "writable": False,
     "options": {0: "Ya", 1: "Tidak"}},
    {"reg": 38, "key": "float_chg_a", "label": "Ampere float charge", "type": "number", "writable": False,
     "scale": 0.1, "unit": "A"},
    {"reg": 39, "key": "bat_type", "label": "Tipe baterai", "type": "enum", "writable": False,
     "options": {0: "Lead acid", 1: "Lithium", 2: "Custom lead acid"}},
]
SETTINGS_BY_REG = {s["reg"]: s for s in SETTINGS}

# ---------------------------------------------------------------- state
lock = threading.Lock()
state: Dict[str, Any] = {"live": None, "holding": {}, "last_seen": 0.0, "slave": None,
                         "ok": False, "error": None, "device": None}
commands: Dict[int, Dict[str, Any]] = {}
next_cmd_id = 1

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS readings (ts INTEGER, pv_w REAL, load_w REAL, bat_v REAL, "
           "soc REAL, bat_w REAL, grid_v REAL, inv_temp REAL)")
db.execute("CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts)")
db.execute("CREATE TABLE IF NOT EXISTS audit (ts INTEGER, reg INTEGER, label TEXT, old TEXT, new TEXT, "
           "status TEXT, detail TEXT)")
db.commit()


def audit(reg: int, label: str, old: Any, new: Any, status: str, detail: str = "") -> None:
    with lock:
        db.execute("INSERT INTO audit VALUES (?,?,?,?,?,?,?)",
                   (int(time.time()), reg, label, str(old), str(new), status, detail))
        db.commit()


# ---------------------------------------------------------------- app
app = FastAPI(title="SPF Gateway")


class Ingest(BaseModel):
    device: Optional[str] = None
    slave: Optional[int] = None
    ok: bool = False
    error: Optional[str] = None
    live: Optional[Dict[str, Any]] = None
    holding: Optional[Dict[str, int]] = None
    results: List[Dict[str, Any]] = []


class SettingIn(BaseModel):
    reg: int
    value: float          # nilai dalam satuan tampil (mis. 56.4 untuk voltase)


def fmt_value(s: Dict[str, Any], raw: Optional[int]) -> Any:
    if raw is None:
        return None
    if s["type"] == "enum":
        return s["options"].get(raw, f"? ({raw})")
    v = raw * s.get("scale", 1)
    return round(v, 1) if s.get("scale", 1) != 1 else int(v)


@app.post("/api/ingest")
def ingest(p: Ingest):
    now = time.time()
    out: List[Dict[str, Any]] = []
    with lock:
        state.update(last_seen=now, slave=p.slave, ok=p.ok, error=p.error, device=p.device)
        if p.live:
            state["live"] = p.live
        if p.holding:
            state["holding"] = {int(k): v for k, v in p.holding.items()}

        # hasil perintah dari ESP32
        for r in p.results:
            c = commands.get(r.get("id"))
            if not c:
                continue
            c["status"] = "ok" if r.get("ok") else "gagal"
            c["detail"] = r.get("error") or f"inverter membalas {r.get('readback')}"
            c["done_at"] = now
            s = SETTINGS_BY_REG.get(c["reg"], {})
            db.execute("INSERT INTO audit VALUES (?,?,?,?,?,?,?)",
                       (int(now), c["reg"], s.get("label", ""), fmt_value(s, c.get("old_raw")),
                        fmt_value(s, c["value"]), c["status"], c["detail"]))
            db.commit()

        # simpan histori
        if p.ok and p.live:
            L = p.live
            db.execute("INSERT INTO readings VALUES (?,?,?,?,?,?,?,?)",
                       (int(now), L.get("pv1_w", 0) + L.get("pv2_w", 0), L.get("out_w"), L.get("bat_v"),
                        L.get("soc"), L.get("bat_w"), L.get("grid_v"), L.get("inv_temp")))
            db.commit()

        # perintah yang menunggu -> kirim ke ESP32 (sekali saja)
        for c in commands.values():
            if c["status"] == "menunggu":
                c["status"] = "dikirim"
                c["sent_at"] = now
                out.append({"id": c["id"], "reg": c["reg"], "value": c["value"]})
            elif c["status"] == "dikirim" and now - c["sent_at"] > CMD_TIMEOUT_S:
                c["status"] = "timeout"
                c["detail"] = "tidak ada hasil dari ESP32"
    return {"commands": out}


def decode_fw(h: Dict[int, int], start: int) -> str:
    try:
        chars = []
        for r in range(start, start + 3):
            v = h[r]
            chars += [chr(v >> 8), chr(v & 0xFF)]
        return "".join(c for c in chars if 32 < ord(c) < 127)
    except KeyError:
        return ""


@app.get("/api/latest")
def latest():
    with lock:
        age = time.time() - state["last_seen"] if state["last_seen"] else None
        h = state["holding"]
        settings = []
        for s in SETTINGS:
            raw = h.get(s["reg"])
            settings.append({**s, "raw": raw, "display": fmt_value(s, raw)})
        return {
            "online": age is not None and age < OFFLINE_AFTER_S,
            "age_s": round(age, 1) if age is not None else None,
            "inverter_ok": state["ok"], "error": state["error"], "slave": state["slave"],
            "live": state["live"], "settings": settings,
            "fw": decode_fw(h, 9), "fw_ctrl": decode_fw(h, 12),
        }


@app.get("/api/history")
def history(minutes: int = Query(60, ge=1, le=1440)):
    since = int(time.time()) - minutes * 60
    with lock:
        rows = db.execute("SELECT ts,pv_w,load_w,bat_v,soc,bat_w,grid_v,inv_temp FROM readings "
                          "WHERE ts>=? ORDER BY ts", (since,)).fetchall()
    step = max(1, len(rows) // 600)
    rows = rows[::step]
    keys = ["ts", "pv_w", "load_w", "bat_v", "soc", "bat_w", "grid_v", "inv_temp"]
    return [dict(zip(keys, r)) for r in rows]


@app.post("/api/settings")
def change_setting(body: SettingIn):
    global next_cmd_id
    s = SETTINGS_BY_REG.get(body.reg)
    if not s or not s["writable"]:
        raise HTTPException(400, "register ini tidak boleh diubah dari dashboard")

    if s["type"] == "enum":
        raw = int(body.value)
        if raw not in s["options"]:
            raise HTTPException(400, "pilihan tidak valid")
    else:
        if not s["min"] <= body.value <= s["max"]:
            raise HTTPException(400, f"nilai harus {s['min']} - {s['max']} {s['unit']}")
        raw = int(round(body.value / s["scale"]))

    with lock:
        h = state["holding"]
        if not state["last_seen"] or time.time() - state["last_seen"] > OFFLINE_AFTER_S:
            raise HTTPException(409, "ESP32 sedang offline")
        # aturan silang: bulk >= float
        if body.reg == 35 and 36 in h and raw < h[36]:
            raise HTTPException(400, "voltase bulk tidak boleh lebih kecil dari voltase float")
        if body.reg == 36 and 35 in h and raw > h[35]:
            raise HTTPException(400, "voltase float tidak boleh lebih besar dari voltase bulk")
        # satu perintah menunggu per register
        for c in commands.values():
            if c["reg"] == body.reg and c["status"] in ("menunggu", "dikirim"):
                raise HTTPException(409, "masih ada perintah untuk register ini")
        cid = next_cmd_id
        next_cmd_id += 1
        commands[cid] = {"id": cid, "reg": body.reg, "label": s["label"], "value": raw,
                         "old_raw": h.get(body.reg), "status": "menunggu", "detail": "",
                         "created_at": time.time(),
                         "shown": fmt_value(s, raw), "old_shown": fmt_value(s, h.get(body.reg))}
    return {"id": cid, "status": "menunggu"}


@app.get("/api/commands")
def list_commands():
    with lock:
        cur = sorted(commands.values(), key=lambda c: -c["id"])[:15]
        hist = db.execute("SELECT ts,label,old,new,status,detail FROM audit ORDER BY ts DESC LIMIT 30").fetchall()
    return {"current": cur,
            "audit": [dict(zip(["ts", "label", "old", "new", "status", "detail"], r)) for r in hist]}


@app.get("/", response_class=HTMLResponse)
def index():
    return (BASE / "dashboard.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
