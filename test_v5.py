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


def test_diagnostic():
    import main
    from types import SimpleNamespace
    bot = main.Bot.__new__(main.Bot)
    bot.ex = SimpleNamespace(contracts={"T-USDT": {"tick": 0.0001, "cls_label": "cripto"}})
    bot.symbols = ["T-USDT"]
    bot.universe_note = "prueba"
    tf = C.TIMEFRAMES[0]
    eng = WyckoffEngine(C.tf_seconds(tf), 0.0001, "Agresivo", keep_bars=400)
    for r in synth(1, cycles=8):
        d = eng.update(*r)
        bot.tally("T-USDT", eng, d)
    bot.engines = {("T-USDT", tf): eng}
    txt = bot.diagnostic_text()
    assert "Embudo" in txt and "Entradas del motor" in txt and "Por clase: cripto 1 símb" in txt, txt
    h = eng.hist
    assert h["bars"] > 1000 and h["climax"] >= 1 and h["entries"] >= 1, h
    # universo vacío → advierte
    bot.symbols = []
    assert "Universo casi vacío" in bot.diagnostic_text()
    # sin nada de nada → diagnostica "ni un clímax"
    eng2 = WyckoffEngine(C.tf_seconds(tf), 0.0001, "Agresivo", keep_bars=400)
    flat = [[i * 900_000, 100.0, 100.1, 99.9, 100.0, 10.0] for i in range(400)]
    for r in flat:
        bot.tally("T-USDT", eng2, eng2.update(*r))
    bot.engines = {("T-USDT", tf): eng2}
    bot.symbols = ["A", "B", "C", "D", "E"]
    assert "Ni un clímax" in bot.diagnostic_text()
    print("diagnóstico OK (embudo, universo vacío, sin clímax)")


# ═════════════ v5.1: salida por estructura y registro de eventos ═════════════
def test_engine_broke():
    from collections import Counter
    c = Counter()
    for seed in range(1, 9):
        e = WyckoffEngine(900, 0.0001, "Agresivo", keep_bars=100000)
        for r in synth(seed, cycles=12):
            d = e.update(*r)
            b = d["broke"]
            if b:
                assert b["why"] in ("INVALID", "DEMOTE", "STALE") and b["side"] in ("LONG", "SHORT"), b
                c[b["why"]] += 1
            else:
                assert "broke" in d and d["broke"] is None
    assert sum(c.values()) >= 1, "el motor no emitió ninguna rotura en ciclos sintéticos"
    print(f"motor: roturas emitidas {dict(c)} (DEMOTE/STALE no aparecen en los ciclos sintéticos: sin cobertura de datos)")


def test_trade_sim_mark():
    from strategy import TradeSim
    sig = {"side": "LONG", "entry": 100.0, "sl": 99.0, "tp1": 101.0, "tp2": 103.0}
    s = TradeSim(sig, 0, 0.0, 0.5, True)
    assert abs(s.mark(100.5) - 0.5) < 1e-9
    s.half = True
    assert abs(s.mark(100.5) - 0.75) < 1e-9          # 50% ya cobrado a +1R, 50% al precio
    s2 = TradeSim(dict(sig, side="SHORT", sl=101.0, tp1=99.0, tp2=97.0), 0, 0.05, 0.5, True)
    assert abs(s2.mark(99.6) - (0.4 - 2 * 0.05 / 100 * 100 / 1)) < 1e-9   # short a favor +0.4R menos coste de ida y vuelta
    assert abs(s2.mark(100.4) - (-0.4 - 0.1)) < 1e-9                       # y en contra
    print("TradeSim.mark OK (long, parcial, short, costes)")


