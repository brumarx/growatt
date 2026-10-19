#!/usr/bin/env python3
"""Solar — lê o inversor Growatt MIN 4200TL-XH via Modbus TCP (só leitura)
e serve uma página com produção, consumo e trocas com a rede.

Sem dependências externas: stdlib apenas.
"""
import bisect
import json
import re
import urllib.request
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
# Baterias a simular, com custo em €. Capacidade útil ≈ 90% da nominal.
# "inversor": ligadas ao MIN 4200TL-XH (ARK XH ou APX), com instalação; precisam de autorização da Otovo.
# "tomada": baterias AC que se ligam a uma tomada e carregam com o excedente (medidor CT/Shelly no quadro).
#   Numa tomada partilhada só podem devolver 800 W. Eficiência ida e volta e preços: energienerds.nl, jan. 2026.
BATTERIES = [
    {"name": "Growatt 5 kWh", "kind": "inversor", "usable": 4.5, "charge_w": 2500, "discharge_w": 2500, "rte": 0.90,
     "cost": float(os.environ.get("SOLAR_BATTERY_COST_5", "2100"))},
    {"name": "Growatt 10 kWh", "kind": "inversor", "usable": 9.0, "charge_w": 4200, "discharge_w": 4200, "rte": 0.90,
     "cost": float(os.environ.get("SOLAR_BATTERY_COST_10", "3500"))},
    {"name": "Zendure SolarFlow 2400 AC+ (2,4 kWh)", "kind": "tomada", "usable": 2.16, "charge_w": 2400,
     "discharge_w": 800, "rte": 0.88, "cost": float(os.environ.get("SOLAR_BATTERY_COST_ZENDURE", "969"))},
    {"name": "Marstek Venus E 3.0 (5,12 kWh)", "kind": "tomada", "usable": 4.6, "charge_w": 2500,
     "discharge_w": 800, "rte": 0.83, "cost": float(os.environ.get("SOLAR_BATTERY_COST_MARSTEK", "1200"))},
]
# Eventos: leitura rápida só do consumo, para apanhar aparelhos que ligam poucos segundos
FAST_SECONDS = int(os.environ.get("SOLAR_FAST_SECONDS", "5"))
EVENT_W = float(os.environ.get("SOLAR_EVENT_W", "300"))  # salto mínimo acima do consumo de fundo
# Tomadas Shelly com medição (lidas pela rede local, sem cloud)
PLUG_SECONDS = int(os.environ.get("SOLAR_PLUG_SECONDS", "5"))
PLUG_ON_W = float(os.environ.get("SOLAR_PLUG_ON_W", "10"))  # acima disto o aparelho está a trabalhar
PLUG_GAP = int(os.environ.get("SOLAR_PLUG_GAP", "600"))  # pausa (s) que ainda conta como o mesmo uso
# Estudos de tarifa (preços SEM IVA e SEM descontos, para comparar opções iguais entre si)
POWER_KVA = float(os.environ.get("SOLAR_POWER_KVA", "6.9"))
# potência €/dia por kVA; os valores abaixo de 6,9 são proporcionais (estimativa): confirmar com o comercializador
POWER_PRICES = {float(k): float(v) for k, v in (x.split(":") for x in os.environ.get(
    "SOLAR_POWER_PRICES", "3.45:0.3150,4.6:0.4100,5.75:0.4940,6.9:0.5780").split(","))}
SIMPLE_KWH = float(os.environ.get("SOLAR_SIMPLE_KWH", "0.1671"))
# bi-horária: fora de vazio, vazio (€/kWh) e potência 6,9 kVA (€/dia)
BI_PRICES = [float(x) for x in os.environ.get("SOLAR_BI_PRICES", "0.1916,0.1108,0.7645").split(",")]
BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "solar.db")

