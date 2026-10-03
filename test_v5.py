"""
Pruebas v5 sin red: guarda del libro, cambio de stop sin hueco, caché de motores (idéntica a no reiniciar),
cartera con topes + Monte Carlo y paridad CSV.   python test_v5.py
"""
import csv
import os
import pickle
import random
import sys
import tempfile
from types import SimpleNamespace

import config as C
import portfolio
from bingx import BingXError, walk_book
from parity import compare, load_csv, run_engine, tick_of
from test_engine import synth
from wyckoff_engine import WyckoffEngine


def test_walk_book():
    book = [(100.0, 1.0), (100.5, 2.0), (101.0, 5.0)]
    assert walk_book(book, 0.5) == 100.0
    assert abs(walk_book(book, 2.0) - 100.25) < 1e-9
    assert abs(walk_book(book, 3.0) - (100.0 * 1 + 100.5 * 2) / 3) < 1e-9
    assert walk_book(book, 9.0) is None  # no alcanza: libro fino
    print("walk_book OK")


class FakeEx:
    """Mini-exchange: guarda stops vivos; puede rechazar la colocación."""
    def __init__(self):
        self.stops, self.n, self.fail_place, self.log = {}, 0, False, []

    def stop_orders(self, sym, long):
        return [{"orderId": k, "type": "STOP_MARKET"} for k in self.stops]

    def exit_order(self, sym, long, kind, qty, px):
        if self.fail_place:
            raise BingXError("stop would trigger immediately")
        self.n += 1
        self.stops[str(self.n)] = (qty, px)
        self.log.append(("place", str(self.n)))
        return str(self.n)

    def cancel(self, sym, oid):
        self.stops.pop(str(oid), None)
        self.log.append(("cancel", str(oid)))
        return True


def test_replace_stop():
    import main  # noqa: F401  (importa el Bot)
    bot = main.Bot.__new__(main.Bot)
    bot.ex = FakeEx()
    bot.ex.stops = {"viejo": (10.0, 95.0)}
    rec = {"side": "LONG", "sl_id": "viejo"}
    assert bot.replace_stop("X-USDT", rec, 5.0, 100.0)
    # el nuevo se coloca ANTES de cancelar el viejo: nunca hay una ventana sin stop
    assert bot.ex.log[0][0] == "place" and bot.ex.log[1] == ("cancel", "viejo"), bot.ex.log
    assert list(bot.ex.stops.values()) == [(5.0, 100.0)]
    bot.ex.fail_place = True
    assert not bot.replace_stop("X-USDT", rec, 5.0, 101.0)
    assert len(bot.ex.stops) == 1  # el viejo se queda si el nuevo no se puede colocar
    print("replace_stop OK (nuevo antes que cancelar; si falla, el viejo sigue)")


def test_book_guard():
    import main
    bot = main.Bot.__new__(main.Bot)
    bids = [(99.9, 1.0), (99.8, 1.0)]
    asks = [(100.1, 1.0), (100.6, 2.0), (101.5, 9.0)]
    bot.ex = SimpleNamespace(depth=lambda s, n: (bids, asks))
    ok, _ = bot.book_guard("X", True, 0.5, risk=2.0)           # mid 100, coste 0.1 → 0.05R
    assert ok
    ok, why = bot.book_guard("X", True, 3.0, risk=2.0)          # recorre varios niveles → caro
    assert not ok and "impacto" in why, why
    ok, why = bot.book_guard("X", True, 50.0, risk=2.0)
    assert not ok and "profundidad" in why, why
    def boom(*a):
        raise BingXError("sin libro")
    bot.ex = SimpleNamespace(depth=boom)
    assert bot.book_guard("X", True, 1.0, 1.0)[0]               # sin libro no bloquea
    print("book_guard OK")


def test_cache_roundtrip():
    rows = synth(2, cycles=6)
    cut = len(rows) // 2
    a = WyckoffEngine(900, 0.0001, "Agresivo", keep_bars=400)
    outs_a = []
    for r in rows:
        d = a.update(*r)
        outs_a.append((d["phase"], d["entry_now"], d["sig"], round(d["conf"], 6)))
    b = WyckoffEngine(900, 0.0001, "Agresivo", keep_bars=400)
    for r in rows[:cut]:
        b.update(*r)
    b = pickle.loads(pickle.dumps(b, protocol=pickle.HIGHEST_PROTOCOL))  # "reinicio" del bot
    outs_b = []
    for r in rows[cut:]:
        d = b.update(*r)
        outs_b.append((d["phase"], d["entry_now"], d["sig"], round(d["conf"], 6)))
    assert outs_a[cut:] == outs_b, "el motor restaurado de la caché difiere del continuo"
    print(f"caché OK: {len(rows) - cut} velas tras el reinicio idénticas al motor continuo")


