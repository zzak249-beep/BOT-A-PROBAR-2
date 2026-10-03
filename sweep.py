"""
Barrido de variantes SIN engañarse: elige con el primer 70% del tiempo (entrenamiento) y enseña cómo le fue
a esa misma variante en el último 30% (prueba), que no se usó para elegir.

  python sweep.py --modo entradas --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT
  python sweep.py --modo salidas  --symbols ...      (TP2 × altura, trailing tras TP1, salida por tiempo)

Probar muchas variantes es hacer muchas apuestas: alguna sale bien por azar. Por eso:
  · la columna que manda es la de PRUEBA, no la de entrenamiento
  · el umbral de t se corrige por el número de variantes (Bonferroni)
  · si la mejor en entrenamiento se hunde en prueba, no hay nada que elegir
  · entradas y salidas se barren POR SEPARADO (48 + 27 pruebas, no 1.296)
"""
import argparse
import itertools
from statistics import NormalDist
from types import SimpleNamespace

import config as C
from backtest import TIMELINES, load_symbol, metrics
from strategy import apply_breadth, exit_variant, select_trades


def cfg_with(**kw):
    base = {k: getattr(C, k) for k in dir(C) if k.isupper()}
    base.update(kw)
    return SimpleNamespace(**base)


def table(rows, n_tests, head):
    rows.sort(key=lambda r: (r[1]["avg"] if r[1]["n"] >= 15 else -9), reverse=True)
    crit = NormalDist().inv_cdf(1 - 0.025 / n_tests)
    print(f"\n{n_tests} variantes · ordenadas por ENTRENAMIENTO (≥15 ops) · t crítico Bonferroni {crit:.2f}")
    print(f"{head} | {'entren. n':>9} {'media':>7} | {'PRUEBA n':>8} {'media':>7} {'PF':>5} | {'total t':>7}")
    for label, a, b, t in rows[:20]:
        flag = " ✓" if t["t"] >= crit and b["avg"] > 0 and b["n"] >= 15 else ""
        print(f"{label} | {a['n']:>9} {a['avg']:+7.3f} | {b['n']:>8} {b['avg']:+7.3f} {b['pf']:5.2f} | {t['t']:+7.2f}{flag}")
    best = rows[0]
    print(f"\nMejor en entrenamiento: {best[0].strip()} → prueba {best[2]['n']} ops, media {best[2]['avg']:+.3f}R")
    if best[2]["n"] < 15:
        print(f"⚠ Solo {best[2]['n']} operaciones en la PRUEBA: muestra insuficiente para concluir nada (mínimo 15, mejor 30+).")
    elif best[2]["avg"] <= 0:
        print("⚠ La mejor en entrenamiento NO aguanta en prueba: no hay variante que elegir con estos datos.")
    elif best[3]["t"] < crit:
        print(f"⚠ Aguanta en prueba pero t={best[3]['t']:.2f} < {crit:.2f}: compatible con azar tras {n_tests} pruebas.")
    else:
        print("✓ Aguanta en prueba y supera Bonferroni. Aun así: confírmalo en SIGNAL antes de dinero real.")