_lock = threading.Lock()
_fast = {"load": None, "imp": None, "ts": None, "errors": 0}  # última leitura rápida do consumo
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
        con.execute("CREATE TABLE IF NOT EXISTS peaks (day TEXT PRIMARY KEY, imp_w REAL, imp_ts TEXT, "
                    "load_w REAL, load_ts TEXT)")
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
    peak = {"day": None, "imp_w": 0, "load_w": 0}
    while True:
        t = datetime.now()
        try:
            r = read_input_registers(3041, 6)  # compra, venda e consumo
            imp = ((r[0] << 16) | r[1]) / 10.0
            load = ((r[4] << 16) | r[5]) / 10.0
            ts = t.strftime("%Y-%m-%d %H:%M:%S")
            _fast.update(load=load, imp=imp, ts=ts)
            # pico do dia (compra à rede e consumo), para o estudo da potência contratada
            day = ts[:10]
            if peak["day"] != day:
                peak = {"day": day, "imp_w": 0, "load_w": 0}
            if imp > peak["imp_w"] or load > peak["load_w"]:
                with db() as con:
                    if imp > peak["imp_w"]:
                        peak["imp_w"] = imp
                        con.execute("INSERT INTO peaks (day, imp_w, imp_ts) VALUES (?,?,?) ON CONFLICT(day) DO "
                                    "UPDATE SET imp_w=excluded.imp_w, imp_ts=excluded.imp_ts "
                                    "WHERE excluded.imp_w > coalesce(peaks.imp_w, 0)", (day, imp, ts))
                    if load > peak["load_w"]:
                        peak["load_w"] = load
                        con.execute("INSERT INTO peaks (day, load_w, load_ts) VALUES (?,?,?) ON CONFLICT(day) DO "
                                    "UPDATE SET load_w=excluded.load_w, load_ts=excluded.load_ts "
                                    "WHERE excluded.load_w > coalesce(peaks.load_w, 0)", (day, load, ts))
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
                        con.execute("INSERT OR REPLACE INTO events (start, end, secs, base_w, peak_w, avg_w, wh) VALUES (?,?,?,?,?,?,?)", (
                            ev["start"].strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"),
                            round(secs), round(ev["base"]), round(ev["peak"]),
                            round(ev["sum"] / max(ev["n"], 1)), round(ev["wh"], 1)))
                except Exception:
                    pass
                ev = None
        time.sleep(max(0.5, FAST_SECONDS - (datetime.now() - t).total_seconds()))


def api_power(days=30):
    """Picos de compra à rede contra o limite de cada potência contratada."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with db() as con:
        fast = {r["day"]: dict(r) for r in con.execute("SELECT * FROM peaks WHERE day>=?", (since,))}
        # antes da leitura rápida: o máximo das leituras de minuto
        slow = con.execute("SELECT substr(ts,1,10) day, max(p_import) w, ts FROM samples "
                           "WHERE substr(ts,1,10)>=? GROUP BY day", (since,)).fetchall()
    days_ = {}
    for r in slow:
        days_[r["day"]] = {"day": r["day"], "w": r["w"] or 0, "ts": r["ts"], "fast": False}
    for d, r in fast.items():
        if r["imp_w"] is not None and r["imp_w"] >= days_.get(d, {}).get("w", 0):
            days_[d] = {"day": d, "w": r["imp_w"], "ts": r["imp_ts"], "fast": True}
    rows = sorted(days_.values(), key=lambda r: r["day"])
    top = sorted(rows, key=lambda r: -r["w"])[:5]
    peak = top[0]["w"] if top else 0
    cur = POWER_PRICES.get(POWER_KVA)
    options = []
    for kva, price in sorted(POWER_PRICES.items()):
        limit = kva * 1000
        options.append({"kva": kva, "limit_w": limit, "eur_day": price,
                        "days_over": sum(1 for r in rows if r["w"] > limit),
                        "margin_w": round(limit - peak),
                        "eur_year_vs_current": round((price - cur) * 365) if cur else None,
                        "current": kva == POWER_KVA})
    # a menor opção com pelo menos 15% de folga sobre o maior pico medido
    ok = [o for o in options if peak <= o["limit_w"] * 0.85]
    return {"days": len(rows), "peak_w": round(peak), "top": top, "options": options,
            "current_kva": POWER_KVA, "best_kva": ok[0]["kva"] if ok else None,
            "fast_since": min(fast) if fast else None}


def is_vazio(t, cycle):
    """Horas de vazio da tarifa bi-horária (BTN). Diário: 22h–8h todos os dias.
    Semanal: dias úteis 0h–7h; domingo todo o dia; sábado conforme a estação."""
    h = t.hour + t.minute / 60
    if cycle == "diario":
        return h >= 22 or h < 8
    wd = t.weekday()
    if wd == 6:
        return True
    if wd == 5:
        summer = 4 <= t.month <= 10  # hora legal de verão, aproximado
        if summer:
            return h < 9 or 14 <= h < 20 or h >= 22
        return h < 9.5 or 13 <= h < 18.5 or h >= 22
    return h < 7


def api_tariff(days=30):
    """Simples ou bi-horária, com a energia comprada à rede em cada minuto."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with db() as con:
        rows = con.execute("SELECT ts, p_import FROM samples WHERE substr(ts,1,10)>=? ORDER BY ts",
                           (since,)).fetchall()
    if len(rows) < 2:
        return {"days": 0}
    tot = 0.0
    vaz = {"diario": 0.0, "semanal": 0.0}
    by_hour = [0.0] * 24
    prev = datetime.strptime(rows[0]["ts"], "%Y-%m-%d %H:%M:%S")
    first = prev
    for r in rows[1:]:
        t = datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S")
        kwh = (r["p_import"] or 0) * min((t - prev).total_seconds(), 300) / 3600 / 1000
        prev = t
        tot += kwh
        by_hour[t.hour] += kwh
        for c in vaz:
            if is_vazio(t, c):
                vaz[c] += kwh
    ndays = max((prev - first).total_seconds() / 86400, 1 / 24)
    year = tot / ndays * 365
    fv, v, bi_pot = BI_PRICES
    simple_pot = POWER_PRICES.get(6.9, 0.578)
    simple_year = year * SIMPLE_KWH + simple_pot * 365
    cycles = []
    for c, kv in vaz.items():
        share = kv / tot if tot else 0
        cost = year * ((1 - share) * fv + share * v) + bi_pot * 365
        cycles.append({"cycle": c, "vazio_share": round(share * 100, 1),
                       "eur_year": round(cost), "diff_eur_year": round(cost - simple_year)})
    # % de vazio a partir da qual a bi-horária empata, contando com a potência
    be = None
    if year and fv != v:
        be = ((year * fv + bi_pot * 365) - simple_year) / (year * (fv - v))
    return {"days": round(ndays, 1), "import_kwh_day": round(tot / ndays, 2), "import_kwh_year": round(year),
            "simple_eur_year": round(simple_year), "simple_kwh": SIMPLE_KWH, "simple_pot": simple_pot,
            "bi_prices": BI_PRICES, "cycles": cycles,
            "breakeven_share": round(be * 100, 1) if be is not None else None,
            "by_hour": [round(x / ndays, 3) for x in by_hour]}


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
    apps = appliances()
    names = {a["id"]: a["name"] for a in apps}
    for r in rows[:50]:
        a = match_appliance(r, apps)
        r["appliance"] = names.get(a)
        r["manual"] = r.get("appliance_id") is not None
    return {"days": days, "threshold_w": EVENT_W, "fast_seconds": FAST_SECONDS,
            "events": rows[:50], "groups": out, "appliances": [{"id": a["id"], "name": a["name"]} for a in apps]}


