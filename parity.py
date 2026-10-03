"""
Paridad con TradingView: ¿el motor de Python da las MISMAS entradas que el indicador Pine?

Todo el bot depende de esto y hasta ahora solo se probó con ciclos sintéticos. Dos formas de comprobarlo:

 A) Mismas velas que TradingView (lo más limpio: elimina diferencias de datos)
      1. En TradingView, gráfico BINGX:BTCUSDT.P (el mismo exchange que opera el bot) y el TF a probar.
      2. Menú del gráfico → Exportar datos del gráfico → CSV (time, open, high, low, close, Volume).
      3. python parity.py --csv BINGX_BTCUSDT.P_60.csv --tf 1h
      4. Anota en expected.csv las ENTRADAS que dibuja el indicador (hora, LONG/SHORT) y añade --expected expected.csv

 B) Velas de BingX en directo:  python parity.py --fetch BTC-USDT --tf 1h --bars 3000

Sin --expected solo imprime las entradas del motor para compararlas a ojo con las etiquetas del gráfico.
Si TradingView y el motor no coinciden en velas iguales, hay una diferencia de traducción: manda el caso.
"""
import argparse
import csv
import sys
from datetime import datetime, timezone

import config as C
from strategy import build_signal
from wyckoff_engine import ENTRY_NAMES, PHASE_NAMES, WyckoffEngine


def parse_time(s):
    s = s.strip()
    if s.replace(".", "", 1).isdigit():
        v = float(s)
        return int(v if v > 1e11 else v * 1000)
    s = s.replace("Z", "+00:00").replace(" ", "T")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def load_csv(path):
    rows = []
    with open(path, newline="") as f:
        rd = csv.DictReader(f)
        cols = {k.strip().lower(): k for k in rd.fieldnames}
        need = {"time": None, "open": None, "high": None, "low": None, "close": None}
        for k in need:
            if k not in cols:
                sys.exit(f"falta la columna '{k}' en {path} (columnas: {list(cols)})")
        vcol = cols.get("volume") or cols.get("vol") or cols.get("volumen")
        for r in rd:
            rows.append([parse_time(r[cols["time"]]), float(r[cols["open"]]), float(r[cols["high"]]),
                         float(r[cols["low"]]), float(r[cols["close"]]), float(r[vcol]) if vcol and r[vcol] else 0.0])
    rows.sort()
    return rows


def tick_of(rows):
    dec = 0
    for r in rows[-300:]:
        for v in r[1:5]:
            s = f"{v:.10f}".rstrip("0")
            dec = max(dec, len(s.split(".")[1]) if "." in s else 0)
    return 10 ** (-min(dec, 8))


def run_engine(rows, tf_s, tick, strict, warmup=0):
    eng = WyckoffEngine(tf_s, tick, strict, keep_bars=100000)
    out = []
    for idx, (t, o, h, l, c, v) in enumerate(rows):
        if v <= 0 and h == l:
            continue
        d = eng.update(t, o, h, l, c, v)
        if d["entry_now"] and idx >= warmup:
            sig = build_signal(d, C, tick)
            out.append({"t": t, "side": sig["side"] if sig else ("LONG" if d["outcome"] > 0 else "SHORT"),
                        "kind": ENTRY_NAMES.get(d["entryKind"], "-"), "entry": d["close"],
                        "sl": sig["sl"] if sig else None, "tp1": sig["tp1"] if sig else None,
                        "tp2": sig["tp2"] if sig else None, "phase": PHASE_NAMES[d["phase"]]})
    return out, eng


def compare(engine_entries, expected, tf_ms, tol_bars=1):
    exp = [(parse_time(r["time"]), r["side"].strip().upper()) for r in expected]
    used, hit, only_tv = set(), [], []
    for t, side in exp:
        best = None
        for k, e in enumerate(engine_entries):
            if k in used or e["side"] != side:
                continue
            if abs(e["t"] - t) <= tol_bars * tf_ms and (best is None or abs(e["t"] - t) < abs(engine_entries[best]["t"] - t)):
                best = k
        if best is None:
            only_tv.append((t, side))
        else:
            used.add(best)
            hit.append((t, side, engine_entries[best]["t"]))
    only_py = [e for k, e in enumerate(engine_entries) if k not in used]
    return hit, only_tv, only_py


def fmt_t(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="exportación de datos de TradingView")
    ap.add_argument("--fetch", help="símbolo BingX, p. ej. BTC-USDT")
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--bars", type=int, default=3000)
    ap.add_argument("--strict", default=C.ENTRY_STRICTNESS)
    ap.add_argument("--expected", help="CSV con columnas time,side de las entradas del indicador")
    ap.add_argument("--warmup", type=int, default=300, help="velas iniciales sin comparar (las campañas empiezan a medias)")
    ap.add_argument("--tol", type=int, default=1, help="tolerancia en velas")
    a = ap.parse_args()
    if not a.csv and not a.fetch:
        sys.exit("indica --csv o --fetch")
    tf_s = C.tf_seconds(a.tf)
    if a.csv:
        rows = load_csv(a.csv)
    else:
        from bingx import BingX
        rows = BingX("", "").klines_history(a.fetch, a.tf, a.bars, tf_s * 1000)
        rows = [r for r in rows if r[0] + tf_s * 1000 <= __import__("time").time() * 1000]
    tick = tick_of(rows)
    print(f"{len(rows)} velas {a.tf} · tick {tick} · exigencia {a.strict} · {fmt_t(rows[0][0])} → {fmt_t(rows[-1][0])}")
    ents, eng = run_engine(rows, tf_s, tick, a.strict, a.warmup)
    print(f"\nEntradas del motor ({len(ents)}):")
    for e in ents:
        print(f"  {fmt_t(e['t'])}  {e['side']:<5} {e['kind']:<12} entrada {e['entry']:.6g}"
              + (f"  SL {e['sl']:.6g}  TP1 {e['tp1']:.6g}  TP2 {e['tp2']:.6g}" if e["sl"] else ""))
    print(f"\nEstado final: {eng.status_text()}")
    if a.expected:
        with open(a.expected, newline="") as f:
            exp = list(csv.DictReader(f))
        hit, only_tv, only_py = compare(ents, exp, tf_s * 1000, a.tol)
        n = max(len(exp), 1)
        print(f"\n══════ PARIDAD ══════\nTradingView {len(exp)} · coinciden {len(hit)} ({len(hit) * 100 // n}%) "
              f"· solo TradingView {len(only_tv)} · solo Python {len(only_py)}")
        for t, s in only_tv:
            print(f"  ✗ solo TradingView: {fmt_t(t)} {s}")
        for e in only_py:
            print(f"  ✗ solo Python:     {fmt_t(e['t'])} {e['side']} {e['kind']}")
        if len(hit) == len(exp) and not only_py:
            print("✓ Paridad total en este tramo.")


if __name__ == "__main__":
    main()
