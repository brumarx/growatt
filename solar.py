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
import statistics
import threading
import time
from collections import deque
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
# Instalação: 8 painéis JA Solar JAM66S30-500/MR (2 linhas de 4), contador Growatt SPM-E
INSTALL_DATE = os.environ.get("SOLAR_INSTALL_DATE", "2026-04-06")
KWP = float(os.environ.get("SOLAR_KWP", "4.0"))
# Sistema alugado: renda mensal com IVA (0 = sistema próprio)
RENT_MONTH = float(os.environ.get("SOLAR_RENT_MONTH", "0"))
# Venda do excedente: indexado ao OMIE menos a comissão do comercializador.
# OMIE médio 2025 entre as 9h e as 18h ≈ 0,035 €/kWh, menos 25% ≈ 0,026 €/kWh.
EXPORT_PRICE = float(os.environ.get("SOLAR_EXPORT_PRICE", "0.03"))  # € por kWh vendido
# Baterias compatíveis com o MIN 4200TL-XH (ARK XH ou APX): custo estimado com instalação, em €.
# Capacidade útil ≈ 90% da nominal; potência de carga/descarga limitada pelo inversor/BMS.
BATTERIES = [
    {"name": "5 kWh", "usable": 4.5, "max_w": 2500,
     "cost": float(os.environ.get("SOLAR_BATTERY_COST_5", "2100"))},
    {"name": "10 kWh", "usable": 9.0, "max_w": 4200,
     "cost": float(os.environ.get("SOLAR_BATTERY_COST_10", "3500"))},
]
BATTERY_EFF = 0.95
# Eventos: leitura rápida só do consumo, para apanhar aparelhos que ligam poucos segundos
FAST_SECONDS = int(os.environ.get("SOLAR_FAST_SECONDS", "5"))
EVENT_W = float(os.environ.get("SOLAR_EVENT_W", "300"))  # salto mínimo acima do consumo de fundo  # eficiência em cada sentido (≈ 90% ida e volta)
BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "solar.db")

_lock = threading.Lock()
_fast = {"load": None, "ts": None, "errors": 0}  # última leitura rápida do consumo
_bus = threading.Lock()  # o dongle aceita mal ligações em simultâneo: uma leitura de cada vez
_last = {"ok": False, "error": "ainda sem leitura", "ts": None}


# ---------------------------------------------------------------- Modbus
def read_input_registers(start, count):
    """Função 04 (input registers). Nunca escreve no inversor."""
    with _bus:
        return _read(start, count)


def _read(start, count):
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
        # energia DC de cada linha de painéis (strings)
        "epv1_today": u32(55), "epv1_total": u32(57),
        "epv2_today": u32(59), "epv2_total": u32(61),
    }


# ---------------------------------------------------------------- DB
COLS = ["status", "ppv", "pv1_v", "pv1_a", "pv1_w", "pv2_v", "pv2_a", "pv2_w",
        "pac", "grid_v", "grid_hz", "p_import", "p_export", "p_load",
        "e_pv_today", "e_pv_total", "e_import_today", "e_import_total",
        "e_export_today", "e_export_total", "e_load_today", "e_load_total", "temp",
        "epv1_today", "epv1_total", "epv2_today", "epv2_total"]

# Os contadores "hoje" do inversor só voltam a zero quando ele acorda de manhã,
# por isso os totais por dia calculam-se pela diferença dos contadores totais.
TOTALS = {"pv": "e_pv_total", "load": "e_load_total", "imp": "e_import_total",
          "exp": "e_export_total", "pv1": "epv1_total", "pv2": "epv2_total"}


def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS samples (ts TEXT PRIMARY KEY, %s)"
                    % ", ".join("%s REAL" % c for c in COLS))
        have = {r[1] for r in con.execute("PRAGMA table_info(samples)")}
        for c in COLS:
            if c not in have:
                con.execute("ALTER TABLE samples ADD COLUMN %s REAL" % c)
        con.execute("CREATE INDEX IF NOT EXISTS idx_samples_day ON samples(substr(ts,1,10))")


def init_events():
    with db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS events (start TEXT PRIMARY KEY, end TEXT, secs REAL, "
                    "base_w REAL, peak_w REAL, avg_w REAL, wh REAL)")


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


