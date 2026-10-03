"""
Backtest local con el MISMO motor, plan, filtros y gestión que el bot.

  python backtest.py --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT --tf 15m --days 180
  python backtest.py --symbols BTCUSDT,ETHUSDT --tf 1h --days 365 --strict Conservador --context-filter bloquea

Datos: endpoint público de Binance Futures (sin key), cacheados en ./cache.
Sin mirar el futuro: entra al cierre de la vela que valida la entrada; el contexto (CONTEXT_TF) y la EMA
solo ven velas superiores YA CERRADAS. Gestión: TP1 parcial + SL a BE, resto TP2; si una vela toca SL y TP
cuenta el SL; descuenta comisión + deslizamiento por lado en R.
"""
import argparse
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from statistics import NormalDist

import requests

import config as C
from edge import bootstrap_ci, mc_drawdown, throttled_curve
from strategy import FAIL_KIND, META, MetaModel, apply_breadth, scan_candidates, select_trades

TIMELINES = {}
COVERAGE = {}
_WARNED = set()

BINANCE = "https://fapi.binance.com/fapi/v1/klines"
BINGX = "https://open-api.bingx.com/openApi/swap/v3/quote/klines"
SOURCE = "auto"  # auto: BingX para TradFi (NC...-USDT) y símbolos con guion; Binance para el resto


def use_bingx(symbol):
    return SOURCE == "bingx" or (SOURCE == "auto" and "-" in symbol)


def _get_bingx(symbol, tf, end_ms):
    r = requests.get(BINGX, params={"symbol": symbol, "interval": tf, "limit": 1440, "endTime": end_ms}, timeout=20)
    r.raise_for_status()
    out = []
    for k in r.json().get("data") or []:
        if isinstance(k, dict):
            out.append([int(k["time"]), float(k["open"]), float(k["high"]), float(k["low"]), float(k["close"]),
                        float(k.get("volume", 0))])
        else:
            out.append([int(k[0])] + [float(v) for v in k[1:6]])
    return sorted(out)
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


def fetch(symbol, tf, start_ms, end_ms):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{'bx_' if use_bingx(symbol) else 'v4_'}{symbol}_{tf}_{start_ms // 86400000}_{end_ms // 3600000}.csv")
    if os.path.exists(path):
        with open(path) as f:
            return [[int(x[0])] + [float(v) for v in x[1:]] for x in (l.strip().split(",") for l in f) if x[0]]
    rows, cur = [], start_ms
    if use_bingx(symbol):  # BingX pagina hacia atrás con endTime
        end = end_ms
        while end > start_ms:
            chunk = [k for k in _get_bingx(symbol, tf, end) if k[0] >= start_ms]
            if not chunk or chunk[0][0] >= end:
                break
            rows = chunk + rows
            end = chunk[0][0] - 1
            time.sleep(0.15)
        rows.sort()
        cur = end_ms
    while cur < end_ms:
        r = requests.get(BINANCE, params={"symbol": symbol, "interval": tf, "startTime": cur, "endTime": end_ms,
                                          "limit": 1500}, timeout=20)
        r.raise_for_status()
        k = r.json()
        if not k:
            break
        # col 9 de Binance = volumen comprado por agresores (taker buy): base del flujo de órdenes
        rows += [[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]), float(x[9])] for x in k]
        cur = k[-1][0] + 1
        time.sleep(0.15)
    out, seen = [], set()
    for x in rows:
        if x[0] not in seen:
            seen.add(x[0])
            out.append(x)
    with open(path, "w") as f:
        f.write("\n".join(",".join(str(v) for v in x) for x in out))
    return out


def tick_of(rows):
    dec = 0
    for r in rows[-300:]:
        for v in r[1:5]:
            s = f"{v:.10f}".rstrip("0")
            dec = max(dec, len(s.split(".")[1]) if "." in s else 0)
    return 10 ** (-min(dec, 8))


def htf_ema_series(rows, n, ms):
    out, e, closes, k = [], None, [], 2.0 / (n + 1)
    for t, o, h, l, c, v, *_ in rows:
        closes.append(c)
        if len(closes) == n:
            e = sum(closes) / n
        elif len(closes) > n:
            e = c * k + e * (1 - k)
        out.append((t + ms, e if e is not None else float("nan")))
    return out


