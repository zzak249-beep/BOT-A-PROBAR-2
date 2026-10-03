"""
Capa de estrategia común a bot, backtest y sweep: plan SL/TP (f_riskPlan del indicador),
filtros, contextos (estructura del TF superior, estructura de BTC), simulación de la gestión
y selección de operaciones. Bot y backtest llaman a ESTAS funciones: se mide lo mismo que se opera.

Salidas configurables (para medir, no para creer):
  TP2_MULT   proyección de TP2 = altura del rango × TP2_MULT (1.0 = indicador)
  TRAIL_ATR  tras TP1, stop que persigue a cierre − TRAIL_ATR×ATR (0 = off, indicador)
  TIME_STOP  cierra a mercado si en N velas no ha tocado TP1 (0 = off, indicador)
"""
import bisect
import json
import math

from wyckoff_engine import (DIR_ACCUM, DIR_DIST, ENTRY_NAMES, PHASE_A, PHASE_C, PHASE_E, PHASE_NAMES, TYPE_NAMES,
                            WyckoffEngine, na)

FAIL_KIND = "Trampa (estructura rota)"


def risk_plan(long, entry, rh, rl, exc, excP, testP, atr, sl_buffer_atr, tick, tp2_mult=1.0):
    """Port de f_riskPlan (+ multiplicador de TP2). Devuelve (sl, tp1, tp2, rr_tp2) o None."""
    if na(rh) or na(rl) or na(entry) or na(atr):
        return None
    hgt = max(rh - rl, tick)
    if long:
        ref = min(excP, rl) if (exc and not na(excP)) else (min(testP, rl) if not na(testP) else rl)
        sl = ref - atr * sl_buffer_atr
        risk = max(entry - sl, tick)
        tp1 = rh if entry < rh - atr * 0.25 else entry + risk
        tp2 = max(rh + hgt * tp2_mult, tp1 + risk)
    else:
        ref = max(excP, rh) if (exc and not na(excP)) else (max(testP, rh) if not na(testP) else rh)
        sl = ref + atr * sl_buffer_atr
        risk = max(sl - entry, tick)
        tp1 = rl if entry > rl + atr * 0.25 else entry - risk
        tp2 = min(rl - hgt * tp2_mult, tp1 - risk)
    rr2 = abs(tp2 - entry) / max(abs(entry - sl), tick)
    return sl, tp1, tp2, rr2


def build_signal(d, cfg, tick, tp2_mult=None):
    """A partir del estado del motor en la vela de entrada, arma la señal completa."""
    if not d.get("entry_now") or d["outcome"] not in (DIR_ACCUM, DIR_DIST):
        return None
    long = d["outcome"] == DIR_ACCUM
    m = cfg.TP2_MULT if tp2_mult is None else tp2_mult
    plan = risk_plan(long, d["close"], d["rh"], d["rl"], d["exc"], d["excP"], d["testP"], d["atr"],
                     cfg.SL_BUFFER_ATR, tick, m)
    if plan is None:
        return None
    sl, tp1, tp2, rr = plan
    return {
        "side": "LONG" if long else "SHORT", "entry": d["close"], "sl": sl, "tp1": tp1, "tp2": tp2, "rr": rr,
        "kind": ENTRY_NAMES.get(d["entryKind"], "-"), "rh": d["rh"], "rl": d["rl"], "atr": d["atr"],
        "conf": d["conf"], "val": d["val"], "time": d["time"], "risk_pct": abs(d["close"] - sl) / d["close"] * 100,
        "range_atr": round(d.get("range_atr", 0.0), 2), "b_bars": d.get("b_bars", 0),
    }