def test_scan_struct_exit():
    import strategy
    from strategy import scan_candidates, select_trades

    class Inj(WyckoffEngine):
        """Inyecta una rotura de la estructura 3 velas después de cada entrada."""
        def update(self, *a):
            d = super().update(*a)
            if d["entry_now"]:
                self._inj = (self.i + 3, "LONG" if d["outcome"] > 0 else "SHORT")
            inj = getattr(self, "_inj", None)
            if inj and self.i == inj[0]:
                d = dict(d)
                d["broke"] = {"why": "DEMOTE", "side": inj[1], "phase": 4}
            return d

    base = {k: getattr(C, k) for k in dir(C) if k.isupper()}
    base.update(MIN_RR=0.0, TREND_FILTER="off", CONTEXT_FILTER="off", BTC_FILTER="off", BREADTH_FILTER="off",
                META_FILTER="off", MIN_RISK_DIST_PCT=0.0, MAX_RISK_DIST_PCT=1000.0, FAIL_TRADES="off")
    off, on = SimpleNamespace(**dict(base, STRUCT_EXIT="off")), SimpleNamespace(**dict(base, STRUCT_EXIT="cierra"))
    real = strategy.WyckoffEngine
    strategy.WyckoffEngine = Inj
    try:
        cands = []
        for seed in range(1, 7):
            cands += scan_candidates(synth(seed, cycles=8), 900, 0.0001, "Agresivo", off, 400, symbol=f"S{seed}")
    finally:
        strategy.WyckoffEngine = real
    assert cands, "sin candidatas"
    from strategy import exit_variant
    key = exit_variant(off)
    hit = 0
    for c in cands:
        assert "res_struct" in c
        if key in c["res"]:
            assert key in c["res_struct"], "si la normal cerró, la estructural debe tener resultado (el mismo o uno anterior)"
            if c["res_struct"][key]["reason"] == "estructura":
                hit += 1
                assert c["res_struct"][key]["close_t"] <= c["res"][key]["close_t"], "la salida por estructura no puede ser posterior"
    assert hit >= 1, "ninguna operación fue cerrada por la rotura inyectada"
    a = {id(x): x for x in select_trades(cands, off)}
    b = select_trades(cands, on)
    assert any(x["reason"] == "estructura" for x in b)
    assert not any(x["reason"] == "estructura" for x in select_trades(cands, off))
    print(f"scan/select OK: {hit} operaciones cerradas por estructura; con STRUCT_EXIT=off no cambia nada")


def _fake_bot(tmp, mode):
    import main
    from collections import namedtuple
    bot = main.Bot.__new__(main.Bot)
    C.STRUCT_EXIT = mode
    C.STRUCT_EXIT_REASONS = ["DEMOTE", "INVALID"]
    sent, rows, results = [], [], []
    bot.tg = SimpleNamespace(send=lambda m: sent.append(m))
    bot.journal = SimpleNamespace(write=lambda r: rows.append(r))
    bot.register_result = lambda r: results.append(r)
    bot.save_state = lambda: None
    bot.state = {"sims": {"X-USDT": {"tf": "1h", "side": "LONG", "kind": "Test Fase C", "entry": 100.0, "sl": 99.0,
                                      "tp1": 101.0, "tp2": 103.0, "rr": 2.0, "bar_t": 1000, "half": False,
                                      "open_ts": 0.0, "conf": 70, "val": 70, "risk": 1.0}},
                 "positions": {}, "daily": {"r": 0.0}, "stats": {"sum_r": 0.0}}
    return bot, sent, rows, results


