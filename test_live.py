"""
Prueba sin red de la capa LIVE con un exchange simulado: antirrebote de posición, cortacircuitos,
guardia de deslizamiento, cierre manual y de fin de semana, huérfanas en reconcile.
  python test_live.py
"""
import os, tempfile
os.environ.update(MODE="LIVE", CONFIRM_LIVE="SI", BINGX_API_KEY="k", BINGX_API_SECRET="s", DATA_DIR=tempfile.mkdtemp())
import time
from datetime import datetime, timezone
import config as C
import main as M
from bingx import BingXError

assert C.LIVE


class FakeEx:
    def __init__(self):
        self.contracts = {"AAA-USDT": {"cls": "crypto", "cls_label": "cripto", "pp": 2, "qp": 3, "tick": 0.01,
                                       "min_qty": 0.001, "min_usdt": 1, "api_open": True, "name": "AAA"},
                          "NCFXEUR2USD-USDT": {"cls": "forex", "cls_label": "forex", "pp": 4, "qp": 1, "tick": 1e-4,
                                               "min_qty": 0.1, "min_usdt": 1, "api_open": True, "name": "EUR/USD"}}
        self.pos, self.orders, self.closed, self.cancelled = [], [], [], []
        self.eq, self.fill, self.px, self.last_price = 1000.0, 100.0, 100.0, 100.0

    def price(self, s): return self.px
    def balance(self): return self.eq, self.eq
    def positions(self, symbol=None): return [p for p in self.pos if symbol in (None, p["symbol"])]
    def fmt_qty(self, s, q): return round(q, 3)
    def fmt_px(self, s, p): return round(p, 4)
    def set_margin_mode(self, *a): pass
    def set_leverage(self, *a): pass
    def order_exists(self, *a): return False
    def stop_orders(self, s, long): return [o for o in self.orders if o["symbol"] == s and o["type"] == "STOP_MARKET"]
    def open_orders(self, symbol=None): return list(self.orders)
    def get_order(self, *a, **k): return {}
    def cancel(self, s, oid): self.cancelled.append(oid); return True
    def realized_pnl(self, s, t): return None
    def market_open(self, s, long, qty, cid, stop_loss=None):
        self.pos.append({"symbol": s, "positionSide": "LONG" if long else "SHORT", "positionAmt": str(qty), "avgPrice": str(self.fill)})
        if stop_loss:
            self.orders.append({"symbol": s, "type": "STOP_MARKET", "orderId": "sl1", "positionSide": "LONG" if long else "SHORT", "side": "SELL" if long else "BUY"})
    def market_close(self, s, long, qty): self.closed.append((s, qty)); self.pos = [p for p in self.pos if p["symbol"] != s]
    def exit_order(self, s, long, kind, qty, px, client_id=None):
        self.orders.append({"symbol": s, "type": kind, "orderId": f"{kind}{px}", "positionSide": "LONG" if long else "SHORT", "side": "SELL" if long else "BUY"})
        return f"{kind}{px}"
    def hedge_mode(self): return True


def mk():
    b = M.Bot()
    b.ex = FakeEx()
    b.tg.send = lambda t: msgs.append(t)
    b.state.update(positions={}, sims={}, paused=False, streak=0, hwm=0.0)
    return b


def sig(entry=100.0, sl=98.0, tf="15m"):
    return {"side": "LONG", "entry": entry, "sl": sl, "tp1": 103.0, "tp2": 106.0, "rr": 3, "kind": "LPS", "rh": 103, "rl": 97,
            "atr": 1, "conf": 70, "val": 80, "time": 0, "risk_pct": abs(entry - sl) / entry * 100, "tf": tf, "ctx_align": "neutral"}