def build_fail_signal(fail, close, atr, t, cfg, tick, tp2_mult=None):
    """IDEA NUEVA — operar a los atrapados. La estructura estaba en Fase C/D con dirección decidida y el precio
    cierra más allá del nivel duro (Spring/UTAD o clímax): quien compró el Spring tiene el stop justo debajo.
    Entrada en contra de la estructura al cierre; stop de vuelta al otro lado del nivel duro (si el precio
    recupera el nivel, la ruptura también ha fallado); TP2 = movimiento medido de la altura del rango."""
    if not fail or na(fail.get("hard")) or na(fail.get("rh")) or na(fail.get("rl")) or na(atr):
        return None
    long = fail["side"] == "LONG"
    m = cfg.TP2_MULT if tp2_mult is None else tp2_mult
    hgt = max(fail["rh"] - fail["rl"], tick)
    if long:
        sl = fail["hard"] - cfg.FAIL_SL_ATR * atr
        risk = close - sl
        if risk <= tick:
            return None
        tp1 = close + risk
        tp2 = max(fail["hard"] + hgt * m, close + 2 * risk)
    else:
        sl = fail["hard"] + cfg.FAIL_SL_ATR * atr
        risk = sl - close
        if risk <= tick:
            return None
        tp1 = close - risk
        tp2 = min(fail["hard"] - hgt * m, close - 2 * risk)
    return {"side": fail["side"], "entry": close, "sl": sl, "tp1": tp1, "tp2": tp2, "rr": abs(tp2 - close) / risk,
            "kind": FAIL_KIND, "rh": fail["rh"], "rl": fail["rl"], "atr": atr, "conf": fail["conf"],
            "val": fail["val"], "time": t, "risk_pct": risk / close * 100,
            "range_atr": round(fail["range_atr"], 2), "b_bars": fail["b_bars"], "had_entry": fail["had_entry"]}


# ── IDEA NUEVA: flujo agresor (taker) en el Spring/UTAD ──
def flow_features(side, rows_tail, exc_ts=None, n=10):
    """rows_tail: velas [t,o,h,l,c,v,taker_buy] terminando en la vela de entrada.
    flow     = (compra agresora − venta agresora) / volumen de las últimas n velas, con signo del lado
               (+ = el flujo empuja a favor de la operación).
    flow_exc = lo mismo en la vela del Spring/UTAD y la siguiente, con signo del lado. Wyckoff dice que
               un Spring bueno es venta ABSORBIDA: venta agresora fuerte (flow_exc negativo) y aun así el
               precio recupera. La otra lectura (flujo a favor confirma) también es posible: decide el backtest."""
    if not rows_tail or len(rows_tail[-1]) < 7:
        return None, None
    d = 1 if side == "LONG" else -1

    def norm(rs):
        v = sum(r[5] for r in rs)
        if v <= 0:
            return None
        return round(d * sum(2 * r[6] - r[5] for r in rs) / v, 4)

    flow = norm(rows_tail[-n:])
    flow_exc = None
    if exc_ts is not None and not na(exc_ts):
        for k, r in enumerate(rows_tail):
            if r[0] == exc_ts:
                flow_exc = norm(rows_tail[k:k + 2])
                break
    return flow, flow_exc


# ── IDEA NUEVA: amplitud Wyckoff entre símbolos ──
def wyckoff_state(d):
    """+1 estructura alcista avanzada (Fase C-E acumulación), -1 bajista, 0 nada."""
    if not d or d.get("phase", 0) < PHASE_C or d.get("phase", 0) > PHASE_E:
        return 0
    w = d.get("wdir", 0)
    return 1 if w == DIR_ACCUM else -1 if w == DIR_DIST else 0


def breadth_label(x):
    if x is None:
        return "-"
    return "a favor" if x > 0.02 else "en contra" if x < -0.02 else "neutral"


def apply_breadth(cands, timelines):
    """timelines: {símbolo: (lista_t, lista_estado)}. Para cada señal: media del estado Wyckoff de los DEMÁS
    símbolos en ese instante, con signo del lado. ¿Funciona mejor un Spring cuando medio mercado también acumula?"""
    syms = list(timelines)
    for c in cands:
        tot = n = 0
        for s in syms:
            if s == c["symbol"]:
                continue
            ts, st = timelines[s]
            k = bisect.bisect_right(ts, c["open_t"]) - 1
            if k >= 0:
                tot += st[k]
                n += 1
        if n == 0:
            c["breadth"], c["breadth_align"] = None, "-"
            continue
        b = tot / n * (1 if c["side"] == "LONG" else -1)
        c["breadth"], c["breadth_align"] = round(b, 4), breadth_label(b)