# ---------------------------------------------------------------- Aparelhos
APP_TOL = 0.2  # um evento é de um aparelho se a potência acima do fundo estiver a ±20% da dele


def init_appliances():
    with db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS appliances (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                    "watts REAL NOT NULL, minutes REAL NOT NULL, per_week REAL NOT NULL, flexible INTEGER DEFAULT 1)")
        # energia por uso tirada da etiqueta (kWh/100 ciclos ÷ 100); vazio = potência × duração
        if "kwh_use" not in {r[1] for r in con.execute("PRAGMA table_info(appliances)")}:
            con.execute("ALTER TABLE appliances ADD COLUMN kwh_use REAL")
        if "plug_ip" not in {r[1] for r in con.execute("PRAGMA table_info(appliances)")}:
            con.execute("ALTER TABLE appliances ADD COLUMN plug_ip TEXT")
        if "appliance_id" not in {r[1] for r in con.execute("PRAGMA table_info(events)")}:
            con.execute("ALTER TABLE events ADD COLUMN appliance_id INTEGER")
        # tomadas: energia por dia (e a parte comprada à rede) e cada uso do aparelho
        # known_wh: a energia em que se sabia a origem (sol ou rede); sem leitura do inversor não conta
        con.execute("CREATE TABLE IF NOT EXISTS plug_daily (day TEXT, appliance_id INTEGER, wh REAL, grid_wh REAL, "
                    "known_wh REAL, secs REAL, PRIMARY KEY (day, appliance_id))")
        con.execute("CREATE TABLE IF NOT EXISTS plug_runs (appliance_id INTEGER, start TEXT, end TEXT, secs REAL, "
                    "wh REAL, grid_wh REAL, known_wh REAL, peak_w REAL, PRIMARY KEY (appliance_id, start))")


def appliances():
    with db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM appliances ORDER BY name")]