msgs = []
# 1) apertura normal: SL adjunto + TP1 + TP2
b = mk()
b.open_live("AAA-USDT", sig(), "txt")
assert "AAA-USDT" in b.state["positions"], msgs
kinds = sorted(o["type"] for o in b.ex.orders)
assert kinds == ["STOP_MARKET", "TAKE_PROFIT_MARKET", "TAKE_PROFIT_MARKET"], kinds
print("1 OK apertura con SL+TP1+TP2")

# 2) deslizamiento excesivo → cierre inmediato, sin posición en el estado
b = mk(); msgs.clear(); b.ex.fill = 100.9  # 0.45R peor
b.open_live("AAA-USDT", sig(), "txt")
assert "AAA-USDT" not in b.state["positions"] and b.ex.closed, msgs
print("2 OK guardia de deslizamiento")

# 3) antirrebote: una lectura vacía NO cierra el registro ni cancela órdenes; la segunda sí
b = mk(); b.open_live("AAA-USDT", sig(), "txt"); b.ex.cancelled.clear()
saved = b.ex.pos; b.ex.pos = []
b.manage()
assert "AAA-USDT" in b.state["positions"] and not b.ex.cancelled
b.ex.pos = saved; b.manage()
assert b.state["positions"]["AAA-USDT"]["gone"] == 0   # reaparece → contador a cero
b.ex.pos = []; b.manage(); b.manage()
assert "AAA-USDT" not in b.state["positions"]
print("3 OK antirrebote de posición desaparecida")

# 4) cortacircuitos de equity
b = mk(); msgs.clear(); b.state["hwm"] = 1000.0; b.ex.eq = 900.0
b.guard()
assert b.state["paused"], msgs
print("4 OK pausa por drawdown")

# 5) racha de pérdidas
b = mk()
for _ in range(C.MAX_LOSS_STREAK):
    b.register_result(-1.0)
assert b.state["paused"]
b2 = mk(); b2.register_result(-1); b2.register_result(0.5); assert b2.state["streak"] == 0
print("5 OK pausa por racha")

# 6) límite de liquidación
b = mk(); msgs.clear(); C.LEVERAGE = 50
b.open_live("AAA-USDT", sig(), "txt"); C.LEVERAGE = 5
assert "AAA-USDT" not in b.state["positions"] and "liquidación" in msgs[-1]
print("6 OK bloqueo por cercanía a liquidación")

# 7) cierre manual y de fin de semana (forzando la hora)
b = mk(); b.open_live("AAA-USDT", sig(), "txt")
assert "cierre a mercado" in b.manual_close("AAA"); assert b.ex.closed
class FakeDT(datetime):
    @classmethod
    def now(cls, tz=None): return datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc)  # viernes 21h UTC
M.datetime = FakeDT
b = mk(); b.state["positions"]["NCFXEUR2USD-USDT"] = {**sig(1.1, 1.09), "qty": 100, "entry_real": 1.1, "risk": 0.01, "be": False, "open_ts": time.time(), "tf": "15m"}
b.ex.pos = [{"symbol": "NCFXEUR2USD-USDT", "positionSide": "LONG", "positionAmt": "100"}]
b.weekend_close()
assert b.ex.closed and b.state["positions"]["NCFXEUR2USD-USDT"]["force_reason"] == "fin de semana"
print("7 OK cierre manual y de fin de semana")

# 8) reconcile no toca las órdenes de una posición viva sin estado
b = mk(); msgs.clear(); b.ex.pos = [{"symbol": "AAA-USDT", "positionSide": "LONG", "positionAmt": "1"}]
b.ex.orders = [{"symbol": "AAA-USDT", "type": "TAKE_PROFIT_MARKET", "orderId": "x", "clientOrderID": "wykTP123"},
               {"symbol": "ZZZ-USDT", "type": "TAKE_PROFIT_MARKET", "orderId": "y", "clientOrderID": "wykTP999"}]
b.reconcile()
assert b.ex.cancelled == ["y"], b.ex.cancelled
assert any("SIN estado" in m for m in msgs)
print("8 OK reconcile protege posiciones vivas")
print("TODO OK")