# ── IDEA NUEVA: meta-etiquetado (un segundo modelo decide qué señales del indicador tomar) ──
META_FEATURES = ("val", "conf", "rr", "range_atr", "log_b", "risk_pct", "ctx", "btc", "ema", "flow", "flow_exc",
                 "breadth", "long", "kind_lps", "kind_sos", "kind_fail")


def _align_num(x):
    return {"a favor": 1.0, "en contra": -1.0}.get(x, 0.0)


def meta_features(c):
    k = c.get("kind", "")
    return {
        "val": c.get("val"), "conf": c.get("conf"), "rr": min(c.get("rr", 0) or 0, 10),
        "range_atr": c.get("range_atr"), "log_b": math.log1p(max(c.get("b_bars", 0) or 0, 0)),
        "risk_pct": c.get("risk_pct"), "ctx": _align_num(c.get("ctx_align")), "btc": _align_num(c.get("btc_align")),
        "ema": -1.0 if c.get("against_trend") else 0.0, "flow": c.get("flow"), "flow_exc": c.get("flow_exc"),
        "breadth": c.get("breadth"), "long": 1.0 if c.get("side") == "LONG" else 0.0,
        "kind_lps": 1.0 if "LPS" in k else 0.0, "kind_sos": 1.0 if "SOS" in k else 0.0,
        "kind_fail": 1.0 if k == FAIL_KIND else 0.0,
    }


class MetaModel:
    """Regresión logística con L2, en Python puro (sin numpy). Valores ausentes → media de entrenamiento."""

    def __init__(self, feats=META_FEATURES):
        self.feats = list(feats)
        self.mean, self.std, self.w, self.b, self.thr = {}, {}, {}, 0.0, 0.5

    def _x(self, c):
        f = meta_features(c)
        return [((f[k] if f[k] is not None else self.mean[k]) - self.mean[k]) / self.std[k] for k in self.feats]

    def fit(self, cands, labels, l2=1.0, iters=600, lr=0.1):
        for k in self.feats:
            vals = [meta_features(c)[k] for c in cands]
            vals = [v for v in vals if v is not None]
            m = sum(vals) / len(vals) if vals else 0.0
            sd = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals)) if vals else 1.0
            self.mean[k], self.std[k] = m, (sd if sd > 1e-9 else 1.0)
        X = [self._x(c) for c in cands]
        y = labels
        n, p = len(X), len(self.feats)
        w, b = [0.0] * p, 0.0
        for _ in range(iters):
            gw, gb = [0.0] * p, 0.0
            for xi, yi in zip(X, y):
                z = b + sum(wj * xj for wj, xj in zip(w, xi))
                e = 1 / (1 + math.exp(-max(min(z, 30), -30))) - yi
                gb += e
                for j in range(p):
                    gw[j] += e * xi[j]
            b -= lr * gb / n
            w = [wj - lr * (gj / n + l2 * wj / n) for wj, gj in zip(w, gw)]
        self.w, self.b = dict(zip(self.feats, w)), b
        return self

    def prob(self, c):
        z = self.b + sum(self.w[k] * x for k, x in zip(self.feats, self._x(c)))
        return 1 / (1 + math.exp(-max(min(z, 30), -30)))

    def save(self, path, info):
        with open(path, "w") as f:
            json.dump({"feats": self.feats, "mean": self.mean, "std": self.std, "w": self.w, "b": self.b,
                       "thr": self.thr, "info": info}, f, indent=1)

    @classmethod
    def load(cls, path):
        with open(path) as f:
            j = json.load(f)
        m = cls(j["feats"])
        m.mean, m.std, m.w, m.b, m.thr = j["mean"], j["std"], j["w"], j["b"], j["thr"]
        m.info = j.get("info", {})
        return m