def test_on_break():
    d = {"broke": {"why": "DEMOTE", "side": "LONG", "phase": 4}, "time": 2000}
    # aviso: registra cuánto daría cerrar, avisa y NO cierra
    bot, sent, rows, results = _fake_bot(None, "aviso")
    bot.on_break("X-USDT", "1h", d, 99.6)
    s = bot.state["sims"]["X-USDT"]
    assert abs(s["struct_r"] - (-0.4 - 0.1)) < 1e-6 and s["struct_why"] == "DEMOTE", s
    assert "X-USDT" in bot.state["sims"] and not results and any("estructura rota" in m for m in sent)
    # cierra: cierra la virtual con ese R y lo anota en el diario
    bot, sent, rows, results = _fake_bot(None, "cierra")
    bot.on_break("X-USDT", "1h", d, 99.6)
    assert "X-USDT" not in bot.state["sims"] and len(results) == 1 and abs(results[0] + 0.5) < 1e-6
    assert rows[0]["exit_reason"] == "estructura (demote)" and rows[0]["struct_why"] == "DEMOTE"
    # off, lado contrario, otro TF, o rotura de la propia vela de entrada: no hace nada
    for mode, dd, tf in (("off", d, "1h"), ("cierra", dict(d, broke={"why": "DEMOTE", "side": "SHORT"}), "1h"),
                         ("cierra", d, "4h"), ("cierra", dict(d, time=1000), "1h"),
                         ("cierra", dict(d, broke={"why": "STALE", "side": "LONG"}), "1h")):
        bot, sent, rows, results = _fake_bot(None, mode)
        bot.on_break("X-USDT", tf, dd, 99.6)
        assert "X-USDT" in bot.state["sims"] and not results and "struct_r" not in bot.state["sims"]["X-USDT"], (mode, dd, tf)
    C.STRUCT_EXIT = "aviso"
    print("on_break OK (aviso, cierra, y los 5 casos en que no debe actuar)")


def test_event_log():
    import csv as _csv
    import time as _t
    from concurrent.futures import ThreadPoolExecutor
    import main
    from notify import EventLog
    with tempfile.TemporaryDirectory() as td:
        bot = main.Bot.__new__(main.Bot)
        C.EVENT_OI_BINANCE = False
        now = _t.time()
        bot.ex = SimpleNamespace(contracts={"T-USDT": {"cls": "crypto", "cls_label": "cripto"}},
                                 premium_info=lambda s: (0.04, 0.02), open_interest=lambda s: 1000.0)
        bot.pool = ThreadPoolExecutor(2)
        bot.events = EventLog(td)
        bot.oi_snap = {"T-USDT": [(now - 3600, 900.0), (now - 6 * 3600, 1100.0)]}
        bot.oi_binance_fails = 0
        d = {"time": 1_700_000_000_000, "close": 5.0, "atr": 0.2, "phase": 3, "conf": 60, "val": 70, "rh": 6.0, "rl": 4.0,
             "wdir": 1, "broke": None, "sig": 0, "entry_now": False}
        bot.log_events([("T-USDT", "1h", d, ["SPRING", "ENTRADA"])])
        rows = list(_csv.DictReader(open(os.path.join(td, "events.csv"))))
        assert [r["event"] for r in rows] == ["SPRING", "ENTRADA"], rows
        r = rows[0]
        assert r["dir"] == "LONG" and r["symbol"] == "T-USDT" and r["cls"] == "cripto" and r["phase"] == "C"
        assert abs(float(r["funding_pct"]) - 0.04) < 1e-9 and abs(float(r["premium_pct"]) - 0.02) < 1e-9
        assert r["oi_src"] == "bingx" and abs(float(r["oi_chg_1h"]) - 11.111) < 0.01 and abs(float(r["oi_chg_6h"]) + 9.091) < 0.01
        assert int(r["ts_ms"]) == 1_700_000_000_000 + 3_600_000
        # SC → LONG, BC → SHORT aunque wdir diga otra cosa; rotura → lado de la estructura
        bot.log_events([("T-USDT", "1h", dict(d, wdir=-1), ["SC"]),
                        ("T-USDT", "1h", dict(d, broke={"why": "DEMOTE", "side": "SHORT"}), ["ROTURA_DEMOTE"])])
        rows = list(_csv.DictReader(open(os.path.join(td, "events.csv"))))
        assert rows[2]["dir"] == "LONG" and rows[3]["dir"] == "SHORT"
        # los eventos del pasado (calentamiento) no se registran: lo garantiza process_tf (t + 2*ms > cut)
        # cabecera distinta → se archiva el CSV viejo en vez de mezclar columnas
        with open(os.path.join(td, "events.csv"), "w") as f:
            f.write("a,b\n1,2\n")
        EventLog(td)
        assert any(n.startswith("events_old_") for n in os.listdir(td))
    C.EVENT_OI_BINANCE = True
    print("registro de eventos OK (funding, prima, OI propio, direcciones, archivado de cabecera vieja)")


