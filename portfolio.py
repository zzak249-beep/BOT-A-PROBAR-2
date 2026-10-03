"""
Cartera: lo que el backtest por símbolo NO muestra.

El backtest mide cada operación suelta (+R). El bot, en cambio, tiene topes (máx. posiciones a la vez, máx. en la
misma dirección, pérdida diaria, enfriamiento por símbolo) y arriesga un % del equity que se compone. Aquí se
pasan las operaciones por esos MISMOS topes y se calcula:
  · curva de capital real (compuesta) y caída máxima en %
  · simulación de Monte Carlo: mismas operaciones en otro orden → distribución de caídas máximas
  · RISK_PCT recomendado: el mayor riesgo cuyo percentil 95 de caída máxima no pasa de RISK_TARGET_DD %
"""
import random
from datetime import datetime, timezone


def _day(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def simulate(trades, cfg, risk_pct=None):
    """trades: dicts con open_t, close_t (ms), r, symbol, side. Devuelve resultado con los topes del bot aplicados."""
    risk = (cfg.RISK_PCT if risk_pct is None else risk_pct) / 100.0
    eq, peak, maxdd = 1.0, 1.0, 0.0
    curve, taken, skipped = [], [], {"tope de posiciones": 0, "misma dirección": 0, "pérdida diaria": 0,
                                     "enfriamiento": 0}
    open_, daily, last_sig = [], {}, {}
    cooldown = cfg.SIGNAL_COOLDOWN_MIN * 60_000

    def close_until(t):
        nonlocal eq, peak, maxdd
        for tr in sorted([x for x in open_ if x["close_t"] <= t], key=lambda x: x["close_t"]):
            open_.remove(tr)
            eq += tr["_stake"] * tr["r"]
            daily[_day(tr["close_t"])] = daily.get(_day(tr["close_t"]), 0.0) + tr["r"]
            peak = max(peak, eq)
            maxdd = max(maxdd, (peak - eq) / peak)
            curve.append((tr["close_t"], eq))

    for tr in sorted(trades, key=lambda x: x["open_t"]):
        t = tr["open_t"]
        close_until(t)
        sym = tr["symbol"]
        if t - last_sig.get(sym, -10**18) < cooldown:
            skipped["enfriamiento"] += 1
            continue
        last_sig[sym] = t
        if len(open_) >= cfg.MAX_CONCURRENT:
            skipped["tope de posiciones"] += 1
            continue
        if cfg.MAX_SAME_SIDE > 0 and sum(1 for o in open_ if o["side"] == tr["side"]) >= cfg.MAX_SAME_SIDE:
            skipped["misma dirección"] += 1
            continue
        if daily.get(_day(t), 0.0) <= -cfg.MAX_DAILY_LOSS_R:
            skipped["pérdida diaria"] += 1
            continue
        rec = dict(tr, _stake=eq * risk)  # se arriesga un % del equity realizado en el momento de abrir
        open_.append(rec)
        taken.append(rec)
    close_until(10**18)
    rs = [x["r"] for x in taken]
    streak = worst = 0
    for r in rs:
        streak = streak + 1 if r <= 0 else 0
        worst = max(worst, streak)
    return {"taken": taken, "n": len(taken), "equity": eq, "ret_pct": (eq - 1) * 100, "maxdd_pct": maxdd * 100,
            "streak": worst, "skipped": skipped, "curve": curve}


def mc_drawdown(rs, risk_pct, n_sims=3000, seed=11):
    """Percentiles de la caída máxima (%) al remuestrear el ORDEN de las operaciones (con reemplazo)."""
    if not rs:
        return {"p50": 0.0, "p95": 0.0, "ruin": 0.0}
    rnd = random.Random(seed)
    risk = risk_pct / 100.0
    dds, ruin = [], 0
    for _ in range(n_sims):
        eq = peak = 1.0
        dd = 0.0
        for _ in range(len(rs)):
            eq *= max(1.0 + risk * rnd.choice(rs), 0.0)
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak)
        dds.append(dd * 100)
        ruin += dd >= 0.5
    dds.sort()
    return {"p50": dds[len(dds) // 2], "p95": dds[int(len(dds) * 0.95)], "ruin": ruin / n_sims * 100}


def recommend_risk(rs, target_dd, grid=(0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0)):
    """Mayor riesgo de la rejilla cuyo p95 de caída máxima cabe en target_dd (None si ninguno)."""
    best = None
    for g in grid:
        if mc_drawdown(rs, g, n_sims=1500)["p95"] <= target_dd:
            best = g
    return best


def report(trades, cfg):
    if len(trades) < 20:
        print("\n(cartera: menos de 20 operaciones, no se simula)")
        return
    res = simulate(trades, cfg)
    print("\n══════ CARTERA (con los topes del bot) ══════")
    print(f"topes: máx {cfg.MAX_CONCURRENT} a la vez · misma dirección {cfg.MAX_SAME_SIDE or 'sin tope'} · "
          f"pérdida diaria {cfg.MAX_DAILY_LOSS_R}R · enfriamiento {cfg.SIGNAL_COOLDOWN_MIN} min · riesgo {cfg.RISK_PCT}%/op")
    sk = ", ".join(f"{k} {v}" for k, v in res["skipped"].items() if v) or "ninguna"
    print(f"operaciones: {len(trades)} del backtest → {res['n']} con los topes (saltadas: {sk})")
    rs = [x["r"] for x in res["taken"]]
    avg = sum(rs) / len(rs) if rs else 0
    print(f"media {avg:+.3f}R · rentabilidad compuesta {res['ret_pct']:+.1f}% · caída máxima {res['maxdd_pct']:.1f}% · "
          f"peor racha {res['streak']}")
    if res["n"] < 20:
        return
    mc = mc_drawdown(rs, cfg.RISK_PCT)
    print(f"Monte Carlo (mismas operaciones, otro orden): caída máx típica {mc['p50']:.1f}% · 95% de los casos "
          f"≤ {mc['p95']:.1f}% · probabilidad de perder la mitad {mc['ruin']:.1f}%")
    rec = recommend_risk(rs, cfg.RISK_TARGET_DD)
    if avg <= 0:
        print("→ La media es ≤ 0: ningún riesgo es adecuado hasta que la estrategia gane en prueba.")
    elif rec is None:
        print(f"→ Ni 0.1% por operación cabe en una caída del {cfg.RISK_TARGET_DD:.0f}%: la racha mala es demasiado larga.")
    else:
        flag = "" if rec >= cfg.RISK_PCT else f"  ⚠ tu RISK_PCT={cfg.RISK_PCT}% es mayor"
        print(f"→ RISK_PCT máximo para que el 95% de los casos no pase de {cfg.RISK_TARGET_DD:.0f}% de caída: "
              f"{rec}%{flag}")
        print("  (con una media medida sobre pocas operaciones, usa la MITAD: el edge estimado suele ser optimista)")
