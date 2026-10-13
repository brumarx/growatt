#!/usr/bin/env python3
"""Solar — lê o inversor Growatt MIN 4200TL-XH via Modbus TCP (só leitura)
e serve uma página com produção, consumo e trocas com a rede.

Sem dependências externas: stdlib apenas.
"""
import json
import os
import socket
import sqlite3
import struct
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

INVERTER_IP = os.environ.get("SOLAR_INVERTER_IP", "192.168.1.69")
INVERTER_PORT = int(os.environ.get("SOLAR_INVERTER_PORT", "502"))
UNIT_ID = 1
POLL_SECONDS = int(os.environ.get("SOLAR_POLL_SECONDS", "60"))
HTTP_PORT = int(os.environ.get("SOLAR_HTTP_PORT", "8096"))
# EDP Eletricidade Verde Digital 20% (simples 6,9 kVA), desde Out 2026:
# 0,1337 energia + 0,0010 IEC + 0,0021 tarifa social, com IVA 23% (kWh acima dos 200/mês)
# Atenção: o desconto acaba em Out 2027 (passa a 0,1671 €/kWh s/IVA).
PRICE_KWH = float(os.environ.get("SOLAR_PRICE_KWH", "0.168"))  # € por kWh comprado, com IVA
PRICE_LABEL = os.environ.get("SOLAR_PRICE_LABEL", "EDP Digital 20%")
BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "solar.db")

_lock = threading.Lock()
_last = {"ok": False, "error": "ainda sem leitura", "ts": None}