# ── tendencia HTF (EMA de la vela anterior ya cerrada, como el indicador v2) ──
def ema_last(closes, n):
    if len(closes) < n:
        return float("nan")
    k = 2.0 / (n + 1)
    e = sum(closes[:n]) / n  # ta.ema de Pine arranca con SMA
    for x in closes[n:]:
        e = x * k + e * (1 - k)
    return e


def trend_dir(price, ema):
    if na(ema):
        return 0
    return 1 if price > ema else -1 if price < ema else 0


# ── contextos Wyckoff (TF superior del propio símbolo, y BTC) ──
def context_of(ctx_state):
    """Dirección de la estructura: +1 acumulación, -1 distribución, 0 nada/Fase A."""
    if not ctx_state or ctx_state.get("phase", 0) <= PHASE_A:
        return 0, "sin estructura"
    w = ctx_state.get("wdir", 0)
    label = f"{TYPE_NAMES.get(ctx_state.get('type', 0), '-')} fase {PHASE_NAMES[ctx_state['phase']]}"
    return (1 if w == DIR_ACCUM else -1 if w == DIR_DIST else 0), label


def alignment(side, ctx_dir):
    d = 1 if side == "LONG" else -1
    if ctx_dir == 0:
        return "neutral"
    return "a favor" if ctx_dir == d else "en contra"


def filters(sig, cfg, trend, ctx_align="neutral", btc_align="neutral"):
    """Lista de motivos por los que NO se abre (vacía = se puede abrir)."""
    why = []
    if cfg.MIN_RR > 0 and sig["rr"] < cfg.MIN_RR:
        why.append(f"R:R {sig['rr']:.2f} < {cfg.MIN_RR}")
    if sig["risk_pct"] > cfg.MAX_RISK_DIST_PCT:
        why.append(f"stop a {sig['risk_pct']:.2f}% (> {cfg.MAX_RISK_DIST_PCT}%)")
    if sig["risk_pct"] < cfg.MIN_RISK_DIST_PCT:
        why.append(f"stop a {sig['risk_pct']:.2f}% (< {cfg.MIN_RISK_DIST_PCT}%, el coste se come la R)")
    against = (sig["side"] == "LONG" and trend < 0) or (sig["side"] == "SHORT" and trend > 0)
    sig["against_trend"], sig["ctx_align"], sig["btc_align"] = against, ctx_align, btc_align
    if against and cfg.TREND_FILTER == "bloquea":
        why.append(f"contra tendencia EMA {cfg.TREND_TF}")
    if ctx_align == "en contra" and cfg.CONTEXT_FILTER == "bloquea":
        why.append(f"estructura {cfg.CONTEXT_TF} en contra")
    if btc_align == "en contra" and cfg.BTC_FILTER == "bloquea":
        why.append(f"estructura de BTC {cfg.CONTEXT_TF} en contra")
    return why


