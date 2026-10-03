"""
Prueba sin red: genera ciclos sintéticos tendencia → clímax → rango → spring/UTAD → ruptura
y comprueba que el motor corre sin errores, recorre las fases y produce entradas con plan válido.

  python test_engine.py
"""
import random
from collections import Counter

import config as C
from strategy import TradeSim, build_signal, filters
from wyckoff_engine import PHASE_NAMES, WyckoffEngine


def synth(seed=1, cycles=12):
    rnd = random.Random(seed)
    p, t, rows = 100.0, 0, []

    def bar(drift, vol, volm=1.0, wide=1.0):
        nonlocal p, t
        o = p
        c = max(1.0, o * (1 + drift + rnd.gauss(0, vol)))
        span = abs(c - o) + o * vol * wide * rnd.uniform(0.3, 1.2)
        h = max(o, c) + span * rnd.uniform(0.1, 0.5)
        l = min(o, c) - span * rnd.uniform(0.1, 0.5)
        v = 1000 * volm * rnd.uniform(0.6, 1.4)
        rows.append([t, o, h, l, c, v])
        p = c
        t += 900_000

    for k in range(cycles):
        d = -1 if k % 2 == 0 else 1
        for _ in range(rnd.randint(60, 120)):  # tendencia previa
            bar(d * 0.003, 0.004)
        bar(d * 0.03, 0.006, volm=5, wide=4)  # clímax
        lo, hi = sorted([p, p * (1 - d * 0.05)])
        for _ in range(8):  # AR con esfuerzo: absorción de la parada
            bar(-d * 0.006, 0.003, volm=1.4)
        for _ in range(rnd.randint(70, 130)):  # rango con rebotes en los bordes
            target = lo if rnd.random() < 0.5 else hi
            bar((target - p) / p * 0.15, 0.004, volm=rnd.uniform(0.5, 0.9))
        edge = lo if rnd.random() < 0.5 else hi
        sgn = -1 if edge == lo else 1
        bar(sgn * 0.02, 0.003, volm=1.0)  # spring/UTAD
        bar(-sgn * 0.025, 0.003, volm=1.0)
        for _ in range(10):
            bar(-sgn * 0.002, 0.003, volm=0.6)
        for _ in range(rnd.randint(40, 80)):  # salida
            bar(-sgn * 0.004, 0.004, volm=1.5)
    return rows


def main():
    total_entries, trades, phases = 0, [], Counter()
    for seed in range(1, 7):
        rows = synth(seed)
        eng = WyckoffEngine(900, 0.0001, "Agresivo")
        sim = None
        for idx, (t, o, h, l, c, v) in enumerate(rows):
            if sim:
                r = sim.step(idx, h, l)
                if r is not None:
                    trades.append(r)
                    sim = None
            d = eng.update(t, o, h, l, c, v)
            phases[PHASE_NAMES[d["phase"]]] += 1
            if d["entry_now"]:
                total_entries += 1
                sig = build_signal(d, C, 0.0001)
                assert sig is not None, "entrada sin plan"
                if sig["side"] == "LONG":
                    assert sig["sl"] < sig["entry"] < sig["tp2"], sig
                else:
                    assert sig["sl"] > sig["entry"] > sig["tp2"], sig
                if sim is None and not filters(sig, C, 0):
                    sim = TradeSim(sig, idx, C.FEE_PCT)
        print(f"seed {seed}: {len(rows)} velas · {eng.status_text()} · resets {eng.rsCounts}")
    print("velas por fase:", dict(phases))
    print("entradas:", total_entries, "· operaciones simuladas:", len(trades),
          "· R total:", round(sum(trades), 2) if trades else 0)
    assert phases["B"] > 0, "nunca llegó a Fase B: revisar detección"
    print("OK")


if __name__ == "__main__":
    main()