# ---------------------------------------------------------------- Modbus
def read_input_registers(start, count):
    """Função 04 (input registers). Nunca escreve no inversor."""
    with socket.create_connection((INVERTER_IP, INVERTER_PORT), timeout=8) as s:
        s.sendall(struct.pack(">HHHBBHH", 1, 0, 6, UNIT_ID, 4, start, count))
        head = b""
        while len(head) < 9:
            chunk = s.recv(9 - len(head))
            if not chunk:
                raise IOError("ligação fechada")
            head += chunk
        if head[7] & 0x80:
            raise IOError("erro Modbus %d" % head[8])
        n = head[8]
        body = b""
        while len(body) < n:
            chunk = s.recv(n - len(body))
            if not chunk:
                raise IOError("resposta incompleta")
            body += chunk
    return list(struct.unpack(">%dH" % (n // 2), body))


def sample():
    a = read_input_registers(3000, 100)  # 3000..3099
    u32 = lambda i: ((a[i] << 16) | a[i + 1]) / 10.0
    s16 = lambda i: (a[i] - 65536 if a[i] > 32767 else a[i]) / 10.0
    return {
        "status": a[0],
        "ppv": u32(1),
        "pv1_v": a[3] / 10.0, "pv1_a": a[4] / 10.0, "pv1_w": u32(5),
        "pv2_v": a[7] / 10.0, "pv2_a": a[8] / 10.0, "pv2_w": u32(9),
        "pac": u32(23),
        "grid_v": a[26] / 10.0, "grid_hz": a[25] / 100.0,
        "p_import": u32(41), "p_export": u32(43), "p_load": u32(45),
        "e_pv_today": u32(49), "e_pv_total": u32(51),
        "e_import_today": u32(67), "e_import_total": u32(69),
        "e_export_today": u32(71), "e_export_total": u32(73),
        "e_load_today": u32(75), "e_load_total": u32(77),
        "temp": s16(93),
    }


# ---------------------------------------------------------------- DB
COLS = ["status", "ppv", "pv1_v", "pv1_a", "pv1_w", "pv2_v", "pv2_a", "pv2_w",
        "pac", "grid_v", "grid_hz", "p_import", "p_export", "p_load",
        "e_pv_today", "e_pv_total", "e_import_today", "e_import_total",
        "e_export_today", "e_export_total", "e_load_today", "e_load_total", "temp"]


def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS samples (ts TEXT PRIMARY KEY, %s)"
                    % ", ".join("%s REAL" % c for c in COLS))
        con.execute("CREATE INDEX IF NOT EXISTS idx_samples_day ON samples(substr(ts,1,10))")


def store(ts, d):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO samples (ts, %s) VALUES (?%s)"
                    % (", ".join(COLS), ", ?" * len(COLS)),
                    [ts] + [d[c] for c in COLS])


def sample_retry(tries=3):
    # o Wi-Fi do dongle é fraco: uma leitura falhada não deve perder o minuto
    for i in range(tries):
        try:
            return sample()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(5)


def load_last():
    """Ao arrancar, mostra a última leitura guardada em vez de uma página vazia."""
    global _last
    with db() as con:
        r = con.execute("SELECT * FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
    if r:
        _last = {"ok": False, "error": "a reconectar ao inversor", **dict(r)}


def poller():
    global _last
    while True:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            d = sample_retry()
            store(ts, d)
            with _lock:
                _last = {"ok": True, "ts": ts, **d}
        except Exception as e:  # inversor desligado à noite, Wi-Fi em baixo, etc.
            with _lock:
                _last = {**_last, "ok": False, "error": str(e), "err_ts": ts}
        time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------- API
def api_today(day):
    with db() as con:
        rows = con.execute(
            "SELECT substr(ts,12,5) t, ppv, p_load, p_import, p_export FROM samples "
            "WHERE substr(ts,1,10)=? ORDER BY ts", (day,)).fetchall()
    return [dict(r) for r in rows]


def api_days(n):
    since = (datetime.now() - timedelta(days=n - 1)).strftime("%Y-%m-%d")
    # os contadores "hoje" do inversor sobem ao longo do dia: o máximo do dia é o total do dia
    with db() as con:
        rows = con.execute(
            "SELECT substr(ts,1,10) day, max(e_pv_today) pv, max(e_load_today) load, "
            "max(e_import_today) imp, max(e_export_today) exp FROM samples "
            "WHERE substr(ts,1,10)>=? GROUP BY day ORDER BY day", (since,)).fetchall()
    return [dict(r) for r in rows]


def api_tip(days=7):
    """Janela de horas com mais excedente nos últimos dias, a partir das leituras reais."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with db() as con:
        rows = con.execute(
            "SELECT CAST(substr(ts,12,2) AS INTEGER) h, avg(p_export) exp, "
            "count(DISTINCT substr(ts,1,10)) nd FROM samples "
            "WHERE substr(ts,1,10)>=? GROUP BY h ORDER BY h", (since,)).fetchall()
        ndays = con.execute("SELECT count(DISTINCT substr(ts,1,10)) FROM samples "
                            "WHERE substr(ts,1,10)>=?", (since,)).fetchone()[0]
    by_hour = {r["h"]: r["exp"] or 0 for r in rows}
    # só conta como "aprendido" com pelo menos um dia completo de dados (manhã e tarde)
    if ndays < 2 or not by_hour or max(by_hour.values()) < 100:
        return {"learned": False, "start": 11, "end": 16, "days": ndays}
    peak = max(by_hour.values())
    good = [h for h, v in by_hour.items() if v >= peak * 0.5]
    # maior bloco contínuo de horas boas
    best, cur = [], []
    for h in range(24):
        if h in good:
            cur.append(h)
            if len(cur) > len(best):
                best = cur[:]
        else:
            cur = []
    avg_w = sum(by_hour[h] for h in best) / len(best)
    return {"learned": True, "start": best[0], "end": best[-1] + 1, "days": ndays,
            "avg_export_w": round(avg_w)}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        if path in ("/", "/index.html"):
            with open(os.path.join(BASE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/now":
            with _lock:
                return self._send(200, json.dumps({**_last, "price": PRICE_KWH, "price_label": PRICE_LABEL}), "application/json")
        if path == "/api/tip":
            return self._send(200, json.dumps(api_tip()), "application/json")
        if path == "/api/today":
            day = params.get("day", datetime.now().strftime("%Y-%m-%d"))[:10]
            return self._send(200, json.dumps(api_today(day)), "application/json")
        if path == "/api/days":
            n = max(1, min(int(params.get("n", "30")), 400))
            return self._send(200, json.dumps(api_days(n)), "application/json")
        self._send(404, "not found", "text/plain")

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    init_db()
    load_last()
    threading.Thread(target=poller, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()