def fast_poller():
    """Lê o consumo a cada FAST_SECONDS e guarda cada salto acima do consumo de fundo:
    início, duração, potência e energia. Serve para identificar aparelhos."""
    hist = deque(maxlen=max(12, 300 // FAST_SECONDS))  # últimos ~5 min, para o consumo de fundo
    ev = None
    while True:
        t = datetime.now()
        try:
            r = read_input_registers(3045, 2)
            load = ((r[0] << 16) | r[1]) / 10.0
            _fast.update(load=load, ts=t.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            _fast["errors"] += 1
            time.sleep(FAST_SECONDS)
            continue
        if ev is None:
            base = statistics.median(hist) if len(hist) >= 6 else None
            if base is not None and load > base + EVENT_W:
                ev = {"start": t, "last": t, "base": base, "peak": load, "sum": 0.0, "n": 0, "wh": 0.0, "low": 0}
            else:
                hist.append(load)
        if ev is not None:
            dt = (t - ev["last"]).total_seconds() or FAST_SECONDS
            ev["last"] = t
            if load < ev["base"] + EVENT_W / 2:
                ev["low"] += 1
            else:
                ev["low"] = 0
                ev["peak"] = max(ev["peak"], load)
                ev["sum"] += load - ev["base"]
                ev["n"] += 1
                ev["wh"] += (load - ev["base"]) * min(dt, 30) / 3600
            # termina com 2 leituras seguidas de volta ao fundo, ou ao fim de 3 h
            if ev["low"] >= 2 or (t - ev["start"]).total_seconds() > 10800:
                end = t - timedelta(seconds=FAST_SECONDS * ev["low"])
                secs = max((end - ev["start"]).total_seconds(), FAST_SECONDS)
                try:
                    with db() as con:
                        con.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?)", (
                            ev["start"].strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"),
                            round(secs), round(ev["base"]), round(ev["peak"]),
                            round(ev["sum"] / max(ev["n"], 1)), round(ev["wh"], 1)))
                except Exception:
                    pass
                ev = None
        time.sleep(max(0.5, FAST_SECONDS - (datetime.now() - t).total_seconds()))


def api_events(days=7):
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM events WHERE start>=? ORDER BY start DESC", (since,))]
    # agrupa por potência acima do fundo (degraus de 100 W): o mesmo aparelho puxa sempre o mesmo
    groups = {}
    for r in rows:
        k = int(round(r["avg_w"] / 100.0) * 100)
        g = groups.setdefault(k, {"watts": k, "count": 0, "secs": 0, "wh": 0.0, "times": []})
        g["count"] += 1
        g["secs"] += r["secs"]
        g["wh"] += r["wh"]
        g["times"].append(r["start"][5:16])
    out = []
    for g in sorted(groups.values(), key=lambda g: -g["count"]):
        out.append({"watts": g["watts"], "count": g["count"], "avg_secs": round(g["secs"] / g["count"]),
                    "wh_day": round(g["wh"] / days, 1), "times": g["times"][:8]})
    return {"days": days, "threshold_w": EVENT_W, "fast_seconds": FAST_SECONDS,
            "events": rows[:50], "groups": out}


# ---------------------------------------------------------------- API
def api_today(day):
    with db() as con:
        rows = con.execute(
            "SELECT substr(ts,12,5) t, ppv, p_load, p_import, p_export FROM samples "
            "WHERE substr(ts,1,10)=? ORDER BY ts", (day,)).fetchall()
    return [dict(r) for r in rows]


def api_days(n):
    since = (datetime.now() - timedelta(days=n - 1)).strftime("%Y-%m-%d")
    sel = ", ".join("max(%s) %s" % (c, k) for k, c in TOTALS.items())
    with db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT substr(ts,1,10) day, %s FROM samples GROUP BY day ORDER BY day" % sel)]
        firsts = {r["day"]: dict(r) for r in con.execute(
            "SELECT substr(ts,1,10) day, %s FROM samples GROUP BY day"
            % ", ".join("min(%s) %s" % (c, k) for k, c in TOTALS.items()))}
    out, prev = [], None
    for r in rows:
        d = {"day": r["day"], "partial": []}
        for k in TOTALS:
            # ponto de partida: o fim do dia anterior; sem ele, o início do próprio dia (dia incompleto)
            if prev and prev[k] is not None:
                base = prev[k]
            else:
                base = firsts[r["day"]][k]
                d["partial"].append(k)
            d[k] = round(r[k] - base, 1) if r[k] is not None and base is not None else None
        prev = r
        if r["day"] >= since:
            out.append(d)
    return out


