"""
¿Qué anticipa el movimiento tras un evento Wyckoff? Analiza events.csv (lo escribe el bot en cada evento).

Para cada evento baja las velas POSTERIORES de BingX y mide el recorrido a favor de la dirección esperada
en unidades de ATR a +6/+12/+24/+48 velas. Luego lo desglosa por rasgos de perpetuos:
  · funding: ¿el lado amontonado era el contrario al de la señal (combustible para un squeeze) o el mismo?
  · open interest 6h/24h: ¿caía (limpieza de posiciones) o subía (más apalancamiento entrando)?
  · prima mark/index.

  python events_report.py --csv /data/events.csv            (en Railway: railway run o descarga el archivo)
  python events_report.py --csv events.csv --event SPRING,UTAD --horizon 24

Con menos de ~30 eventos por celda no hay conclusión: es un dibujo. Hace falta acumular semanas de registro
(Binance solo guarda 30 días de historial de OI, por eso el bot lo registra en vivo).
"""
import argparse
import csv
import math
import sys
import time
from collections import defaultdict

import config as C

HORIZONS = (6, 12, 24, 48)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load_events(path):
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            r["ts_ms"] = int(float(r["ts_ms"]))
            for k in ("price", "atr", "funding_pct", "premium_pct", "oi_now", "oi_chg_1h", "oi_chg_6h", "oi_chg_24h"):
                r[k] = _f(r.get(k))
            out.append(r)
    return out


def forward(events, get_klines):
    """Añade a cada evento ret_6/12/24/48 (en ATR, con el signo de la dirección esperada) y mfe24/mae24."""
    by = defaultdict(list)
    for e in events:
        by[(e["symbol"], e["tf"])].append(e)
    done = []
    for (sym, tf), evs in by.items():
        tf_ms = C.tf_seconds(tf) * 1000
        need = int((time.time() * 1000 - min(e["ts_ms"] for e in evs)) / tf_ms) + 60
        try:
            rows = get_klines(sym, tf, min(need, 5000), tf_ms)
        except Exception as ex:  # símbolo deslistado, red…
            print(f"  (sin velas de {sym} {tf}: {ex})", file=sys.stderr)
            continue
        idx = {r[0] + tf_ms: i for i, r in enumerate(rows)}  # tiempo de CIERRE de cada vela → posición
        for e in evs:
            i = idx.get(e["ts_ms"])
            sgn = 1 if e["dir"] == "LONG" else -1 if e["dir"] == "SHORT" else 0
            if i is None or sgn == 0 or not e["atr"] or e["atr"] <= 0 or e["price"] is None:
                continue
            for k in HORIZONS:
                e[f"ret_{k}"] = (rows[i + k][4] - e["price"]) * sgn / e["atr"] if i + k < len(rows) else None
            win = rows[i + 1:i + 25]
            if win:
                hi, lo = max(r[2] for r in win), min(r[3] for r in win)
                e["mfe24"] = ((hi if sgn == 1 else -lo) - (e["price"] if sgn == 1 else -e["price"])) / e["atr"]
                e["mae24"] = ((lo if sgn == 1 else -hi) - (e["price"] if sgn == 1 else -e["price"])) / e["atr"]
            done.append(e)
    return done


def stats(vals):
    v = [x for x in vals if x is not None]
    n = len(v)
    if n == 0:
        return n, 0.0, 0.0, 0.0
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1)) if n > 1 else 0.0
    t = m / (sd / math.sqrt(n)) if sd > 0 else 0.0
    return n, m, sum(1 for x in v if x > 0) * 100.0 / n, t


# ── rasgos ──
def f_funding(e):
    f = e["funding_pct"]
    if f is None or e["dir"] not in ("LONG", "SHORT"):
        return "sin dato"
    s = f if e["dir"] == "SHORT" else -f      # >0: el lado amontonado coincide con el de la señal
    return "en contra (lado amontonado = el de la señal)" if s >= 0.03 else \
        "a favor (el lado amontonado es el contrario)" if s <= -0.03 else "neutro (±0.03%)"


def _oi(key, lo, hi):
    def g(e):
        v = e[key]
        return "sin dato" if v is None else f"OI baja >{abs(lo)}%" if v <= lo else f"OI sube >{hi}%" if v >= hi else "OI estable"
    return g


def f_prem(e):
    p = e["premium_pct"]
    return "sin dato" if p is None else "prima >+0.1%" if p >= 0.1 else "prima <-0.1%" if p <= -0.1 else "prima ~0"


FEATURES = (("funding", f_funding), ("OI 6h", _oi("oi_chg_6h", -3, 3)), ("OI 24h", _oi("oi_chg_24h", -5, 5)),
            ("prima mark/index", f_prem))


def report(events, horizon=24, event_filter=None, out=print):
    key = f"ret_{horizon}"
    evs = [e for e in events if e.get(key) is not None and (not event_filter or e["event"] in event_filter)]
    out(f"\n{len(evs)} eventos con +{horizon} velas de futuro · recorrido en ATR a favor de la dirección esperada")
    if not evs:
        out("Nada que analizar todavía: deja el bot acumulando eventos.")
        return
    groups = defaultdict(list)
    for e in evs:
        groups[e["event"]].append(e)
    for ev in sorted(groups, key=lambda k: -len(groups[k])):
        g = groups[ev]
        n, m, w, t = stats([e[key] for e in g])
        mfe = sum(e.get("mfe24", 0) or 0 for e in g) / len(g)
        mae = sum(e.get("mae24", 0) or 0 for e in g) / len(g)
        out(f"\n━━ {ev}: {n} eventos · media {m:+.2f} ATR · a favor {w:.0f}% · t {t:+.2f} · "
            f"MFE {mfe:+.2f} / MAE {mae:+.2f} (24 velas)")
        for fname, fn in FEATURES:
            sub = defaultdict(list)
            for e in g:
                sub[fn(e)].append(e[key])
            if len(sub) == 1 and "sin dato" in sub:
                continue
            out(f"  {fname}")
            for lab in sorted(sub):
                n2, m2, w2, t2 = stats(sub[lab])
                out(f"    {lab:<46} {n2:>4}  media {m2:+.2f}  a favor {w2:3.0f}%  t {t2:+.2f}"
                    + ("  ⚠ <30" if n2 < 30 else ""))
    out("\nCon menos de ~30 eventos por celda es un dibujo, no evidencia. Con muchas celdas, alguna saldrá bien por azar: "
        "confirma en un periodo posterior antes de usarlo como filtro.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=f"{C.DATA_DIR}/events.csv")
    ap.add_argument("--event", default="", help="SC,BC,SPRING,UTAD,TEST,CTEST,SOS,SOW,LPS,LPSY,FASE_B,FASE_E,ENTRADA,ROTURA_DEMOTE…")
    ap.add_argument("--horizon", type=int, default=24, choices=HORIZONS)
    a = ap.parse_args()
    from bingx import BingX
    ex = BingX("", "")
    events = load_events(a.csv)
    print(f"{len(events)} eventos en {a.csv}")
    ev = forward(events, lambda s, tf, n, ms: ex.klines_history(s, tf, n, ms))
    report(ev, a.horizon, {x.strip().upper() for x in a.event.split(",") if x.strip()} or None)


if __name__ == "__main__":
    main()
