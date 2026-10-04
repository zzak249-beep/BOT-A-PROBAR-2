"""Cliente BingX perpetuos USDT-M (swap v2).

Firma: HMAC-SHA256 sobre el query string ordenado. El MISMO string firmado es el que se envía
(GET/DELETE en la URL, POST en el cuerpo form-urlencoded), para no repetir el fallo de firma
por reordenación de parámetros.
"""
import hashlib
import json
import math
import os
import threading
import hmac
import logging
import time
from urllib.parse import urlencode

import requests

from universe import classify, pretty

log = logging.getLogger("bingx")


class BingXError(Exception):
    pass


def walk_book(levels, qty):
    """Precio medio al que se llena `qty` recorriendo niveles [(precio, cantidad)] de mejor a peor.
    None si los niveles no alcanzan para llenarla (libro demasiado fino)."""
    left, cost = qty, 0.0
    for px, q in levels:
        take = min(left, q)
        cost += take * px
        left -= take
        if left <= 1e-12:
            return cost / qty
    return None


class BingX:
    def __init__(self, key, secret, vst=False):
        self.key, self.secret = key, secret
        self.base = "https://open-api-vst.bingx.com" if vst else "https://open-api.bingx.com"
        self.http = requests.Session()
        self.http.headers.update({"X-BX-APIKEY": key})
        self.contracts = {}
        self._hedge = None
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self.min_interval = 1.0 / max(float(os.getenv("BINGX_MAX_RPS", "15")), 1.0)

    def _throttle(self):
        """Limita las peticiones por segundo entre todos los hilos (cientos de símbolos por vela)."""
        with self._lock:
            now = time.monotonic()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self.min_interval
        if wait > 0:
            time.sleep(wait)

    # ── núcleo ──
    def _req(self, method, path, params=None, signed=False, retries=3):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in range(retries):
            p = dict(params)
            if signed:
                p["timestamp"] = int(time.time() * 1000)
                p["recvWindow"] = 10000
            qs = urlencode(sorted(p.items()))
            if signed:
                sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
                qs = f"{qs}&signature={sig}" if qs else f"signature={sig}"
            url = self.base + path
            self._throttle()
            try:
                if method == "POST":
                    r = self.http.post(url, data=qs, timeout=15,
                                       headers={"Content-Type": "application/x-www-form-urlencoded"})
                else:
                    r = self.http.request(method, f"{url}?{qs}" if qs else url, timeout=15)
                j = r.json()
            except (requests.RequestException, ValueError) as e:
                # un POST que se queda sin respuesta puede haberse ejecutado: reintentarlo a ciegas duplicaría
                # la orden. Se devuelve el error y quien llama comprueba con order_exists().
                if method == "POST" or attempt == retries - 1:
                    raise BingXError(f"red: {e}")
                time.sleep(1.5 * (attempt + 1))
                continue
            code = j.get("code", 0)
            if code in (0, "0"):
                return j.get("data")
            if code in (100410, 109400) and attempt < retries - 1:  # rate limit / sobrecarga
                time.sleep(2 * (attempt + 1))
                continue
            raise BingXError(f"{path} code={code} msg={j.get('msg')}")
        raise BingXError(f"{path}: sin respuesta")

    # ── mercado (público) ──
    def load_contracts(self):
        data = self._req("GET", "/openApi/swap/v2/quote/contracts") or []
        out = {}
        for c in data:
            sym = c.get("symbol")
            if not sym or not sym.endswith("-USDT"):
                continue
            if str(c.get("status", 1)) not in ("1", "True", "true"):
                continue
            pp = int(c.get("pricePrecision", 4))
            label, key = classify(sym)
            out[sym] = {
                "cls": key, "cls_label": label, "name": c.get("displayName") or pretty(sym),
                "api_open": str(c.get("apiStateOpen", "true")).lower() == "true",
                "pp": pp, "qp": int(c.get("quantityPrecision", 0)),
                "tick": 10 ** (-pp), "min_qty": float(c.get("tradeMinQuantity", 0) or 0),
                "min_usdt": float(c.get("tradeMinUSDT", 0) or 0),
            }
        self.contracts = out
        return out

    def tickers(self):
        return self._req("GET", "/openApi/swap/v2/quote/ticker") or []

    def price(self, symbol):
        d = self._req("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
        return float(d["price"])

    def depth(self, symbol, limit=20):
        """Libro: (bids de mejor a peor, asks de mejor a peor), cada nivel (precio, cantidad)."""
        d = self._req("GET", "/openApi/swap/v2/quote/depth", {"symbol": symbol, "limit": limit}) or {}

        def lv(rows):
            out = []
            for r in rows or []:
                try:
                    if isinstance(r, dict):
                        out.append((float(r["price"]), float(r.get("quantity", r.get("qty", 0)))))
                    else:
                        out.append((float(r[0]), float(r[1])))
                except (KeyError, ValueError, IndexError, TypeError):
                    continue
            return out

        return sorted(lv(d.get("bids")), key=lambda x: -x[0]), sorted(lv(d.get("asks")), key=lambda x: x[0])

    def premium_info(self, symbol):
        """(funding %, prima mark/index %) del símbolo. Cada parte puede ser None."""
        d = self._req("GET", "/openApi/swap/v2/quote/premiumIndex", {"symbol": symbol})
        if isinstance(d, list):
            d = d[0] if d else {}
        d = d or {}
        fr = d.get("lastFundingRate")
        fr = None if fr in (None, "") else float(fr) * 100
        try:
            mk, ix = float(d.get("markPrice")), float(d.get("indexPrice"))
            pr = (mk / ix - 1) * 100 if ix > 0 else None
        except (TypeError, ValueError):
            pr = None
        return fr, pr

    def open_interest(self, symbol):
        """Interés abierto actual (unidades del contrato). None si BingX no lo da para ese símbolo."""
        d = self._req("GET", "/openApi/swap/v2/quote/openInterest", {"symbol": symbol})
        if isinstance(d, list):
            d = d[0] if d else {}
        v = (d or {}).get("openInterest")
        return None if v in (None, "") else float(v)

    def funding_rate(self, symbol):
        d = self._req("GET", "/openApi/swap/v2/quote/premiumIndex", {"symbol": symbol})
        if isinstance(d, list):
            d = d[0] if d else {}
        v = d.get("lastFundingRate")
        return None if v in (None, "") else float(v)

    def klines(self, symbol, interval, limit=1000, end_time=None):
        """Velas ordenadas de antigua a reciente: [t, o, h, l, c, v]. Incluye la vela en formación."""
        d = self._req("GET", "/openApi/swap/v3/quote/klines",
                      {"symbol": symbol, "interval": interval, "limit": min(limit, 1440), "endTime": end_time})
        rows = []
        for k in d or []:
            if isinstance(k, dict):
                rows.append([int(k["time"]), float(k["open"]), float(k["high"]), float(k["low"]),
                             float(k["close"]), float(k.get("volume", 0))])
            else:
                rows.append([int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])])
        rows.sort(key=lambda r: r[0])
        return rows

    def klines_history(self, symbol, interval, total, tf_ms):
        rows = self.klines(symbol, interval, min(total, 1440))
        while len(rows) < total and rows:
            older = self.klines(symbol, interval, min(total - len(rows), 1440), end_time=rows[0][0] - 1)
            older = [r for r in older if r[0] < rows[0][0]]
            if not older:
                break
            rows = older + rows
        return rows[-total:]

    # ── cuenta ──
    def balance(self):
        d = self._req("GET", "/openApi/swap/v2/user/balance", signed=True)
        b = d.get("balance", d) if isinstance(d, dict) else d[0]
        return float(b.get("equity", b.get("balance", 0))), float(b.get("availableMargin", 0))

    def positions(self, symbol=None):
        d = self._req("GET", "/openApi/swap/v2/user/positions", {"symbol": symbol}, signed=True) or []
        return [p for p in d if abs(float(p.get("positionAmt", 0) or 0)) > 0]

    def hedge_mode(self):
        if self._hedge is None:
            try:
                d = self._req("GET", "/openApi/swap/v1/positionSide/dual", signed=True) or {}
                self._hedge = str(d.get("dualSidePosition", "true")).lower() == "true"
            except BingXError as e:
                log.warning("no se pudo leer el modo de posición (%s); se asume Hedge", e)
                self._hedge = True
        return self._hedge

    def set_margin_mode(self, symbol, mode):
        try:
            self._req("POST", "/openApi/swap/v2/trade/marginType", {"symbol": symbol, "marginType": mode}, signed=True)
        except BingXError as e:
            log.debug("marginType %s: %s", symbol, e)  # ya estaba en ese modo

    def set_leverage(self, symbol, lev):
        sides = ("LONG", "SHORT") if self.hedge_mode() else ("BOTH",)
        for side in sides:
            try:
                self._req("POST", "/openApi/swap/v2/trade/leverage",
                          {"symbol": symbol, "side": side, "leverage": lev}, signed=True)
            except BingXError as e:
                log.warning("leverage %s %s: %s", symbol, side, e)

    # ── órdenes ──
    def fmt_qty(self, symbol, q):
        qp = self.contracts.get(symbol, {}).get("qp", 3)
        f = 10 ** qp
        return round(math.floor(q * f + 1e-9) / f, qp)  # siempre hacia abajo, sin ruido de coma flotante

    def fmt_px(self, symbol, p):
        return round(p, self.contracts.get(symbol, {}).get("pp", 4))

    def _pos_side(self, side_long):
        return ("LONG" if side_long else "SHORT") if self.hedge_mode() else "BOTH"

    def market_open(self, symbol, side_long, qty, client_id, stop_loss=None):
        """Entrada a mercado. Con stop_loss, el SL viaja DENTRO de la orden de entrada."""
        p = {"symbol": symbol, "side": "BUY" if side_long else "SELL", "positionSide": self._pos_side(side_long),
             "type": "MARKET", "quantity": self.fmt_qty(symbol, qty), "clientOrderID": client_id}
        if stop_loss is not None:
            px = self.fmt_px(symbol, stop_loss)
            p["stopLoss"] = json.dumps({"type": "STOP_MARKET", "stopPrice": px, "price": px,
                                        "workingType": "MARK_PRICE"}, separators=(",", ":"))
        return self._req("POST", "/openApi/swap/v2/trade/order", p, signed=True)

    def exit_order(self, symbol, side_long, kind, qty, stop_price, client_id=None):
        """kind = STOP_MARKET | TAKE_PROFIT_MARKET. Cierra (parte de) la posición `side_long`."""
        p = {
            "symbol": symbol, "side": "SELL" if side_long else "BUY", "positionSide": self._pos_side(side_long),
            "type": kind, "quantity": self.fmt_qty(symbol, qty), "stopPrice": self.fmt_px(symbol, stop_price),
            "workingType": "MARK_PRICE", "clientOrderID": client_id or f"wyk{kind[:2]}{int(time.time() * 1000) % 10**10}",
        }
        if not self.hedge_mode():
            p["reduceOnly"] = "true"
        d = self._req("POST", "/openApi/swap/v2/trade/order", p, signed=True)
        o = (d or {}).get("order", d) or {}
        return str(o.get("orderId", o.get("orderID", "")))

    def market_close(self, symbol, side_long, qty):
        p = {"symbol": symbol, "side": "SELL" if side_long else "BUY", "positionSide": self._pos_side(side_long),
             "type": "MARKET", "quantity": self.fmt_qty(symbol, qty)}
        if not self.hedge_mode():
            p["reduceOnly"] = "true"
        return self._req("POST", "/openApi/swap/v2/trade/order", p, signed=True)

    def open_orders(self, symbol=None):
        d = self._req("GET", "/openApi/swap/v2/trade/openOrders", {"symbol": symbol}, signed=True) or {}
        return d.get("orders", []) if isinstance(d, dict) else d

    def get_order(self, symbol, order_id=None, client_id=None):
        d = self._req("GET", "/openApi/swap/v2/trade/order",
                      {"symbol": symbol, "orderId": order_id, "clientOrderID": client_id}, signed=True) or {}
        return d.get("order", d)

    def cancel(self, symbol, order_id):
        try:
            self._req("DELETE", "/openApi/swap/v2/trade/order", {"symbol": symbol, "orderId": order_id}, signed=True)
            return True
        except BingXError as e:
            log.info("cancel %s %s: %s", symbol, order_id, e)
            return False

    @staticmethod
    def _side_of(o):
        return str(o.get("positionSide", "BOTH")).upper(), str(o.get("side", "")).upper()

    def stop_orders(self, symbol, side_long):
        """Órdenes STOP vivas que cierran la posición `side_long` de `symbol`."""
        out = []
        for o in self.open_orders(symbol):
            if str(o.get("type", "")).upper() not in ("STOP_MARKET", "STOP"):
                continue
            ps, side = self._side_of(o)
            closes_long = side == "SELL"
            if ps in ("LONG", "SHORT"):
                if (ps == "LONG") != side_long:
                    continue
            elif closes_long != side_long:
                continue
            out.append(o)
        return out

    def order_exists(self, symbol, client_id):
        """Tras un fallo de red al abrir: ¿la orden llegó al exchange?"""
        try:
            o = self.get_order(symbol, client_id=client_id)
            return bool(o) and str(o.get("status", "")).upper() in ("FILLED", "PARTIALLY_FILLED", "NEW")
        except BingXError:
            return False