def cfg(**kw):
    base = dict(RISK_PCT=0.5, MAX_CONCURRENT=2, MAX_SAME_SIDE=0, MAX_DAILY_LOSS_R=3.0, SIGNAL_COOLDOWN_MIN=60,
                RISK_TARGET_DD=20.0)
    base.update(kw)
    return SimpleNamespace(**base)


def trade(open_h, dur_h, r, sym="A", side="LONG"):
    return {"open_t": int(open_h * 3.6e6), "close_t": int((open_h + dur_h) * 3.6e6), "r": r, "symbol": sym, "side": side}


def test_portfolio():
    # 3 abiertas a la vez con tope 2 → la tercera se salta
    tr = [trade(0, 10, 1.0, "A"), trade(1, 10, 1.0, "B"), trade(2, 10, 1.0, "C")]
    res = portfolio.simulate(tr, cfg())
    assert res["n"] == 2 and res["skipped"]["tope de posiciones"] == 1
    # misma dirección
    res = portfolio.simulate(tr, cfg(MAX_CONCURRENT=5, MAX_SAME_SIDE=1))
    assert res["n"] == 1 and res["skipped"]["misma dirección"] == 2
    # pérdida diaria: 3 pérdidas de -1R el mismo día bloquean la siguiente
    tr = [trade(i, 0.5, -1.0, f"S{i}") for i in range(4)]
    res = portfolio.simulate(tr, cfg(MAX_CONCURRENT=5))
    assert res["n"] == 3 and res["skipped"]["pérdida diaria"] == 1, res["skipped"]
    # enfriamiento por símbolo
    tr = [trade(0, 1, 1.0, "A"), trade(0.5, 1, 1.0, "A")]
    assert portfolio.simulate(tr, cfg())["skipped"]["enfriamiento"] == 1
    # capital: +1R a 1% de riesgo ≈ +1%, y compone
    res = portfolio.simulate([trade(0, 1, 1.0, "A"), trade(5, 1, 1.0, "B")], cfg(RISK_PCT=1.0))
    assert abs(res["equity"] - 1.01 * 1.01) < 1e-9, res["equity"]
    # caída máxima
    res = portfolio.simulate([trade(0, 1, -1.0, "A"), trade(5, 1, -1.0, "B")], cfg(RISK_PCT=10.0, MAX_DAILY_LOSS_R=9))
    assert abs(res["maxdd_pct"] - 19.0) < 1e-6, res["maxdd_pct"]
    print("cartera OK (topes, pérdida diaria, enfriamiento, composición, caída)")


def test_montecarlo():
    rnd = random.Random(5)
    good = [1.8 if rnd.random() < 0.5 else -1.0 for _ in range(200)]    # media +0.4R
    bad = [1.0 if rnd.random() < 0.4 else -1.0 for _ in range(200)]     # media -0.2R
    d1, d2 = portfolio.mc_drawdown(good, 0.5), portfolio.mc_drawdown(bad, 0.5)
    assert d1["p95"] < d2["p95"], (d1, d2)
    assert portfolio.mc_drawdown(good, 2.0)["p95"] > portfolio.mc_drawdown(good, 0.5)["p95"]  # más riesgo, más caída
    rec = portfolio.recommend_risk(good, 20.0)
    assert rec is not None and portfolio.mc_drawdown(good, rec, 1500)["p95"] <= 20.0
    print(f"Monte Carlo OK (bueno p95 {d1['p95']:.1f}% · malo {d2['p95']:.1f}% · riesgo recomendado {rec}%)")


def test_parity_csv():
    rows = synth(3, cycles=6)
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "tv.csv")
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time", "open", "high", "low", "close", "Volume"])
            for t, o, h, l, c, v in rows:
                w.writerow([t // 1000, o, h, l, c, v])      # formato de exportación: segundos unix
        back = load_csv(p)
        assert len(back) == len(rows) and back[10][0] == rows[10][0]
        ents, _ = run_engine(back, 900, tick_of(back), "Agresivo", warmup=0)
        assert ents, "sin entradas en el ciclo sintético"
        exp = [{"time": str(e["t"] // 1000), "side": e["side"]} for e in ents]
        hit, only_tv, only_py = compare(ents, exp, 900_000)
        assert len(hit) == len(ents) and not only_tv and not only_py
        # una entrada que TradingView no tiene → aparece como "solo Python"
        hit, only_tv, only_py = compare(ents, exp[1:], 900_000)
        assert len(only_py) == 1 and not only_tv
        # una que Python no tiene → "solo TradingView"
        exp2 = exp + [{"time": str(rows[5][0] // 1000), "side": "LONG"}]
        hit, only_tv, only_py = compare(ents, exp2, 900_000)
        assert len(only_tv) == 1
    print(f"paridad CSV OK ({len(ents)} entradas, lectura de la exportación de TradingView y comparación)")


if __name__ == "__main__":
    os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
    for fn in (test_walk_book, test_replace_stop, test_book_guard, test_cache_roundtrip, test_portfolio,
               test_montecarlo, test_parity_csv):
        fn()
    print("\nTODO OK")