# 9) tamaño fijo 10 USDT: posición ≈ 10 USDT, sin TP1 si no se puede partir, salida única en TP1
C.NOTIONAL_USDT = 10.0
b = mk(); msgs.clear(); b.ex.contracts["AAA-USDT"]["min_qty"] = 0.1; b.ex.contracts["AAA-USDT"]["qp"] = 1
b.ex.fmt_qty = lambda s, q: int(q * 10) / 10  # qp=1 como un contrato caro
b.ex.px = b.ex.fill = 100.0
b.open_live("AAA-USDT", sig(), "txt")
rec = b.state["positions"]["AAA-USDT"]
assert abs(rec["qty"] * 100 - 10) < 1e-6 and rec.get("single"), rec
tps = [o for o in b.ex.orders if o["type"] == "TAKE_PROFIT_MARKET"]
assert len(tps) == 1 and "103.0" in tps[0]["orderId"], tps   # sale entera en TP1
print("9 OK posición fija 10 USDT con salida única")
C.NOTIONAL_USDT = 0.0
print("TODO OK 2")


# 10) rate limit 109429 y símbolo pausado 109415
import bingx as BX
class R:
    def __init__(s, j): s._j = j
    def json(s): return s._j
ex = BX.BingX("k", "s")
calls = []
ex.http.request = lambda m, url, timeout=0: (calls.append(url), R({"code": 109429, "msg": "over 10 error code:109415 requests within 900000 ms for this api, please verify and fix it ,can retry after time: %d" % int((time.time() + 600) * 1000)}))[1]
try:
    ex.klines("AAA-USDT", "1h", 6)
except BX.BingXError as e:
    assert "bloqueada" in str(e)
n0 = len(calls)
try:
    ex.klines("BBB-USDT", "1h", 6)
except BX.BingXError as e:
    assert "bloqueada" in str(e)
assert len(calls) == n0, "no debe llamar a una ruta bloqueada"
ex2 = BX.BingX("k", "s")
ex2.http.request = lambda m, url, timeout=0: R({"code": 109415, "msg": "NCFXUSD2ILS-USDT is pause currently"})
try:
    ex2.klines("NCFXUSD2ILS-USDT", "1h", 6); raise SystemExit("debía fallar")
except BX.BingXPaused:
    pass
from types import SimpleNamespace
b = mk(); msgs.clear()
b.symbols = ["NCFXUSD2ILS-USDT"]; b.engines[("NCFXUSD2ILS-USDT", "15m")] = SimpleNamespace(last={}, last_t=0)
b.ex.klines = lambda *a, **k: (_ for _ in ()).throw(BX.BingXPaused("pausado"))
b.process_tf("15m")
assert not b.usable("NCFXUSD2ILS-USDT")
calls2 = []
b.ex.klines = lambda *a, **k: calls2.append(a) or []
b.process_tf("15m")
assert not calls2, "un símbolo pausado no se vuelve a pedir"
print("10 OK rate limit respetado y símbolos pausados fuera")


# 11) un fallo transitorio en el arranque no deja el universo reducido 6 horas
from test_engine import synth
C.SYMBOLS = ["AAA-USDT"]
b = mk(); state = {"fail": True}
b.ex.load_contracts = lambda: None
b.ex.tickers = lambda: []
def hist(sym, tf, n, ms):
    if state["fail"]:
        raise BX.BingXError("/klines: bloqueada por BingX 300s más (rate limit)")
    return [list(r[:6]) for r in synth(1)][-400:]
b.ex.klines_history = hist
b.refresh_universe(force=True)
assert b.symbols == [] and time.time() - b.universe_ts > C.UNIVERSE_REFRESH_H * 3600 - 400, "debe reintentar pronto"
state["fail"] = False
b.universe_ts = 0
b.refresh_universe()
assert b.symbols == ["AAA-USDT"], b.symbols
C.SYMBOLS = []
print("11 OK reintento del universo tras un fallo transitorio")