def api_battery(days=30):
    """Simula, com as leituras reais, quanto uma bateria teria poupado: guarda o que
    iria para a rede e devolve-o quando a casa compraria à rede."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with db() as con:
        rows = con.execute("SELECT ts, p_import, p_export FROM samples "
                           "WHERE substr(ts,1,10)>=? ORDER BY ts", (since,)).fetchall()
    if len(rows) < 2:
        return {"days": 0, "options": []}
    first, last = (datetime.strptime(rows[i]["ts"], "%Y-%m-%d %H:%M:%S") for i in (0, -1))
    ndays = max((last - first).total_seconds() / 86400, 1 / 24)
    imp_kwh = exp_kwh = 0.0
    sims = [{"soc": 0.0, "out": 0.0, "in": 0.0, **b} for b in BATTERIES]
    prev_t = first
    for r in rows[1:]:
        t = datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S")
        dt = min((t - prev_t).total_seconds(), 300) / 3600  # horas; falhas longas não contam
        prev_t = t
        p_imp, p_exp = r["p_import"] or 0, r["p_export"] or 0
        imp_kwh += p_imp * dt / 1000
        exp_kwh += p_exp * dt / 1000
        for b in sims:
            if p_exp > 0:
                take = min(min(p_exp, b["max_w"]) * dt / 1000, (b["usable"] - b["soc"]) / BATTERY_EFF)
                b["soc"] += take * BATTERY_EFF
                b["in"] += take
            elif p_imp > 0:
                give = min(min(p_imp, b["max_w"]) * dt / 1000, b["soc"] * BATTERY_EFF)
                b["soc"] -= give / BATTERY_EFF
                b["out"] += give
    opts = []
    for b in sims:
        per_day = b["out"] / ndays
        # poupa o kWh comprado, mas perde o que receberia se vendesse esse excedente
        year_eur = (b["out"] * PRICE_KWH - b["in"] * EXPORT_PRICE) / ndays * 365
        opts.append({"name": b["name"], "usable": b["usable"], "cost": b["cost"],
                     "kwh_day": round(per_day, 2), "kwh_year": round(per_day * 365),
                     "eur_year": round(year_eur), "cycles_year": round(b["out"] / b["usable"] / ndays * 365),
                     "payback_years": round(b["cost"] / year_eur, 1) if year_eur > 0 else None})
    return {"days": round(ndays, 1), "since": rows[0]["ts"][:10],
            "import_kwh_day": round(imp_kwh / ndays, 2), "export_kwh_day": round(exp_kwh / ndays, 2),
            "price": PRICE_KWH, "export_price": EXPORT_PRICE, "options": opts}


def api_strings(day):
    """Pico de potência de cada linha no dia (valor e hora)."""
    out = {}
    with db() as con:
        for i in (1, 2):
            r = con.execute("SELECT substr(ts,12,5) t, pv%d_w w FROM samples WHERE substr(ts,1,10)=? "
                            "ORDER BY pv%d_w DESC LIMIT 1" % (i, i), (day,)).fetchone()
            out["pv%d_peak_w" % i] = r["w"] if r else None
            out["pv%d_peak_t" % i] = r["t"] if r else None
    return out


def day_totals(day):
    """Energia de cada contador num dia, pela diferença dos totais."""
    rows = api_days(max(1, (datetime.now() - datetime.strptime(day, "%Y-%m-%d")).days + 1))
    return next((r for r in rows if r["day"] == day), None)


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
        pages = {"/": "index.html", "/index.html": "index.html", "/guia": "guia.html", "/guia.html": "guia.html"}
        if path in pages:
            with open(os.path.join(BASE, pages[path]), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/now":
            with _lock:
                now = dict(_last)
            t = day_totals(datetime.now().strftime("%Y-%m-%d"))
            if t:  # corrige os "hoje" do inversor, que de madrugada ainda mostram ontem
                for k, c in (("pv", "e_pv_today"), ("load", "e_load_today"),
                             ("imp", "e_import_today"), ("exp", "e_export_today"),
                             ("pv1", "epv1_today"), ("pv2", "epv2_today")):
                    if t[k] is not None and k not in t["partial"]:
                        now[c] = t[k]
            now.update(api_strings(datetime.now().strftime("%Y-%m-%d")))
            days = (datetime.now() - datetime.strptime(INSTALL_DATE, "%Y-%m-%d")).days
            return self._send(200, json.dumps({**now, "price": PRICE_KWH, "price_label": PRICE_LABEL,
                                               "export_price": EXPORT_PRICE, "install_date": INSTALL_DATE,
                                               "install_days": days, "kwp": KWP,
                                               "rent_month": RENT_MONTH, "fast": _fast}), "application/json")
        if path == "/api/events":
            n = max(1, min(int(params.get("days", "7")), 90))
            return self._send(200, json.dumps(api_events(n)), "application/json")
        if path == "/api/battery":
            n = max(1, min(int(params.get("days", "30")), 400))
            return self._send(200, json.dumps(api_battery(n)), "application/json")
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
    init_events()
    load_last()
    threading.Thread(target=poller, daemon=True).start()
    threading.Thread(target=fast_poller, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()