_BTC_CACHE = {}


def btc_rows(cfg, start, end):
    """Velas de BTC en CONTEXT_TF (una sola descarga por ejecución) para el contexto 'estructura de BTC'."""
    if not cfg.CONTEXT_TF:
        return None
    key = (cfg.CONTEXT_TF, start, end)
    if key not in _BTC_CACHE:
        ms = C.tf_seconds(cfg.CONTEXT_TF) * 1000
        sym = "BTC-USDT" if SOURCE == "bingx" else "BTCUSDT"
        try:
            _BTC_CACHE[key] = fetch(sym, cfg.CONTEXT_TF, start - C.CONTEXT_WARMUP * ms, end)
        except requests.RequestException as e:
            print(f"BTC contexto: {e}")
            _BTC_CACHE[key] = None
    return _BTC_CACHE[key]


def load_symbol(sym, tf, days, warmup, strict, cfg, exits=None):
    tf_s = C.tf_seconds(tf)
    end = int(time.time() * 1000) // 3600000 * 3600000
    start = end - days * 86400000 - warmup * tf_s * 1000
    rows = [r for r in fetch(sym, tf, start, end) if r[0] + tf_s * 1000 <= end]
    if len(rows) < warmup + 50:
        print(f"{sym}: pocas velas ({len(rows)})")
        return []
    got = (rows[-1][0] - rows[0][0]) / 86400000 - warmup * tf_s / 86400
    COVERAGE[(sym, tf)] = (rows[0][0], rows[-1][0], got)
    if got < days * 0.8 and (sym, tf) not in _WARNED:
        _WARNED.add((sym, tf))
        print(f"⚠ {sym}: solo hay {got:.0f} días útiles de {tf} (pedidos {days})"
              + (" — BingX guarda poco histórico en TF bajos; con Binance (región Europa) o 1h hay más"
                 if use_bingx(sym) else ""))
    ema = None
    if cfg.TREND_FILTER != "off":
        ms = C.tf_seconds(cfg.TREND_TF) * 1000
        ema = htf_ema_series(fetch(sym, cfg.TREND_TF, start - cfg.TREND_EMA * 4 * ms, end), cfg.TREND_EMA, ms)
    ctx_rows, ctx_s = None, None
    if cfg.CONTEXT_TF and C.tf_seconds(cfg.CONTEXT_TF) > tf_s:
        ctx_s = C.tf_seconds(cfg.CONTEXT_TF)
        ctx_rows = fetch(sym, cfg.CONTEXT_TF, start - C.CONTEXT_WARMUP * ctx_s * 1000, end)
    from universe import is_tradfi
    tradfi = is_tradfi(sym)
    btc = None if (tradfi or sym.replace("-", "").startswith("BTCUSDT") or not ctx_s) else btc_rows(cfg, start, end)
    tl = ([], [])
    out = scan_candidates(rows, tf_s, tick_of(rows), strict, cfg, warmup, ema, ctx_rows, ctx_s, sym,
                          range_effort=tradfi and cfg.TRADFI_EFFORT == "rango", btc_rows=btc, exits=exits, timeline=tl)
    TIMELINES[sym] = tl
    return out


def bucket_n(x, edges, labels):
    return "sin dato" if x is None else bucket(x, edges, labels)


def bucket(x, edges, labels):
    for e, lab in zip(edges, labels):
        if x < e:
            return lab
    return labels[-1]


def metrics(rs):
    n = len(rs)
    if n == 0:
        return {"n": 0, "wr": 0, "avg": 0, "tot": 0, "pf": 0, "t": 0, "dd": 0, "streak": 0}
    gw, gl = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    mean = sum(rs) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1)) if n > 1 else 0
    eq = peak = dd = 0.0
    streak = worst = 0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        streak = streak + 1 if r <= 0 else 0
        worst = max(worst, streak)
    return {"n": n, "wr": sum(1 for r in rs if r > 0) * 100 / n, "avg": mean, "tot": sum(rs),
            "pf": gw / gl if gl else float("inf"), "t": mean / (sd / math.sqrt(n)) if sd > 0 else 0,
            "dd": dd, "streak": worst}


def line(m):
    return (f"{m['n']:>4} ops  {m['wr']:5.1f}%  media {m['avg']:+.3f}R  total {m['tot']:+7.2f}R  "
            f"PF {m['pf']:.2f}  t {m['t']:+.2f}")