class TradeSim:
    """Gestión: TP1 parcial (+ SL a BE), resto a TP2; opcional trailing tras TP1 y salida por tiempo.
    Orden dentro de cada vela: primero se comprueban toques con el stop vigente (si toca SL y TP a la vez,
    cuenta el SL: supuesto pesimista); al cierre se aplican tiempo y trailing, que rigen desde la vela siguiente."""

    def __init__(self, sig, bar, cost_pct, tp1_frac=0.5, move_be=True, trail_atr=0.0, time_stop=0):
        self.d = 1 if sig["side"] == "LONG" else -1
        self.e, self.s, self.t1, self.t2 = sig["entry"], sig["sl"], sig["tp1"], sig["tp2"]
        self.risk = abs(self.e - self.s)
        self.half = False
        self.bar = bar
        self.cost = cost_pct
        self.f = tp1_frac
        self.be = move_be
        self.trail = trail_atr
        self.tstop = time_stop
        self.reason = ""

    def _net(self, r):
        return r - 2.0 * self.cost / 100.0 * self.e / self.risk

    def step(self, bar, h, l, c=None, atr=None):
        """Devuelve R neta al cerrar, o None si sigue abierta. self.reason dice cómo cerró."""
        if bar <= self.bar or self.risk <= 0:
            return None
        d, f = self.d, self.f
        hitS = l <= self.s if d == 1 else h >= self.s
        hitT1 = (not self.half) and (h >= self.t1 if d == 1 else l <= self.t1)
        hitT2 = h >= self.t2 if d == 1 else l <= self.t2
        r1 = (self.t1 - self.e) * d / self.risk
        if hitS:
            rStop = (self.s - self.e) * d / self.risk
            if not self.half:
                self.reason = "SL"
                return self._net(rStop)
            self.reason = "BE" if abs(self.s - self.e) < 1e-12 else ("trailing" if self.trail else "SL tras TP1")
            return self._net(f * r1 + (1 - f) * rStop)
        if hitT1:
            self.half = True
            if self.be:
                self.s = self.e
        if hitT2:
            self.reason = "TP2"
            r2 = (self.t2 - self.e) * d / self.risk
            return self._net(f * r1 + (1 - f) * r2 if self.half else r2)
        if c is not None:
            if self.tstop and not self.half and bar - self.bar >= self.tstop:
                self.reason = "tiempo"
                return self._net((c - self.e) * d / self.risk)
            if self.trail and self.half and atr and atr == atr:
                new = c - d * self.trail * atr
                if (new - self.s) * d > 0:
                    self.s = new
        return None


EXIT_KEYS = ("TP2_MULT", "TRAIL_ATR", "TIME_STOP_BARS")
META = {"model": None}  # el backtest/bot cargan aquí el modelo si existe


def exit_variant(cfg, **over):
    """Clave hashable de una variante de salida: (tp2_mult, trail_atr, time_stop)."""
    v = {k: getattr(cfg, k) for k in EXIT_KEYS}
    v.update(over)
    return (float(v["TP2_MULT"]), float(v["TRAIL_ATR"]), int(v["TIME_STOP_BARS"]))