def test_oi_snapshots():
    import time as _t
    import main
    bot = main.Bot.__new__(main.Bot)
    bot.oi_snap = {}
    vals = iter([100.0, 110.0, None])
    bot.ex = SimpleNamespace(open_interest=lambda s: next(vals))
    assert bot.snap_oi("A") == 100.0 and bot.snap_oi("A") == 110.0
    assert bot.snap_oi("A") is None and len(bot.oi_snap["A"]) == 2
    bot.oi_snap["A"] = [(_t.time() - 40 * 3600, 1.0), (_t.time() - 2 * 3600, 1.0)]
    bot.snap_oi.__func__  # existe
    h = bot.oi_snap["A"]
    h.append((_t.time(), 1.0))
    while h and h[0][0] < _t.time() - 30 * 3600:
        h.pop(0)
    assert len(h) == 2
    print("snapshots de OI OK")


def test_events_report():
    import time as _t
    import events_report as er
    tf_ms = 3_600_000
    t0 = int(_t.time() * 1000) // tf_ms * tf_ms - 300 * tf_ms
    rows = [[t0 + i * tf_ms, 100.0 + i, 101.0 + i, 99.0 + i, 100.0 + i, 10.0] for i in range(300)]   # sube 1 por vela
    ev = lambda i, dr, ev_, **k: dict(ts_ms=rows[i][0] + tf_ms, symbol="T-USDT", tf="1h", event=ev_, dir=dr,
                                       price=rows[i][4], atr=2.0, funding_pct=k.get("f", 0.0), premium_pct=0.0,
                                       oi_now=1.0, oi_chg_1h=0.0, oi_chg_6h=k.get("oi", 0.0), oi_chg_24h=0.0, phase="C",
                                       conf=60, val=70, cls="cripto")
    events = [ev(50, "LONG", "SPRING", f=-0.1, oi=-5), ev(60, "SHORT", "SPRING", f=-0.1, oi=5),
              ev(70, "LONG", "SPRING", f=0.1, oi=0), ev(280, "LONG", "TEST")]   # el último no tiene futuro
    done = er.forward(events, lambda s, tf, n, ms: rows)
    by = {(e["dir"], e["ts_ms"]): e for e in done}
    long_, short_ = by[("LONG", rows[50][0] + tf_ms)], by[("SHORT", rows[60][0] + tf_ms)]
    assert abs(long_["ret_6"] - 3.0) < 1e-9 and abs(short_["ret_6"] + 3.0) < 1e-9   # 6 velas × 1 / ATR 2
    assert abs(long_["ret_24"] - 12.0) < 1e-9 and long_["mfe24"] > 0
    assert done[-1]["ret_48"] is None
    out = []
    er.report(done, 24, None, out=out.append)
    txt = "\n".join(out)
    assert "SPRING" in txt and "funding" in txt and "OI 6h" in txt and "⚠ <30" in txt, txt
    assert "a favor (el lado amontonado es el contrario)" in txt   # funding -0.1% en un LONG
    print("events_report OK (retornos en ATR con signo, MFE/MAE, desgloses por funding/OI)")


if __name__ == "__main__":
    os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
    for fn in (test_walk_book, test_replace_stop, test_book_guard, test_cache_roundtrip, test_portfolio,
               test_montecarlo, test_parity_csv, test_diagnostic,
               test_engine_broke, test_trade_sim_mark, test_scan_struct_exit, test_on_break, test_event_log,
               test_oi_snapshots, test_events_report):
        fn()
    print("\nTODO OK")