def split(tr, cutoff):
    a = [x["r"] for x in tr if x["open_t"] < cutoff]
    b = [x["r"] for x in tr if x["open_t"] >= cutoff]
    return metrics(a), metrics(b), metrics(a + b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modo", default="entradas", choices=["entradas", "salidas", "edge"])
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT")
    ap.add_argument("--tf", default=C.TIMEFRAME)
    ap.add_argument("--days", type=int, default=240)
    ap.add_argument("--warmup", type=int, default=400)
    args = ap.parse_args()
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]  # con guion → datos de BingX (TradFi)
    scan_cfg = cfg_with(TREND_FILTER="aviso", CONTEXT_FILTER="aviso", BTC_FILTER="aviso")

    if args.modo == "edge":
        RULES = ["", "squeeze<0.7", "squeeze<0.85", "squeeze>1.0", "er<0.2", "er>0.2", "er>0.35", "dry<0.8", "dry<1.0",
                 "dry>1.0", "clv>0", "clv>0.3", "clv<0.3", "body<1.0", "wick_exc>0.25", "wick_exc>0.5", "vol_exc>1.0",
                 "vol_exc>1.5", "vol_exc<2.0", "risk_atr>1.0", "risk_atr>1.5", "risk_atr<4.0", "session!=Asia",
                 "session!=Europa", "session!=EEUU", "session!=Noche",
                 "vr<0.9", "vr<1.0", "vr>1.0", "atr_pct<75", "atr_pct>25", "depth<1.0", "depth>0.3", "confirm<12",
                 "confirm>3", "avwap>-0.5", "avwap>0", "fund_h>2"]
        cands = [c for s in syms for c in load_symbol(s, args.tf, args.days, args.warmup, C.ENTRY_STRICTNESS, scan_cfg)]
        apply_breadth(cands, TIMELINES)
        print(f"{C.ENTRY_STRICTNESS}: {len(cands)} entradas del motor · filtros base = los de config (EMA {C.TREND_FILTER}...)")
        times = sorted(c["open_t"] for c in cands) or [0]
        cutoff = times[0] + (times[-1] - times[0]) * 0.7
        rows = []
        for rule in RULES:
            cfg = cfg_with(EDGE_FILTER="bloquea" if rule else "off", EDGE_RULES=rule)
            a, b, t = split(select_trades(cands, cfg), cutoff)
            rows.append((f"{rule or '(sin regla: base)':<20}", a, b, t))
        table(rows, len(RULES) - 1, f"{'regla edge (cada una sola)':<20}")
        print("Una regla sola, no combinaciones: combinarlas multiplicaría las pruebas. Si ninguna pasa Bonferroni, "
              "se deja EDGE_FILTER=off (es el resultado honesto más probable).")
        return
    if args.modo == "entradas":
        cands = {}
        for strict in ("Agresivo", "Estándar", "Conservador"):
            cands[strict] = [c for s in syms for c in load_symbol(s, args.tf, args.days, args.warmup, strict, scan_cfg)]
            apply_breadth(cands[strict], TIMELINES)
            print(f"{strict}: {len(cands[strict])} entradas del motor")
        times = sorted(c["open_t"] for c in cands["Agresivo"]) or [0]
        cutoff = times[0] + (times[-1] - times[0]) * 0.7
        grid = list(itertools.product(("Agresivo", "Estándar", "Conservador"), ("off", "bloquea"), ("off", "bloquea"),
                                      (0.0, 1.0, 1.5, 2.0)))
        rows = []
        for strict, trend, ctx, rr in grid:
            cfg = cfg_with(TREND_FILTER=trend, CONTEXT_FILTER=ctx, BTC_FILTER="aviso", MIN_RR=rr)
            a, b, t = split(select_trades(cands[strict], cfg), cutoff)
            rows.append((f"{strict:<12}{trend:<9}{ctx:<9}{rr:>4.1f}", a, b, t))
        table(rows, len(grid), f"{'exigencia':<12}{'EMA':<9}{'ctx':<9}{'RR':>4}")
    else:
        grid = [exit_variant(C, TP2_MULT=m, TRAIL_ATR=tr, TIME_STOP_BARS=ts)
                for m, tr, ts in itertools.product((1.0, 1.5, 2.0), (0.0, 1.5, 3.0), (0, 32, 96))]
        cands = [c for s in syms for c in load_symbol(s, args.tf, args.days, args.warmup, C.ENTRY_STRICTNESS,
                                                      scan_cfg, exits=grid)]
        apply_breadth(cands, TIMELINES)
        print(f"{C.ENTRY_STRICTNESS}: {len(cands)} entradas del motor · filtros de entrada los de config")
        times = sorted(c["open_t"] for c in cands) or [0]
        cutoff = times[0] + (times[-1] - times[0]) * 0.7
        cfg = cfg_with()
        rows = []
        for key in grid:
            a, b, t = split(select_trades(cands, cfg, key), cutoff)
            rows.append((f"TP2×{key[0]:<4} trailing {key[1] or 'off':<4} tiempo {key[2] or 'off':<4}", a, b, t))
        table(rows, len(grid), f"{'variante de salida':<40}")


if __name__ == "__main__":
    main()
