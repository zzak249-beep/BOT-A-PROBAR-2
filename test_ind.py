"""
Prueba sin red de los complementos (ADX, RSI desde el clímax, AVWAP) y de su integración en el escáner.
  python test_ind.py
"""
import random
import config as C
from strategy import adx_last, ind_features, rsi_series, scan_candidates, select_trades, filters
from test_engine import synth


def trend_rows(n, drift, vol=0.002, seed=3):
    rnd, p, rows = random.Random(seed), 100.0, []
    for i in range(n):
        o = p
        c = o * (1 + drift + rnd.gauss(0, vol))
        rows.append([i * 900_000, o, max(o, c) * 1.001, min(o, c) * 0.999, c, 1000.0])
        p = c
    return rows


# 1) ADX: tendencia fuerte >> rango
a_tr, _ = adx_last(trend_rows(300, 0.004))
a_rg, _ = adx_last(trend_rows(300, 0.0, 0.004))
assert a_tr > 35 and a_rg < 25 and a_tr > a_rg + 15, (a_tr, a_rg)
print(f"1 OK ADX tendencia {a_tr:.0f} vs rango {a_rg:.0f}")

# 2) RSI en [0,100], alto en subida, bajo en bajada
up, dn = rsi_series([r[4] for r in trend_rows(120, 0.004)])[-1], rsi_series([r[4] for r in trend_rows(120, -0.004)])[-1]
assert up > 70 and dn < 30
print(f"2 OK RSI subida {up:.0f} / bajada {dn:.0f}")

# 3) Clímax de venta con caída fuerte y recuperación: RSI recupera → rsi_gain>0 y AVWAP calculado
rows = trend_rows(60, -0.01)
rows += trend_rows(80, 0.0015, seed=5)
for i, r in enumerate(rows):
    r[0] = i * 900_000
f = ind_features(rows, len(rows) - 1, "LONG", rows[59][0])
assert f["rsi_gain"] is not None and f["rsi_gain"] > 0 and f["avwap_align"] in ("a favor", "en contra") and f["adx"] is not None, f
f_short = ind_features(rows, len(rows) - 1, "SHORT", rows[59][0])
assert f_short["avwap_align"] != f["avwap_align"] or f["avwap_dist"] == -f_short["avwap_dist"]
print("3 OK rsi_gain", f["rsi_gain"], "AVWAP", f["avwap_align"], f["avwap_dist"])

# 4) sin ancla o sin datos: no inventa y los filtros no bloquean
none = ind_features(rows[:10], 9, "LONG", None)
assert none["adx"] is None and none["rsi_gain"] is None and none["avwap_align"] == "-"
class Cfg:
    def __getattr__(self, k): return getattr(C, k)
cfg = Cfg(); cfg.ADX_FILTER = cfg.DIV_FILTER = cfg.AVWAP_FILTER = "bloquea"
sig = {"side": "LONG", "rr": 3, "risk_pct": 1.0, **none}
assert filters(sig, cfg, 0) == []
sig2 = {**sig, "adx": 45.0, "rsi_gain": -3.0, "avwap_align": "en contra"}
assert len(filters(sig2, cfg, 0)) == 3
print("4 OK filtros: sin datos no bloquea, con malos datos bloquea 3 motivos")

# 5) integración: el escáner pone las características en cada candidata y los filtros recortan operaciones
cands = [c for s in range(1, 5) for c in scan_candidates(synth(s), 900, 0.0001, "Agresivo", C, 100)]
assert cands and all("adx" in c and "rsi_gain" in c and "avwap_align" in c for c in cands)
have = sum(1 for c in cands if c["adx"] is not None)
print(f"5 OK {len(cands)} candidatas, {have} con ADX")
base = len(select_trades(cands, cfg.__class__()))
cfg2 = Cfg(); cfg2.ADX_FILTER = "bloquea"; cfg2.ADX_MAX = 0.0
assert len(select_trades(cands, cfg2)) <= base
print("TODO OK")