def scan_candidates(rows, tf_s, tick, strict, cfg, warmup, htf_ema=None, ctx_rows=None, ctx_tf_s=None,
                    symbol="", range_effort=False, btc_rows=None, exits=None, timeline=None):
    """Recorre las velas con el motor y devuelve TODAS las entradas que valida. Cada una lleva, por variante
    de salida, su resultado simulado de forma independiente (solo depende de los precios futuros) y sus
    etiquetas de filtro; el backtest aplica después filtros + "una posición a la vez" de forma exacta.
    htf_ema: [(cierre_ms, ema)]; ctx_rows / btc_rows: velas del TF de contexto del símbolo y de BTC."""
    exits = exits or [exit_variant(cfg)]
    eng = WyckoffEngine(tf_s, tick, strict, keep_bars=100000, range_effort=range_effort)
    ctx = WyckoffEngine(ctx_tf_s, tick, strict, keep_bars=100000, range_effort=range_effort) if ctx_rows else None
    btc = WyckoffEngine(ctx_tf_s, 0.1, strict, keep_bars=100000) if (btc_rows and ctx_tf_s) else None
    j = k = kb = 0
    ema = float("nan")
    cands, opens = [], []
    tf_ms = tf_s * 1000
    ctx_ms = (ctx_tf_s or 0) * 1000

    def feed(e, rws, kk, until):
        while e is not None and kk < len(rws) and rws[kk][0] + ctx_ms <= until:
            r = rws[kk]
            if not (r[5] <= 0 and r[2] == r[3]):
                e.update(*r[:6])
            kk += 1
        return kk

    for idx, row in enumerate(rows):
        t, o, h, l, c, v = row[:6]
        if v <= 0 and h == l:
            continue  # mercado cerrado (TradFi): igual que en el bot, no alimenta al motor
        d = eng.update(t, o, h, l, c, v)
        if timeline is not None:
            timeline[0].append(t + tf_ms)
            timeline[1].append(wyckoff_state(d))
        still = []
        for cand, key, sim in opens:
            r = sim.step(idx, h, l, c, d["atr"])
            if r is None:
                still.append((cand, key, sim))
            else:
                cand["res"][key] = {"r": r, "close_t": t + tf_ms, "reason": sim.reason, "bars": idx - sim.bar}
        opens = still
        bar_close = t + tf_ms
        while htf_ema and j < len(htf_ema) and htf_ema[j][0] <= bar_close:
            ema = htf_ema[j][1]
            j += 1
        k = feed(ctx, ctx_rows, k, bar_close)
        kb = feed(btc, btc_rows, kb, bar_close)
        if idx < warmup or not (d["entry_now"] or d["fail"]):
            continue
        is_fail = not d["entry_now"]
        mk = (lambda m: build_fail_signal(d["fail"], c, d["atr"], d["time"], cfg, tick, m)) if is_fail else \
             (lambda m: build_signal(d, cfg, tick, m))
        base = mk(exits[0][0])
        if base is None:
            continue
        base["flow"], base["flow_exc"] = flow_features(
            base["side"], rows[max(0, idx - 40):idx + 1],
            None if is_fail else (d["excT"] if not na(d["excT"]) else d["testT"]))
        cdir, clabel = context_of(ctx.last if ctx is not None else None)
        bdir, blabel = context_of(btc.last if btc is not None else None)
        look = max(int(30 * 86400 / tf_s), 50)  # movimiento previo: cambio de precio en los 30 días anteriores
        prior = round((c / rows[idx - look][4] - 1) * 100, 1) if idx >= look and rows[idx - look][4] > 0 else None
        base.update(prior_move=prior)
        base.update(symbol=symbol, trend=trend_dir(c, ema), ctx_dir=cdir, ctx_label=clabel,
                    ctx_align=alignment(base["side"], cdir), btc_label=blabel,
                    btc_align=alignment(base["side"], bdir) if btc is not None else "neutral",
                    open_t=bar_close, res={}, rr_by={})
        for key in exits:
            sig = mk(key[0])
            base["rr_by"][key] = sig["rr"]
            opens.append((base, key, TradeSim(sig, idx, cfg.FEE_PCT + cfg.SLIPPAGE_PCT, cfg.TP1_FRACTION,
                                              cfg.MOVE_SL_TO_BE, key[1], key[2])))
        cands.append(base)
    return cands


def select_trades(cands, cfg, key=None, which="main"):
    """Aplica filtros y 'una posición por símbolo a la vez' (greedy en orden temporal) para una variante
    de salida. which: main = entradas del indicador (+ trampas si FAIL_TRADES=on) · fail = solo trampas.
    Devuelve copias con r / close_t / reason de esa variante."""
    key = key or exit_variant(cfg)
    out, busy_until = [], {}
    for c in sorted(cands, key=lambda x: x["open_t"]):
        is_fail = c["kind"] == FAIL_KIND
        if which == "fail" and not is_fail:
            continue
        if which == "main" and is_fail and cfg.FAIL_TRADES != "on":
            continue
        if is_fail and cfg.FAIL_NEEDS_ENTRY and not c.get("had_entry"):
            continue
        res = c["res"].get(key)
        if res is None:
            continue  # sigue abierta al final de los datos
        sig = dict(c, rr=c["rr_by"].get(key, c["rr"]))
        if filters(sig, cfg, c["trend"], c["ctx_align"], c.get("btc_align", "neutral")):
            continue
        if cfg.BREADTH_FILTER == "bloquea" and c.get("breadth_align") == "en contra":
            continue
        if cfg.META_FILTER == "bloquea" and META.get("model") is not None and \
                META["model"].prob(c) < META["model"].thr:
            continue
        if c["open_t"] < busy_until.get(c["symbol"], 0):
            continue
        busy_until[c["symbol"]] = res["close_t"]
        sig.update(res)
        out.append(sig)
    return out