def report(trades, n_tests):
    if not trades:
        print("\nSin operaciones. Prueba más días/símbolos, otro TF o exigencia Agresivo.")
        return
    trades.sort(key=lambda x: x["open_t"])
    m = metrics([x["r"] for x in trades])
    print("\n══════ RESULTADO ══════")
    print("TOTAL        " + line(m) + f"  peor racha {m['streak']}  DD {m['dd']:.2f}R")
    cut = int(len(trades) * 0.7)
    if len(trades) >= 20:
        a, b = metrics([x["r"] for x in trades[:cut]]), metrics([x["r"] for x in trades[cut:]])
        print("primer 70%   " + line(a))
        print("último 30%   " + line(b))
        if a["avg"] > 0 >= b["avg"]:
            print("  ⚠ gana en la primera parte y pierde en la última: firma típica del sobreajuste o del cambio de régimen")
    crit = NormalDist().inv_cdf(1 - 0.025 / max(n_tests, 1))
    verdict = ("pasa el umbral de data-snooping (t≥3)" if m["t"] >= 3 else
               "solo el umbral clásico (t≥2)" if m["t"] >= 2 else "NO distinguible de cero")
    print(f"t {m['t']:+.2f} → {verdict} · Bonferroni con {n_tests} pruebas: {crit:.2f} "
          f"({'pasa' if m['t'] >= crit else 'no pasa'})")
    rs_all = [x["r"] for x in trades]
    ci = bootstrap_ci(rs_all)
    if ci:
        print(f"IC 95% de la media (bootstrap): [{ci[0]:+.3f}R, {ci[1]:+.3f}R] · P(media>0) = {ci[2]:.0%}"
              + ("  ← el intervalo cruza 0: no se puede descartar que sea suerte" if ci[0] <= 0 <= ci[1] else ""))
    if len(rs_all) >= 10:
        print("Drawdown esperable (Monte Carlo, riesgo fijo por operación) — mediana / peor 5% / P(DD≥25%):")
        for rp in sorted({0.25, 0.5, 1.0, C.RISK_PCT}):
            mc = mc_drawdown(rs_all, rp)
            print(f"  riesgo {rp:>4}%  →  {mc[0]:5.1f}%  /  {mc[1]:5.1f}%  /  {mc[2]:.1%}")
    n_thr = C.THROTTLE_N or 5
    tot, dd, cut = throttled_curve([x["r"] for x in trades], n_thr, C.THROTTLE_MULT)
    print(f"acelerador de capital (últimas {n_thr} ops < 0 → riesgo ×{C.THROTTLE_MULT}): total {tot:+.2f}R · DD {dd:.2f}R "
          f"· {cut} ops recortadas   (sin acelerador: {m['tot']:+.2f}R · DD {m['dd']:.2f}R)")
    groups = defaultdict(lambda: defaultdict(list))
    for x in trades:
        groups["lado"][x["side"]].append(x["r"])
        groups["tipo de entrada"][x["kind"]].append(x["r"])
        groups["contexto " + (C.CONTEXT_TF or "-")][x.get("ctx_align", "-")].append(x["r"])
        groups["EMA " + C.TREND_TF]["en contra" if x.get("against_trend") else "a favor/neutral"].append(x["r"])
        groups["BTC " + (C.CONTEXT_TF or "-")][x.get("btc_align", "-")].append(x["r"])
        groups["cómo cerró"][x.get("reason", "-")].append(x["r"])
        groups["validación del indicador"][bucket(x["val"], (70, 85), ("<70", "70-84", "85+"))].append(x["r"])
        groups["R:R del plan"][bucket(x["rr"], (1.5, 2.5, 4), ("<1.5", "1.5-2.5", "2.5-4", "4+"))].append(x["r"])
        groups["altura del rango (ATR)"][bucket(x.get("range_atr", 0), (3, 6), ("<3", "3-6", "6+"))].append(x["r"])
        groups["duración Fase B (velas)"][bucket(x.get("b_bars", 0), (60, 150), ("<60", "60-149", "150+"))].append(x["r"])
        groups["flujo agresor últimas 10 velas"][bucket_n(x.get("flow"), (-0.05, 0.05), ("en contra", "neutro", "a favor"))].append(x["r"])
        groups["flujo agresor en el Spring/UTAD"][bucket_n(x.get("flow_exc"), (-0.1, 0.1), ("venta/compra absorbida", "neutro", "a favor"))].append(x["r"])
        groups["amplitud Wyckoff (resto de símbolos)"][x.get("breadth_align", "-")].append(x["r"])
        groups["[v5.1] squeeze ATR5/ATR50"][bucket_n(x.get("squeeze"), (0.7, 1.0), ("<0.7 comprimido", "0.7-1.0", ">1.0 agitado"))].append(x["r"])
        groups["[v5.1] eficiencia previa (ER 20)"][bucket_n(x.get("er"), (0.15, 0.35), ("<0.15 serrucho", "0.15-0.35", ">0.35 direccional"))].append(x["r"])
        groups["[v5.1] secado de volumen"][bucket_n(x.get("dry"), (0.8, 1.2), ("<0.8 seco", "0.8-1.2", ">1.2 activo"))].append(x["r"])
        groups["[v5.1] cierre vela de entrada"][bucket_n(x.get("clv"), (-0.3, 0.3), ("débil", "medio", "fuerte a favor"))].append(x["r"])
        groups["[v5.1] mecha rechazo Spring/UTAD"][bucket_n(x.get("wick_exc"), (0.25, 0.5), ("<25%", "25-50%", ">50%"))].append(x["r"])
        groups["[v5.1] volumen del Spring/UTAD"][bucket_n(x.get("vol_exc"), (1.0, 2.0), ("<1x", "1-2x", ">2x"))].append(x["r"])
        groups["[v5.2] razón de varianzas VR(4)"][bucket_n(x.get("vr"), (0.9, 1.1), ("<0.9 reversión", "0.9-1.1 azar", ">1.1 tendencia"))].append(x["r"])
        groups["[v5.2] percentil de volatilidad (ATR)"][bucket_n(x.get("atr_pct"), (25, 75), ("<25 baja", "25-75", ">75 alta"))].append(x["r"])
        groups["[v5.2] profundidad del Spring/UTAD (ATR)"][bucket_n(x.get("depth"), (0.3, 1.0), ("<0.3 superficial", "0.3-1.0", ">1.0 profundo"))].append(x["r"])
        groups["[v5.2] velas Spring→entrada"][bucket_n(x.get("confirm"), (4, 12), ("<4 rápida", "4-11", "12+ lenta"))].append(x["r"])
        groups["[v5.2] entrada vs VWAP anclado al clímax"][bucket_n(x.get("avwap"), (-0.5, 0.5), ("cara (> coste medio)", "en el coste", "barata (< coste medio)"))].append(x["r"])
        groups["[v5.2] horas hasta funding"][bucket_n(x.get("fund_h"), (2, 6), ("<2h", "2-6h", "6-8h"))].append(x["r"])
        groups["[v5.1] sesión UTC"][x.get("session") or "sin dato"].append(x["r"])
        groups["[v5.1] día de la semana"][x.get("dow") or "sin dato"].append(x["r"])
        groups["mes"][datetime.fromtimestamp(x["open_t"] / 1000, timezone.utc).strftime("%Y-%m")].append(x["r"])
        groups["símbolo"][x["symbol"]].append(x["r"])
    for g, d in groups.items():
        print(f"\n— por {g} —")
        for k in sorted(d):
            print(f"  {str(k):<16} " + line(metrics(d[k])))
    if m["n"] < 30:
        print(f"\n⚠ {m['n']} operaciones: un dibujo, no evidencia.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT")
    ap.add_argument("--tf", default=C.TIMEFRAME)
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--warmup", type=int, default=400)
    ap.add_argument("--strict", default=C.ENTRY_STRICTNESS)
    ap.add_argument("--trend", default=C.TREND_FILTER, help="off | aviso | bloquea")
    ap.add_argument("--context-tf", default=C.CONTEXT_TF)
    ap.add_argument("--context-filter", default=C.CONTEXT_FILTER, help="off | aviso | bloquea")
    ap.add_argument("--min-rr", type=float, default=C.MIN_RR)
    ap.add_argument("--tp2-mult", type=float, default=C.TP2_MULT)
    ap.add_argument("--trail-atr", type=float, default=C.TRAIL_ATR)
    ap.add_argument("--time-stop", type=int, default=C.TIME_STOP_BARS)
    ap.add_argument("--btc-filter", default=C.BTC_FILTER, help="off | aviso | bloquea")
    ap.add_argument("--fail", default=C.FAIL_TRADES, help="off | aviso | on (incluir las trampas en el resultado)")
    ap.add_argument("--breadth-filter", default=C.BREADTH_FILTER, help="off | aviso | bloquea")
    ap.add_argument("--meta-filter", default="off", help="off | bloquea (aplica meta_model.json)")
    ap.add_argument("--edge-rules", default=C.EDGE_RULES, help='v5.1: ej. "squeeze<0.8,clv>0" (AND); requiere --edge-filter bloquea')
    ap.add_argument("--edge-filter", default=C.EDGE_FILTER, help="off | bloquea")
    ap.add_argument("--source", default="auto", help="auto | binance | bingx (TradFi: usa BingX, p. ej. NCFXEUR2USD-USDT)")
    args = ap.parse_args()
    C.TREND_FILTER, C.CONTEXT_TF, C.CONTEXT_FILTER, C.MIN_RR = args.trend, args.context_tf, args.context_filter, args.min_rr
    C.TP2_MULT, C.TRAIL_ATR, C.TIME_STOP_BARS, C.BTC_FILTER = args.tp2_mult, args.trail_atr, args.time_stop, args.btc_filter
    C.FAIL_TRADES, C.BREADTH_FILTER, C.META_FILTER = args.fail, args.breadth_filter, args.meta_filter
    C.EDGE_FILTER, C.EDGE_RULES = args.edge_filter, args.edge_rules
    global SOURCE
    SOURCE = args.source
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if SOURCE == "binance":
        syms = [s.replace("-", "") for s in syms]
    elif SOURCE == "bingx":
        syms = [s if "-" in s else s.replace("USDT", "-USDT") for s in syms]
    print(f"Backtest {args.tf} · {args.days} días · exigencia {args.strict} · EMA {C.TREND_FILTER} · "
          f"contexto {C.CONTEXT_TF or '-'} {C.CONTEXT_FILTER} · BTC {C.BTC_FILTER} · R:R≥{C.MIN_RR}\n"
          f"salida: TP2×{C.TP2_MULT} · trailing {C.TRAIL_ATR or 'off'} · tiempo {C.TIME_STOP_BARS or 'off'}"
          f" · coste {C.FEE_PCT}+{C.SLIPPAGE_PCT}%/lado")
    if C.META_FILTER == "bloquea" and os.path.exists(C.META_MODEL):
        META["model"] = MetaModel.load(C.META_MODEL)
        print(f"Meta-modelo {C.META_MODEL} activo (umbral {META['model'].thr:.2f}) — ojo: solo es honesto en datos posteriores a su entrenamiento")
    allc = []
    for s in syms:
        try:
            allc += load_symbol(s, args.tf, args.days, args.warmup, args.strict, C)
        except requests.RequestException as e:
            print(f"{s}: error de datos {e}")
    apply_breadth(allc, TIMELINES)  # necesita a todos los símbolos cargados
    trades = []
    for s in syms:
        mine = [c for c in allc if c["symbol"] == s]
        tr = select_trades(mine, C)
        n_ent = sum(1 for c in mine if c["kind"] != FAIL_KIND)
        print(f"{s:<14} {n_ent:>3} entradas del motor → {len(tr):>3} operadas  {sum(x['r'] for x in tr):+.2f}R")
        trades += tr
    report(trades, len(syms))
    fails = select_trades(allc, C, which="fail")
    print("\n══════ IDEA: TRAMPA (operar contra la estructura rota) ══════")
    if FAIL_KIND in {t["kind"] for t in trades}:
        print("(FAIL_TRADES=on: estas operaciones ya están incluidas arriba)")
    if fails:
        rs = [x["r"] for x in fails]
        print("trampas       " + line(metrics(rs)))
        withe = [x["r"] for x in fails if x.get("had_entry")]
        if withe:
            print("  ·con entrada " + line(metrics(withe)) + "  (la estructura llegó a dar señal y falló)")
    else:
        print("ninguna estructura rota en Fase C/D en el periodo")


if __name__ == "__main__":
    sys.exit(main())