def match_appliance(ev, apps):
    """O aparelho escolhido à mão; senão, o de potência mais próxima dentro da tolerância."""
    if ev.get("appliance_id") is not None:
        return ev["appliance_id"]
    best = None
    for a in apps:
        if a.get("plug_ip"):  # com tomada, a medição é a da tomada
            continue
        d = abs(ev["avg_w"] - a["watts"]) / a["watts"]
        if d <= APP_TOL and (best is None or d < best[1]):
            best = (a["id"], d)
    return best[0] if best else None


def surplus_profile(days=14):
    """Excedente médio (W) em cada quarto de hora, nos últimos dias."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with db() as con:
        rows = con.execute("SELECT CAST(substr(ts,12,2) AS INTEGER)*4 + CAST(substr(ts,15,2) AS INTEGER)/15 slot, "
                           "avg(p_export) w FROM samples WHERE substr(ts,1,10)>=? GROUP BY slot", (since,)).fetchall()
        nd = con.execute("SELECT count(DISTINCT substr(ts,1,10)) FROM samples WHERE substr(ts,1,10)>=?",
                         (since,)).fetchone()[0]
    prof = [0.0] * 96
    for r in rows:
        prof[r["slot"]] = r["w"] or 0.0
    return prof, nd


def best_window(prof, watts, minutes):
    """Início (quarto de hora) em que o excedente cobre a maior parte do aparelho."""
    n = max(1, round(minutes / 15))
    best = (0, 0.0)
    for s in range(96 - n + 1):
        cov = sum(min(prof[s + i], watts) for i in range(n)) / (watts * n)
        if cov > best[1]:
            best = (s, cov)
    return best[0], n, best[1]


def api_appliances(days=14):
    apps = appliances()
    prof, prof_days = surplus_profile(days)
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with db() as con:
        evs = [dict(r) for r in con.execute("SELECT * FROM events WHERE start>=? ORDER BY start", (since,))]
        samples = con.execute("SELECT ts, p_import, p_load FROM samples WHERE ts>=? ORDER BY ts",
                              (evs[0]["start"][:10] if evs else since,)).fetchall()
    ts_list = [r["ts"] for r in samples]
    # dias com eventos registados (a leitura rápida só existe desde a 1.ª versão com eventos)
    ev_days = 0
    if evs:
        first = datetime.strptime(evs[0]["start"], "%Y-%m-%d %H:%M:%S")
        ev_days = max((datetime.now() - first).total_seconds() / 86400, 1 / 24)

    def grid_share(ev):
        """Parte do consumo da casa comprada à rede nos minutos do evento."""
        a = (datetime.strptime(ev["start"], "%Y-%m-%d %H:%M:%S") - timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
        b = (datetime.strptime(ev["end"], "%Y-%m-%d %H:%M:%S") + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
        win = samples[bisect.bisect_left(ts_list, a):bisect.bisect_right(ts_list, b)]
        load = sum(r["p_load"] or 0 for r in win)
        return min(1.0, sum(r["p_import"] or 0 for r in win) / load) if load else None

    meas = {a["id"]: {"count": 0, "wh": 0.0, "grid_wh": 0.0, "times": []} for a in apps}
    unmatched = {"count": 0, "wh": 0.0}
    for ev in evs:
        aid = match_appliance(ev, apps)
        if aid not in meas:
            unmatched["count"] += 1
            unmatched["wh"] += ev["wh"]
            continue
        g = grid_share(ev)
        m = meas[aid]
        m["count"] += 1
        m["wh"] += ev["wh"]
        m["grid_wh"] += ev["wh"] * (g if g is not None else 1)
        m["times"].append({"t": ev["start"][5:16], "grid_pct": None if g is None else round(g * 100)})

    # melhor hora: os aparelhos que gastam mais escolhem primeiro e ocupam esse excedente
    free = prof[:]
    plan = {}
    use_kwh = lambda a: a["kwh_use"] or a["watts"] * a["minutes"] / 60 / 1000
    # potência média durante o uso (uma máquina só aquece a água parte do tempo)
    avg_w = lambda a: min(a["watts"], use_kwh(a) * 1000 / (a["minutes"] / 60))
    for a in sorted((a for a in apps if a["flexible"]), key=lambda a: -use_kwh(a)):
        s, n, cov = best_window(free, avg_w(a), a["minutes"])
        for i in range(n):
            free[s + i] = max(0.0, free[s + i] - avg_w(a))
        plan[a["id"]] = (s, n, cov)

    plugs = plug_summary(days)
    hm = lambda slot: "%02d:%02d" % divmod(slot * 15, 60)
    out = []
    for a in apps:
        kwh_use = use_kwh(a)
        kwh_month = kwh_use * a["per_week"] * 52 / 12
        r = {**a, "kwh_use": round(kwh_use, 2), "eur_use": round(kwh_use * PRICE_KWH, 2),
             "kwh_month": round(kwh_month, 1), "eur_month": round(kwh_month * PRICE_KWH, 2), "best": None}
        if a["id"] in plan and prof_days:
            s, n, cov = plan[a["id"]]
            r["best"] = {"start": hm(s), "end": hm(min(s + n, 96)) if s + n < 96 else "24:00",
                         "solar_pct": round(cov * 100), "save_eur_use": round(kwh_use * cov * PRICE_KWH, 2)}
        m = meas[a["id"]]
        r["measured"] = None
        if m["count"] and ev_days:
            r["measured"] = {"count": m["count"], "kwh_month": round(m["wh"] / ev_days * 30 / 1000, 1),
                             "grid_pct": round(m["grid_wh"] / m["wh"] * 100) if m["wh"] else None,
                             "eur_month": round(m["grid_wh"] / ev_days * 30 / 1000 * PRICE_KWH, 2),
                             "times": m["times"][-8:][::-1]}
        if a.get("plug_ip"):
            r["plug"] = {**plugs.get(a["id"], {}), **_plugs.get(a["id"], {})}
            p = r["plug"]
            if p.get("days", 0) >= 1 / 24:  # com tomada (pelo menos 1 h medida), o medido é o da tomada
                r["measured"] = {"source": "tomada", "count": len(p["runs"]),
                                 "kwh_month": round(p["wh"] / p["days"] * 30 / 1000, 1),
                                 "grid_pct": round(p["grid_wh"] / p["known_wh"] * 100) if p["known_wh"] else None,
                                 # a parte da rede aplicada a toda a energia da tomada
                                 "eur_month": round(p["wh"] * (p["grid_wh"] / p["known_wh"] if p["known_wh"] else 1)
                                                    / p["days"] * 30 / 1000 * PRICE_KWH, 2),
                                 "days": round(p["days"], 1),
                                 "times": [{"t": x["start"][5:16], "grid_pct": x["grid_pct"], "secs": x["secs"],
                                            "kwh": round(x["wh"] / 1000, 2)} for x in p["runs"][:8]]}
        out.append(r)
    out.sort(key=lambda r: -(r["measured"]["kwh_month"] if r["measured"] else r["kwh_month"]))
    return {"price": PRICE_KWH, "profile_days": prof_days, "events_days": round(ev_days, 1), "tolerance": APP_TOL,
            "threshold_w": EVENT_W, "appliances": out,
            "unmatched": {"count": unmatched["count"],
                          "kwh_month": round(unmatched["wh"] / ev_days * 30 / 1000, 1) if ev_days else 0},
            "profile": [round(w) for w in prof]}


def save_appliance(d):
    name = str(d.get("name", "")).strip()[:60]
    watts, minutes, per_week = (float(d.get(k) or 0) for k in ("watts", "minutes", "per_week"))
    kwh_use = float(d["kwh_use"]) if d.get("kwh_use") not in (None, "") else None
    plug_ip = str(d.get("plug_ip") or "").strip() or None
    if plug_ip and not PLUG_RE.match(plug_ip):
        raise ValueError("IP da tomada inválido (exemplo: 192.168.1.80)")
    if not name or not (0 < watts <= 10000) or not (0 < minutes <= 1440) or not (0 <= per_week <= 100):
        raise ValueError("nome, potência (1–10000 W), duração (1–1440 min) e vezes por semana (0–100)")
    if kwh_use is not None and not (0 < kwh_use <= watts * minutes / 60 / 1000 + 1e-9):
        raise ValueError("kWh por uso tem de ser maior que 0 e no máximo potência × duração")
    vals = (name, watts, minutes, per_week, 1 if d.get("flexible", True) else 0, kwh_use, plug_ip)
    with db() as con:
        if d.get("id"):
            con.execute("UPDATE appliances SET name=?, watts=?, minutes=?, per_week=?, flexible=?, kwh_use=?, plug_ip=? "
                        "WHERE id=?", vals + (int(d["id"]),))
        else:
            con.execute("INSERT INTO appliances (name, watts, minutes, per_week, flexible, kwh_use, plug_ip) "
                        "VALUES (?,?,?,?,?,?,?)", vals)


def delete_appliance(d):
    with db() as con:
        con.execute("DELETE FROM appliances WHERE id=?", (int(d["id"]),))
        con.execute("UPDATE events SET appliance_id=NULL WHERE appliance_id=?", (int(d["id"]),))
        con.execute("DELETE FROM plug_daily WHERE appliance_id=?", (int(d["id"]),))
        con.execute("DELETE FROM plug_runs WHERE appliance_id=?", (int(d["id"]),))


def label_event(d):
    aid = d.get("appliance_id")
    with db() as con:
        con.execute("UPDATE events SET appliance_id=? WHERE start=?",
                    (int(aid) if aid not in (None, "") else None, str(d["start"])[:19]))


# ---------------------------------------------------------------- Tomadas (Shelly)
PLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,62}(:\d{1,5})?$")
_plugs = {}  # appliance_id -> última leitura da tomada


def read_plug(ip):
    """Potência (W) e energia total (Wh) de uma tomada com medição, lida pela rede local.
    Shelly Gen2/Gen3 (RPC), Shelly Gen1 e Tasmota (Nous A1T, Athom e outras com Tasmota)."""
    def get(path):
        with urllib.request.urlopen("http://%s%s" % (ip, path), timeout=3) as r:
            return json.loads(r.read())

    def shelly2():
        d = get("/rpc/Switch.GetStatus?id=0")
        return float(d["apower"]), float(d["aenergy"]["total"])

    def shelly1():
        m = get("/status")["meters"][0]  # total em watts-minuto
        return float(m["power"]), float(m["total"]) / 60

    def tasmota():
        e = get("/cm?cmnd=Status%2010")["StatusSNS"]["ENERGY"]  # total em kWh
        return float(e["Power"]), float(e["Total"]) * 1000

    for f in (shelly2, shelly1, tasmota):
        try:
            return f()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise IOError("a tomada pede palavra-passe: desligue a autenticação na app ou na página dela")
        except urllib.error.URLError as e:
            raise IOError("sem resposta da tomada (%s)" % e.reason)
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            pass  # não é este tipo de tomada: tenta o seguinte
    raise IOError("a tomada respondeu, mas não é Shelly nem Tasmota (ou tem palavra-passe)")


def grid_now():
    """Parte do consumo da casa que está a ser comprada à rede neste momento."""
    f = _fast
    if f.get("ts") and (datetime.now() - datetime.strptime(f["ts"], "%Y-%m-%d %H:%M:%S")).total_seconds() < 30:
        load, imp = f["load"], f["imp"]
    elif _last.get("ok"):
        load, imp = _last["p_load"], _last["p_import"]
    else:
        return None
    return min(1.0, imp / load) if load else None


def plug_poller():
    state = {}  # appliance_id -> {"total", "t", "run"}
    while True:
        t0 = datetime.now()
        try:
            apps = [a for a in appliances() if a.get("plug_ip")]
        except Exception:
            apps = []
        for a in apps:
            aid, st = a["id"], state.setdefault(a["id"], {"total": None, "t": None, "run": None})
            t = datetime.now()
            ts = t.strftime("%Y-%m-%d %H:%M:%S")
            try:
                w, total = read_plug(a["plug_ip"])
            except Exception as e:
                _plugs[aid] = {**_plugs.get(aid, {}), "ok": False, "error": str(e)[:120], "err_ts": ts}
                continue
            g = grid_now()
            _plugs[aid] = {"ok": True, "w": round(w, 1), "ts": ts, "grid_pct": None if g is None else round(g * 100)}
            wh = 0.0
            dt = (t - st["t"]).total_seconds() if st["t"] else 0
            if st["total"] is not None and 0 <= total - st["total"] <= 10000 * dt / 3600 + 1:
                wh = total - st["total"]  # contador da tomada; se voltou a zero (reinício), ignora esta leitura
            st["total"], st["t"] = total, t
            if dt > 120:  # falhou muito tempo: não conta como tempo medido
                dt = 0
            gw, kw = (wh * g, wh) if g is not None else (0.0, 0.0)
            try:
                with db() as con:
                    con.execute("INSERT INTO plug_daily VALUES (?,?,?,?,?,?) ON CONFLICT(day, appliance_id) DO UPDATE SET "
                                "wh=wh+excluded.wh, grid_wh=grid_wh+excluded.grid_wh, known_wh=known_wh+excluded.known_wh, "
                                "secs=secs+excluded.secs", (ts[:10], aid, wh, gw, kw, dt))
                    run = st["run"]
                    if w >= PLUG_ON_W:
                        if run is None:
                            run = st["run"] = {"start": ts, "wh": 0.0, "grid_wh": 0.0, "known_wh": 0.0, "peak": 0.0}
                        run["last_on"] = t
                        run["peak"] = max(run["peak"], w)
                    if run is not None:
                        run["wh"] += wh
                        run["grid_wh"] += gw
                        run["known_wh"] += kw
                        # termina depois de uma pausa longa (a máquina para entre fases)
                        if w < PLUG_ON_W and (t - run["last_on"]).total_seconds() > PLUG_GAP:
                            end = run["last_on"]
                            secs = (end - datetime.strptime(run["start"], "%Y-%m-%d %H:%M:%S")).total_seconds()
                            if run["wh"] >= 1:
                                con.execute("INSERT OR REPLACE INTO plug_runs VALUES (?,?,?,?,?,?,?,?)",
                                            (aid, run["start"], end.strftime("%Y-%m-%d %H:%M:%S"), round(secs),
                                             round(run["wh"], 1), round(run["grid_wh"], 1), round(run["known_wh"], 1),
                                             round(run["peak"])))
                            st["run"] = None
                    _plugs[aid]["running"] = st["run"] is not None
                    if st["run"]:
                        r = st["run"]
                        _plugs[aid]["run"] = {"start": r["start"], "wh": round(r["wh"], 1),
                                              "grid_pct": round(r["grid_wh"] / r["known_wh"] * 100) if r["known_wh"] else None}
            except Exception:
                pass
        time.sleep(max(1.0, PLUG_SECONDS - (datetime.now() - t0).total_seconds()))


def plug_summary(days=30):
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    out = {}
    with db() as con:
        for r in con.execute("SELECT appliance_id, sum(wh) wh, sum(grid_wh) g, sum(known_wh) k, sum(secs) s "
                             "FROM plug_daily WHERE day>=? GROUP BY appliance_id", (since,)):
            out[r["appliance_id"]] = {"wh": r["wh"] or 0, "grid_wh": r["g"] or 0, "known_wh": r["k"] or 0,
                                      "days": (r["s"] or 0) / 86400, "runs": []}
        today = datetime.now().strftime("%Y-%m-%d")
        for r in con.execute("SELECT appliance_id, wh, grid_wh FROM plug_daily WHERE day=?", (today,)):
            out.setdefault(r["appliance_id"], {"wh": 0, "grid_wh": 0, "days": 0, "runs": []})
            out[r["appliance_id"]]["today_wh"] = round(r["wh"] or 0, 1)
        for r in con.execute("SELECT * FROM plug_runs WHERE start>=? ORDER BY start DESC", (since,)):
            if r["appliance_id"] in out:
                out[r["appliance_id"]]["runs"].append(
                    {"start": r["start"], "secs": r["secs"], "wh": r["wh"], "peak_w": r["peak_w"],
                     "grid_pct": round(r["grid_wh"] / r["known_wh"] * 100) if r["known_wh"] else None})
    return out


def api_plug_test(d):
    ip = str(d.get("ip", "")).strip()
    if not PLUG_RE.match(ip):
        raise ValueError("IP da tomada inválido (exemplo: 192.168.1.80)")
    try:
        w, total = read_plug(ip)
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}
    return {"ok": True, "w": round(w, 1), "total_kwh": round(total / 1000, 2)}


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
    sims = [{"soc": 0.0, "out": 0.0, "in": 0.0, "eff": b["rte"] ** 0.5, **b} for b in BATTERIES]
    # noite: do fim da venda ao fim da tarde até à primeira venda da manhã seguinte;
    # a primeira noite não conta (a bateria simulada começa vazia)
    nights, night, selling = [], None, False
    prev_t = first
    for r in rows[1:]:
        t = datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S")
        dt = min((t - prev_t).total_seconds(), 300) / 3600  # horas; falhas longas não contam
        prev_t = t
        p_imp, p_exp = r["p_import"] or 0, r["p_export"] or 0
        imp_kwh += p_imp * dt / 1000
        exp_kwh += p_exp * dt / 1000
        if p_exp > 0:
            if night and t.hour < 14:
                nights.append(night)
            night, selling = None, True
        elif selling and t.hour >= 15:
            selling = False
            if t.date() != first.date():
                night = {"kwh": 0.0, "empty": [None] * len(sims), "grid": [0.0] * len(sims)}
        if night:
            night["kwh"] += p_imp * dt / 1000
        for i, b in enumerate(sims):
            if p_exp > 0:
                take = min(min(p_exp, b["charge_w"]) * dt / 1000, (b["usable"] - b["soc"]) / b["eff"])
                b["soc"] += take * b["eff"]
                b["in"] += take
            elif p_imp > 0:
                give = min(min(p_imp, b["discharge_w"]) * dt / 1000, b["soc"] * b["eff"])
                b["soc"] -= give / b["eff"]
                b["out"] += give
                if night:
                    night["grid"][i] += p_imp * dt / 1000 - give
                    if b["soc"] < 0.01 and night["empty"][i] is None:
                        night["empty"][i] = t
    opts = []
    for i, b in enumerate(sims):
        # hora mediana a que esvazia, contada desde o meio-dia para atravessar a meia-noite
        empties = sorted(n["empty"][i] for n in nights if n["empty"][i])
        mins = sorted((e.hour * 60 + e.minute - 720) % 1440 for e in empties)
        med = (mins[len(mins) // 2] + 720) % 1440 if mins else None
        per_day = b["out"] / ndays
        # poupa o kWh comprado, mas perde o que receberia se vendesse esse excedente
        year_eur = (b["out"] * PRICE_KWH - b["in"] * EXPORT_PRICE) / ndays * 365
        opts.append({"name": b["name"], "kind": b["kind"], "usable": b["usable"], "cost": b["cost"],
                     "discharge_w": b["discharge_w"], "rte": b["rte"],
                     "kwh_day": round(per_day, 2), "kwh_year": round(per_day * 365),
                     "eur_year": round(year_eur), "cycles_year": round(b["out"] / b["usable"] / ndays * 365),
                     "payback_years": round(b["cost"] / year_eur, 1) if year_eur > 0 else None,
                     "nights": len(nights), "nights_full": sum(1 for n in nights if not n["empty"][i]),
                     "empty_at": "%02d:%02d" % divmod(med, 60) if med is not None else None,
                     "night_grid_kwh": round(sum(n["grid"][i] for n in nights) / len(nights), 1) if nights else None})
    return {"days": round(ndays, 1), "since": rows[0]["ts"][:10],
            "night_kwh": round(sum(n["kwh"] for n in nights) / len(nights), 1) if nights else None,
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
        if path == "/api/power":
            return self._send(200, json.dumps(api_power(int(params.get("days", "30")))), "application/json")
        if path == "/api/tariff":
            return self._send(200, json.dumps(api_tariff(int(params.get("days", "30")))), "application/json")
        if path == "/api/events":
            n = max(1, min(int(params.get("days", "7")), 90))
            return self._send(200, json.dumps(api_events(n)), "application/json")
        if path == "/api/battery":
            n = max(1, min(int(params.get("days", "30")), 400))
            return self._send(200, json.dumps(api_battery(n)), "application/json")
        if path == "/api/appliances":
            n = max(1, min(int(params.get("days", "14")), 90))
            return self._send(200, json.dumps(api_appliances(n)), "application/json")
        if path == "/api/plugs":
            return self._send(200, json.dumps({str(k): v for k, v in _plugs.items()}, default=str), "application/json")
        if path == "/api/tip":
            return self._send(200, json.dumps(api_tip()), "application/json")
        if path == "/api/today":
            day = params.get("day", datetime.now().strftime("%Y-%m-%d"))[:10]
            return self._send(200, json.dumps(api_today(day)), "application/json")
        if path == "/api/days":
            n = max(1, min(int(params.get("n", "30")), 400))
            return self._send(200, json.dumps(api_days(n)), "application/json")
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        actions = {"/api/appliances": save_appliance, "/api/appliances/delete": delete_appliance,
                   "/api/events/label": label_event, "/api/plug/test": api_plug_test}
        path = self.path.partition("?")[0]
        if path not in actions:
            return self._send(404, "not found", "text/plain")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            res = actions[path](json.loads(self.rfile.read(min(n, 10000)) or b"{}"))
        except (ValueError, KeyError, TypeError) as e:
            return self._send(400, json.dumps({"error": str(e)}), "application/json")
        self._send(200, json.dumps(res or {"ok": True}), "application/json")

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    init_db()
    init_events()
    init_appliances()
    load_last()
    threading.Thread(target=poller, daemon=True).start()
    threading.Thread(target=fast_poller, daemon=True).start()
    threading.Thread(target=plug_poller, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()
