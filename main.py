"""
Wyckoff Bot v2 — ejecuta en BingX las entradas del indicador "Wyckoff ES [theUltimator5]".

Ciclo:
  · al cierre de cada vela de cada TF de TIMEFRAMES alimenta el motor de cada símbolo con la vela cerrada
  · motor aparte en CONTEXT_TF (p. ej. 4h): la estructura mayor se registra en cada señal y puede filtrar
  · si el motor valida una ENTRADA en ESA vela → plan SL/TP1/TP2 → filtros
  · SIGNAL: avisa y lleva una operación virtual con la misma gestión y costes que el backtest
  · LIVE:   orden a mercado con el SL DENTRO de la orden + TP1 parcial + TP2; tras TP1, SL a breakeven;
            vigila en cada ciclo que la posición tenga stop y lo repone si falta
"""
import json
import logging
import os
import signal
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

import config as C
from bingx import BingX, BingXError, BingXPaused
from notify import Journal, Telegram
from universe import is_tradfi
from strategy import (FAIL_KIND, MetaModel, TradeSim, alignment, breadth_label, build_fail_signal, build_signal,
                      context_of, ema_last, filters, flow_features, ind_features, trend_dir, wyckoff_state)
from wyckoff_engine import (BIT_CTEST, BIT_SOS, BIT_SOW, BIT_SPRING, BIT_TEST, BIT_UTAD, DIR_ACCUM, PHASE_C,
                            PHASE_D, PHASE_NAMES, WyckoffEngine, has_bit)

os.makedirs(C.DATA_DIR, exist_ok=True)
logging.basicConfig(level=getattr(logging, C.LOG_LEVEL.upper(), logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s | %(message)s", stream=sys.stdout)
log = logging.getLogger("main")

STATE_PATH = os.path.join(C.DATA_DIR, "state.json")
ROWS_KEEP = 700
ALL_TFS = list(dict.fromkeys(C.TIMEFRAMES + ([C.CONTEXT_TF] if C.CONTEXT_TF else [])))


def tf_ms(tf):
    return C.tf_seconds(tf) * 1000


def now_ms():
    return int(time.time() * 1000)


def utc_day():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def dead_bar(o, h, l, c, v):
    """Vela sin negociación (mercado TradFi cerrado): precio plano y volumen cero."""
    return v <= 0 and h == l


def friday_cutoff():
    n = datetime.now(timezone.utc)
    return C.TRADFI_NO_ENTRY_FRI_UTC >= 0 and n.weekday() == 4 and n.hour >= C.TRADFI_NO_ENTRY_FRI_UTC


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class Bot:
    def __init__(self):
        self.ex = BingX(C.API_KEY, C.API_SECRET, C.VST)
        self.tg = Telegram(C.TG_TOKEN, C.TG_CHAT)
        self.journal = Journal(C.DATA_DIR)
        self.pool = ThreadPoolExecutor(max_workers=max(1, C.FETCH_WORKERS))
        self.engines = {}          # (símbolo, tf) → motor
        self.rows = {}             # (símbolo, tf) → últimas velas cerradas, para ADX / RSI / AVWAP
        self.symbols = []
        self.universe_ts = 0
        self.paused_until = {}     # símbolo → epoch hasta el que no se le piden velas (contrato pausado)
        self._block_alert = 0.0
        self.trend_cache = {}
        self.cooldown = {}
        self.running = True
        self.attach_ok = C.ATTACH_SL
        self.flow_ok = C.FLOW_SOURCE == "binance"
        self.meta = self.load_meta()
        self.state = self.load_state()
        self.last_status = time.time()

    def load_meta(self):
        if C.META_FILTER == "off":
            return None
        for path in (C.META_MODEL, os.path.join(C.DATA_DIR, os.path.basename(C.META_MODEL))):
            if os.path.exists(path):
                try:
                    m = MetaModel.load(path)
                    log.info("meta-modelo cargado de %s (umbral %.2f)", path, m.thr)
                    return m
                except (OSError, ValueError, KeyError) as e:
                    log.warning("meta-modelo %s ilegible: %s", path, e)
        return None

    def fp(self, x, sym):
        pp = self.ex.contracts.get(sym, {}).get("pp", 6)
        return f"{x:.{pp}f}"

    # ── estado persistente ──
    def load_state(self):
        base = {"positions": {}, "sims": {}, "daily": {"day": utc_day(), "r": 0.0, "n": 0},
                "stats": {"n": 0, "wins": 0, "sum_r": 0.0, "gw": 0.0, "gl": 0.0},
                "last_trade": time.time(), "idle_warned": False, "paused": False, "streak": 0, "hwm": 0.0}
        try:
            with open(STATE_PATH) as f:
                st = json.load(f)
            for k, v in base.items():
                st.setdefault(k, v)
            for rec in list(st["positions"].values()) + list(st["sims"].values()):
                rec.setdefault("tf", C.TIMEFRAME)
            return st
        except (OSError, ValueError):
            return base

    def save_state(self):
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=1, default=str)
        os.replace(tmp, STATE_PATH)

    def wr(self):
        st = self.state["stats"]
        return round(st["wins"] * 100 / st["n"]) if st["n"] else 0

    def stats_text(self):
        st = self.state["stats"]
        pf = f"{st['gw'] / st['gl']:.2f}" if st["gl"] > 0 else "∞"
        avg = st["sum_r"] / st["n"] if st["n"] else 0
        return (f"{st['n']} ops · acierto {self.wr()}% · media {avg:+.3f}R · total {st['sum_r']:+.2f}R · PF {pf}"
                + (" (muestra pequeña: un dibujo, no evidencia)" if st["n"] < 30 else ""))

    def roll_day(self):
        d = utc_day()
        if self.state["daily"]["day"] != d:
            old = self.state["daily"]
            self.tg.send(f"📅 <b>Resumen {old['day']}</b>\n{old['n']} cerradas · {old['r']:+.2f}R\n"
                         f"Acumulado: {self.stats_text()}\n"
                         f"Compara la media por operación con la del backtest en los mismos símbolos.")
            self.state["daily"] = {"day": d, "r": 0.0, "n": 0}
            self.save_state()

    def register_result(self, r):
        st, dl = self.state["stats"], self.state["daily"]
        st["n"] += 1
        st["wins"] += 1 if r > 0 else 0
        st["sum_r"] += r
        st["gw"] += r if r > 0 else 0
        st["gl"] += -r if r < 0 else 0
        dl["r"] += r
        dl["n"] += 1
        self.state["last_trade"] = time.time()
        self.state["idle_warned"] = False
        self.state["streak"] = 0 if r > 0 else self.state.get("streak", 0) + 1
        if C.LIVE and C.MAX_LOSS_STREAK and self.state["streak"] >= C.MAX_LOSS_STREAK and not self.state["paused"]:
            self.state["paused"] = True
            self.tg.send(f"🛑 {self.state['streak']} operaciones seguidas sin ganancia: bot PAUSADO (MAX_LOSS_STREAK). "
                         f"Revisa qué ha pasado y reanuda con /reanudar.")
        self.save_state()
        if dl["r"] <= -C.MAX_DAILY_LOSS_R:
            self.tg.send(f"🛑 Pérdida diaria {dl['r']:+.2f}R ≥ {C.MAX_DAILY_LOSS_R}R: sin nuevas aperturas hasta 00:00 UTC.")

    # ── universo y calentamiento ──
    def usable(self, sym):
        return self.paused_until.get(sym, 0.0) < time.time()

    def mark_paused(self, sym, tf, e, hours=6):
        if self.usable(sym):
            log.info("%s pausado en BingX (%s): sin velas durante %dh", sym, tf, hours)
        self.paused_until[sym] = time.time() + hours * 3600

    def refresh_universe(self, force=False):
        if not force and time.time() - self.universe_ts < C.UNIVERSE_REFRESH_H * 3600:
            return
        self.ex.load_contracts()
        norm = lambda s: s if "-" in s else s.replace("USDT", "-USDT")
        if C.SYMBOLS:
            syms = [norm(s) for s in C.SYMBOLS]
        else:
            rows, skipped = [], {"categoría": 0, "volumen": 0, "API cerrada": 0, "pausada": 0}
            vols = {t.get("symbol", ""): float(t.get("quoteVolume", 0) or 0) for t in self.ex.tickers()}
            for s, c in self.ex.contracts.items():
                if c["cls"] not in C.CATEGORIES:
                    skipped["categoría"] += 1
                    continue
                if not c["api_open"]:
                    skipped["API cerrada"] += 1
                    continue
                if not self.usable(s):
                    skipped["pausada"] += 1
                    continue
                qv = vols.get(s, 0.0)
                if qv < (C.MIN_QUOTE_VOL if c["cls"] == "crypto" else C.MIN_QUOTE_VOL_TRADFI):
                    skipped["volumen"] += 1
                    continue
                rows.append((qv, s))
            rows.sort(reverse=True)
            n = C.AUTO_TOP_N if C.UNIVERSE == "top" else C.MAX_SYMBOLS
            syms = [s for _, s in rows[:n]]
            self.universe_note = (f"{len(self.ex.contracts)} perpetuos en BingX · fuera por " +
                                  ", ".join(f"{k} {v}" for k, v in skipped.items() if v) +
                                  (f", tope {n}" if len(rows) > n else ""))
        bl = {norm(b) for b in C.BLACKLIST}
        syms = [s for s in syms if s in self.ex.contracts and s not in bl]
        for s in list(self.state["positions"]) + list(self.state["sims"]):
            if s not in syms and s in self.ex.contracts:
                syms.append(s)  # no abandonar lo que está abierto
        if C.CONTEXT_TF and "BTC-USDT" in self.ex.contracts and "BTC-USDT" not in syms and "crypto" in C.CATEGORIES:
            syms.append("BTC-USDT")  # contexto de BTC para las señales de alts
        jobs = [(s, tf) for s in syms for tf in ALL_TFS if (s, tf) not in self.engines]
        self.warmup_many(jobs)
        for key in list(self.engines):
            if key[0] not in syms:
                del self.engines[key]
                self.rows.pop(key, None)
        self.symbols = [s for s in syms if all((s, tf) in self.engines for tf in C.TIMEFRAMES)]
        self.universe_ts = time.time()
        missing = [s for s in syms if s not in self.symbols and self.usable(s)]
        if missing:  # fallo transitorio (p. ej. bloqueo de BingX): no esperar 6 h, reintentar en 5 min
            self.universe_ts = time.time() - C.UNIVERSE_REFRESH_H * 3600 + 300
            log.warning("%d símbolos sin motor (%s…): se reintenta en 5 min", len(missing), ", ".join(missing[:4]))
        log.info("universo: %d símbolos · %d motores", len(self.symbols), len(self.engines))
        by = {}
        for s in self.symbols:
            k = self.ex.contracts[s]["cls_label"]
            by[k] = by.get(k, 0) + 1
        txt = " · ".join(f"{k} {v}" for k, v in sorted(by.items(), key=lambda x: -x[1]))
        if txt != getattr(self, "_last_universe_txt", None):
            self._last_universe_txt = txt
            self.tg.send(f"🌐 Universo: {len(self.symbols)} símbolos ({txt})\n{getattr(self, 'universe_note', '')}")

    def warmup_many(self, jobs):
        def fetch(job):
            s, tf = job
            n = C.WARMUP_BARS if tf in C.TIMEFRAMES else C.CONTEXT_WARMUP
            if not self.usable(s):
                return job, None
            try:
                return job, self.ex.klines_history(s, tf, n + 1, tf_ms(tf))
            except BingXPaused as e:
                self.mark_paused(s, tf, e)
                return job, None
            except BingXError as e:
                if "bloqueada" not in str(e):
                    log.warning("warmup %s %s: %s", s, tf, e)
                return job, None

        for (s, tf), rows in self.pool.map(fetch, jobs):
            if rows:
                self.build_engine(s, tf, rows)

    def build_engine(self, sym, tf, rows):
        eng = WyckoffEngine(C.tf_seconds(tf), self.ex.contracts[sym]["tick"], C.ENTRY_STRICTNESS,
                            keep_bars=C.ENGINE_KEEP_BARS,
                            range_effort=is_tradfi(sym) and C.TRADFI_EFFORT == "rango")
        cut, last = now_ms(), 0
        for t, o, h, l, c, v in rows:
            if t + tf_ms(tf) <= cut:
                if not dead_bar(o, h, l, c, v):
                    eng.update(t, o, h, l, c, v)
                last = t
        eng.last_t = last
        if tf in C.TIMEFRAMES:
            self.rows[(sym, tf)] = [list(r[:6]) for r in rows if r[0] + tf_ms(tf) <= cut][-ROWS_KEEP:]
        self.engines[(sym, tf)] = eng

    # ── filtros de contexto ──
    def trend(self, sym, price):
        if C.TREND_FILTER == "off":
            return 0
        ms = tf_ms(C.TREND_TF)
        key = (sym, now_ms() // ms)
        if key not in self.trend_cache:
            try:
                rows = self.ex.klines(sym, C.TREND_TF, C.TREND_EMA * 4 + 10)
                closes = [r[4] for r in rows if r[0] + ms <= now_ms()]
                self.trend_cache = {k: v for k, v in self.trend_cache.items() if k[1] == key[1]}
                self.trend_cache[key] = ema_last(closes, C.TREND_EMA)
            except BingXError as e:
                log.warning("tendencia %s: %s", sym, e)
                return 0
        return trend_dir(price, self.trend_cache[key])

    def context(self, sym, tf):
        if not C.CONTEXT_TF or C.tf_seconds(C.CONTEXT_TF) <= C.tf_seconds(tf):
            return 0, "-"
        eng = self.engines.get((sym, C.CONTEXT_TF))
        return context_of(eng.last if eng else None)

    def flow(self, sym, tf, side, exc_ts):
        """Compra/venta agresora de Binance (BingX no la publica en sus velas). Solo cripto; solo registro/meta."""
        if not self.flow_ok or self.ex.contracts.get(sym, {}).get("cls") != "crypto":
            return None, None
        try:
            r = requests.get("https://fapi.binance.com/fapi/v1/klines",
                             params={"symbol": sym.replace("-", ""), "interval": tf, "limit": 42}, timeout=8)
            if r.status_code == 451:
                self.flow_ok = False
                self.tg.send("ℹ Binance bloquea esta región (HTTP 451): sin flujo agresor. Cambia la región del "
                             "servicio en Railway a Europa para tenerlo.")
                return None, None
            if r.status_code != 200:
                return None, None
            ms = tf_ms(tf)
            rows = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), float(k[9])]
                    for k in r.json() if int(k[0]) + ms <= now_ms()]
            return flow_features(side, rows, exc_ts)
        except (requests.RequestException, ValueError, IndexError) as e:
            log.debug("flujo %s: %s", sym, e)
            return None, None

    def breadth(self, sym, tf, side):
        st = [wyckoff_state(e.last) for (s, t), e in self.engines.items() if t == tf and s != sym]
        if not st:
            return None
        return round(sum(st) / len(st) * (1 if side == "LONG" else -1), 4)

    def btc_context(self, sym, tf):
        if sym == "BTC-USDT" or not C.CONTEXT_TF or C.tf_seconds(C.CONTEXT_TF) <= C.tf_seconds(tf):
            return 0, "-"
        eng = self.engines.get(("BTC-USDT", C.CONTEXT_TF))
        return context_of(eng.last if eng else None) if eng else (0, "-")

    def funding(self, sym):
        """Registro (no filtro): quién paga el funding en el momento de la señal = qué lado está amontonado."""
        try:
            fr = self.ex.funding_rate(sym)
        except Exception as e:  # es solo registro: nunca debe impedir la señal
            log.debug("funding %s: %s", sym, e)
            return None, ""
        if fr is None:
            return None, ""
        pct = fr * 100
        tag = "largos amontonados" if pct >= 0.03 else "cortos amontonados" if pct <= -0.03 else "neutro"
        return round(pct, 4), f"{pct:+.4f}%/8h ({tag})"

    # ── cierre de velas ──
    def process_tf(self, tf):
        cut, ms = now_ms(), tf_ms(tf)
        # primero los símbolos con estructura en Fase C/D: son los que pueden dar entrada en esta vela
        syms = sorted((s for s in self.symbols if (s, tf) in self.engines and self.usable(s)),
                      key=lambda s: 0 if (self.engines[(s, tf)].last or {}).get("phase", 0) in (PHASE_C, PHASE_D) else 1)

        blocked = []

        def fetch(sym):
            try:
                return sym, self.ex.klines(sym, tf, 6)
            except BingXPaused as e:
                self.mark_paused(sym, tf, e)
                return sym, None
            except BingXError as e:
                if "bloqueada" in str(e):
                    blocked.append(str(e))
                else:
                    log.warning("klines %s %s: %s", sym, tf, e)
                return sym, None

        trading = tf in C.TIMEFRAMES
        watch, rebuild = [], []
        results = list(self.pool.map(fetch, syms))
        if blocked and time.time() - self._block_alert > 600:
            self._block_alert = time.time()
            self.tg.send(f"⚠ BingX limita las peticiones de velas ({len(blocked)} símbolos sin datos en esta vela {tf}). "
                         f"Se reintenta solo cuando BingX lo permita. Si se repite: menos símbolos o CATEGORIES=crypto.")
        for sym, rows in results:
            eng = self.engines.get((sym, tf))
            if rows is None or eng is None:
                continue
            fresh = [r for r in rows if r[0] > eng.last_t and r[0] + ms <= cut]
            if len(fresh) > 4:  # hueco grande (caída/redeploy): reconstruir
                rebuild.append((sym, tf))
                continue
            d = None
            for t, o, h, l, c, v in fresh:
                eng.last_t = t
                if trading:
                    buf = self.rows.setdefault((sym, tf), [])
                    buf.append([t, o, h, l, c, v])
                    del buf[:-ROWS_KEEP]
                if dead_bar(o, h, l, c, v):
                    continue  # mercado cerrado (TradFi): no alimenta al motor
                d = eng.update(t, o, h, l, c, v)
                if trading:
                    self.step_sim(sym, tf, t, h, l, c, d["atr"])
                    self.manage_bar(sym, tf, c, d["atr"])
            if d is None or not trading:
                continue
            note = self.event_note(d)
            if note:
                watch.append(f"{sym} {tf} {note}")
            if d["entry_now"] and d["time"] == fresh[-1][0]:
                self.handle_entry(sym, tf, d)
            elif d.get("fail") and d["time"] == fresh[-1][0] and C.FAIL_TRADES != "off":
                self.handle_entry(sym, tf, d, fail=True)
        if rebuild:
            self.warmup_many(rebuild)
        if watch:
            self.tg.send("👀 <b>Eventos Wyckoff</b>\n" + "\n".join(watch[:25]))

    @staticmethod
    def event_note(d):
        sig = d["sig"]
        long = d["wdir"] == DIR_ACCUM
        side = "LONG" if long else "SHORT"
        if has_bit(sig, BIT_SPRING):
            return f"Spring → preparando {side} (falta Test/SOS)"
        if has_bit(sig, BIT_UTAD):
            return f"UTAD → preparando {side} (falta Test/SOW)"
        if (has_bit(sig, BIT_TEST) or has_bit(sig, BIT_CTEST)) and not d["entry_now"]:
            return f"Test Fase C sin entrada (madurez {d['conf']}, validación {d['val']})"
        if (has_bit(sig, BIT_SOS) or has_bit(sig, BIT_SOW)) and not d["entry_now"]:
            return f"{'SOS' if long else 'SOW'} → Fase D, esperando {'LPS' if long else 'LPSY'}"
        return ""

    # ── señales ──
    def handle_entry(self, sym, tf, d, fail=False):
        tick = self.ex.contracts[sym]["tick"]
        sig = build_fail_signal(d["fail"], d["close"], d["atr"], d["time"], C, tick) if fail else build_signal(d, C, tick)
        if sig is None or time.time() - self.cooldown.get(sym, 0) < C.SIGNAL_COOLDOWN_MIN * 60:
            return
        self.cooldown[sym] = time.time()
        sig["tf"] = tf
        buf = self.rows.get((sym, tf), [])
        sig.update(ind_features(buf, len(buf) - 1, sig["side"], d.get("climT"), C.ADX_LEN, C.RSI_LEN))
        sig["trend"] = self.trend(sym, sig["entry"])
        cdir, clabel = self.context(sym, tf)
        sig["ctx_label"] = clabel
        cinfo = self.ex.contracts[sym]
        bdir, blabel = self.btc_context(sym, tf) if cinfo["cls"] == "crypto" else (0, "-")
        sig["btc_label"] = blabel
        sig["funding"], flabel = self.funding(sym)
        sig["flow"], sig["flow_exc"] = self.flow(sym, tf, sig["side"], None if fail else (
            d["excT"] if d["excT"] == d["excT"] else d["testT"]))
        sig["breadth"] = self.breadth(sym, tf, sig["side"])
        sig["breadth_align"] = breadth_label(sig["breadth"])
        why = filters(sig, C, sig["trend"], alignment(sig["side"], cdir),
                      alignment(sig["side"], bdir) if blabel != "-" else "neutral")
        sig["meta_p"] = round(self.meta.prob(sig), 3) if self.meta else None
        if fail and C.FAIL_TRADES == "aviso":
            why.append("FAIL_TRADES=aviso (solo aviso, se registra para medir)")
        if C.BREADTH_FILTER == "bloquea" and sig["breadth_align"] == "en contra":
            why.append("amplitud Wyckoff en contra")
        if self.meta and C.META_FILTER == "bloquea" and sig["meta_p"] < self.meta.thr:
            why.append(f"meta-modelo p={sig['meta_p']:.2f} < {self.meta.thr:.2f}")
        if C.MAX_SAME_SIDE > 0:
            book = self.state["positions"] if C.LIVE else self.state["sims"]
            same = sum(1 for r in book.values() if r.get("side") == sig["side"])
            if same >= C.MAX_SAME_SIDE:
                why.append(f"ya hay {same} {sig['side']} abiertas (MAX_SAME_SIDE): en un desplome las alts se mueven como una")
        if self.state["paused"]:
            why.append("bot en pausa (/reanudar)")
        if sym in self.state["sims"] or sym in self.state["positions"]:
            why.append("ya hay una operación en este símbolo")
        if cinfo["cls"] != "crypto":
            if friday_cutoff():
                why.append(f"TradFi en viernes ≥{C.TRADFI_NO_ENTRY_FRI_UTC}h UTC: el SL no se ejecuta con el mercado cerrado")
            if not cinfo["api_open"]:
                why.append("BingX tiene cerrada la apertura por API")
        if C.LIVE:
            why += self.live_blockers(sym)
        icon = ("🪤" if fail else "") + ("🟢" if sig["side"] == "LONG" else "🔴")
        ctx_icon = {"a favor": "✅", "en contra": "⚠", "neutral": "·"}[sig["ctx_align"]]
        cls = "" if cinfo["cls"] == "crypto" else f" · {cinfo['cls_label']} {cinfo['name']}"
        txt = (f"{icon} <b>{sig['side']} {sym}</b>{cls} · {tf} · {sig['kind']}\n"
               f"Entrada {self.fp(sig['entry'], sym)} · SL {self.fp(sig['sl'], sym)} ({sig['risk_pct']:.2f}%)\n"
               f"TP1 {self.fp(sig['tp1'], sym)} · TP2 {self.fp(sig['tp2'], sym)} · R:R {sig['rr']:.2f}\n"
               f"Rango {self.fp(sig['rl'], sym)} – {self.fp(sig['rh'], sym)} · madurez {sig['conf']} · validación {sig['val']}\n"
               f"{ctx_icon} Contexto {C.CONTEXT_TF or '-'}: {clabel} ({sig['ctx_align']})"
               + (f"\n{ {'a favor': '✅', 'en contra': '⚠', 'neutral': '·'}[sig['btc_align']] } BTC {C.CONTEXT_TF}: {blabel} ({sig['btc_align']})"
                  if blabel != "-" else "")
               + (f"\n💸 Funding {flabel}" if flabel else "")
               + (f"\n🌊 Flujo agresor {sig['flow']:+.2f}" + (f" · en el Spring/UTAD {sig['flow_exc']:+.2f}"
                                                              if sig["flow_exc"] is not None else "")
                  if sig["flow"] is not None else "")
               + (f"\n📐 ADX {sig['adx']:.0f}" + (f" · RSI {sig['rsi_gain']:+.0f} desde el clímax" if sig["rsi_gain"] is not None else "")
                  + f" · AVWAP {sig['avwap_align']}" if sig.get("adx") is not None else "")
               + (f"\n🧭 Amplitud Wyckoff {sig['breadth']:+.2f} ({sig['breadth_align']})" if sig["breadth"] is not None else "")
               + (f"\n🧠 Meta-modelo p={sig['meta_p']:.2f} (umbral {self.meta.thr:.2f})" if sig["meta_p"] is not None else "")
               + (f"\n🪤 Estructura rota: los del {'Spring' if sig['side'] == 'SHORT' else 'UTAD'} quedan atrapados"
                  if fail else "")
               + (f"\n⚠ contra EMA{C.TREND_EMA} {C.TREND_TF}" if sig.get("against_trend") else ""))
        if why:
            self.tg.send(txt + "\n⏸ <i>No se abre: " + "; ".join(dict.fromkeys(why)) + "</i>")
            return
        if not C.LIVE:
            self.state["sims"][sym] = {**sig, "bar_t": d["time"], "half": False, "open_ts": time.time()}
            self.save_state()
            self.tg.send(txt + "\n📝 SIGNAL: operación virtual abierta")
            return
        self.open_live(sym, sig, txt)

    def step_sim(self, sym, tf, t, h, l, c, atr):
        s = self.state["sims"].get(sym)
        if not s or s.get("tf") != tf or t <= s["bar_t"]:
            return
        sim = TradeSim(s, 0, C.FEE_PCT + C.SLIPPAGE_PCT, C.TP1_FRACTION, C.MOVE_SL_TO_BE, C.TRAIL_ATR, C.TIME_STOP_BARS)
        sim.half = s["half"]
        sim.s = s.get("stop_now", s["entry"] if (s["half"] and C.MOVE_SL_TO_BE) else s["sl"])
        s["nbars"] = s.get("nbars", 0) + 1
        r = sim.step(s["nbars"], h, l, c, atr)
        s["stop_now"] = sim.s
        if sim.half and not s["half"]:
            s["half"] = True
            self.tg.send(f"🎯 Virtual {sym} TP1 tocado" + (" · SL a breakeven" if C.MOVE_SL_TO_BE else ""))
        if r is None:
            self.save_state()
            return
        s["exit_reason"] = sim.reason
        del self.state["sims"][sym]
        self.register_result(r)
        self.journal.write({"open_time": iso(s["open_ts"]), "close_time": iso(time.time()), "symbol": sym,
                            "tf": tf, "side": s["side"], "kind": s["kind"], "entry_expected": s["entry"],
                            "entry_real": s["entry"], "sl": s["sl"], "tp1": s["tp1"], "tp2": s["tp2"],
                            "rr_plan": round(s["rr"], 2), "r_net": round(r, 3),
                            "minutes": round((time.time() - s["open_ts"]) / 60), "conf": s["conf"], "val": s["val"],
                            "against_trend": s.get("against_trend"), "ctx_align": s.get("ctx_align"),
                            "ctx_label": s.get("ctx_label"), "btc_align": s.get("btc_align"),
                            "funding": s.get("funding"), "range_atr": s.get("range_atr"), "b_bars": s.get("b_bars"),
                            "flow": s.get("flow"), "flow_exc": s.get("flow_exc"), "breadth": s.get("breadth"),
                            "adx": s.get("adx"), "rsi_gain": s.get("rsi_gain"), "avwap_align": s.get("avwap_align"),
                            "meta_p": s.get("meta_p"),
                            "exit_reason": sim.reason, "mode": "SIGNAL"})
        self.tg.send(f"{'✅' if r > 0 else '❌'} Virtual {s['side']} {sym} cerrada por {sim.reason}: {r:+.2f}R "
                     f"(hoy {self.state['daily']['r']:+.2f}R · total {self.state['stats']['sum_r']:+.2f}R)")

    # ── LIVE ──
    def live_blockers(self, sym):
        why = []
        if self.state["daily"]["r"] <= -C.MAX_DAILY_LOSS_R:
            why.append(f"pérdida diaria {self.state['daily']['r']:+.2f}R")
        if len(self.state["positions"]) >= C.MAX_CONCURRENT:
            why.append(f"máximo del bot ({C.MAX_CONCURRENT})")
        try:
            allpos = self.ex.positions()
            if len(allpos) >= C.MAX_TOTAL_POSITIONS:
                why.append(f"máximo de la cuenta ({len(allpos)}/{C.MAX_TOTAL_POSITIONS})")
            if any(p.get("symbol") == sym for p in allpos):
                why.append("la cuenta ya tiene posición en este símbolo")
        except BingXError as e:
            why.append(f"no se pudo leer posiciones ({e})")
        return why

    def open_live(self, sym, sig, txt):
        ex = self.ex
        long = sig["side"] == "LONG"
        d = 1 if long else -1
        try:
            price = ex.price(sym)
            equity, avail = ex.balance()
        except BingXError as e:
            self.tg.send(txt + f"\n⚠ sin precio/balance: {e}")
            return
        risk = abs(sig["entry"] - sig["sl"])
        adv = (price - sig["entry"]) * d / risk
        if adv > C.CHASE_MAX_R or (price - sig["sl"]) * d <= 0:
            self.tg.send(txt + f"\n⏸ No se persigue: el precio ya avanzó {adv:+.2f}R")
            return
        if C.NOTIONAL_USDT > 0:
            qty = C.NOTIONAL_USDT / price  # tamaño fijo: el riesgo en USDT es ≈ NOTIONAL × distancia al stop
        else:
            qty = equity * C.RISK_PCT / 100.0 / abs(price - sig["sl"])
        qty = ex.fmt_qty(sym, min(qty, avail * C.LEVERAGE * 0.9 / price))
        c = ex.contracts[sym]
        if qty <= 0 or qty < c["min_qty"] or qty * price < max(c["min_usdt"], 2.0):
            self.tg.send(txt + f"\n⏸ Tamaño insuficiente ({qty}) para equity {equity:.2f}")
            return
        if sig["risk_pct"] * C.LEVERAGE > C.MAX_LIQ_USE_PCT:
            self.tg.send(txt + f"\n⏸ Stop a {sig['risk_pct']:.2f}% con x{C.LEVERAGE}: demasiado cerca de la liquidación "
                               f"(MAX_LIQ_USE_PCT={C.MAX_LIQ_USE_PCT:.0f}). Baja LEVERAGE.")
            return
        ex.set_margin_mode(sym, C.MARGIN_MODE)
        ex.set_leverage(sym, C.LEVERAGE)
        cid = f"wyk{int(time.time())}{sym.split('-')[0][:6]}"
        sent = False
        for attach in ([True, False] if self.attach_ok else [False]):
            try:
                ex.market_open(sym, long, qty, cid + ("a" if attach else "b"), stop_loss=sig["sl"] if attach else None)
                sent = True
                break
            except BingXError as e:
                time.sleep(2)
                if ex.order_exists(sym, cid + ("a" if attach else "b")):
                    log.warning("respuesta perdida pero la orden existe")
                    sent = True
                    break
                if attach and not str(e).startswith("red"):
                    log.warning("BingX rechaza el SL adjunto (%s); se pone aparte", e)
                    self.attach_ok = False
                    continue
                self.tg.send(txt + f"\n❌ Orden rechazada: {e}")
                return
        if not sent:
            return
        pos = None
        for _ in range(8):
            time.sleep(1)
            try:
                pos = self.find_pos(sym, long)
            except BingXError:
                pos = None
            if pos:
                break
        if not pos:
            self.tg.send(txt + "\n🚨 Orden enviada pero la posición no aparece. REVISA BINGX A MANO.")
            return
        amt = abs(float(pos["positionAmt"]))
        entry = float(pos.get("avgPrice", price) or price)
        plan_risk = max(abs(sig["entry"] - sig["sl"]), 1e-12)
        slip_r = (entry - sig["entry"]) * d / plan_risk  # > 0 = entrada peor que el plan
        if C.MAX_SLIP_R > 0 and slip_r > C.MAX_SLIP_R:
            try:
                ex.market_close(sym, long, amt)
                msg = f"⏸ Relleno {slip_r:.2f}R peor que el plan (MAX_SLIP_R={C.MAX_SLIP_R}): cerrada al instante."
            except BingXError as e:
                msg = f"🚨 Relleno {slip_r:.2f}R peor que el plan y NO se pudo cerrar ({e}). CIERRA A MANO."
            time.sleep(1)
            for o in self._stops(sym, long):
                ex.cancel(sym, o.get("orderId"))
            self.tg.send(txt + "\n" + msg)
            return
        rec = {**sig, "qty": amt, "entry_real": entry, "risk": abs(entry - sig["sl"]), "be": False,
               "open_ts": time.time(), "cid": cid, "sl_id": "", "tp1_id": "", "tp2_id": ""}
        if not self.ensure_sl(sym, rec, amt, at_open=True):
            return
        q1 = ex.fmt_qty(sym, amt * C.TP1_FRACTION)
        q2 = ex.fmt_qty(sym, amt - q1)
        split = q1 > 0 and q2 > 0 and q1 >= c["min_qty"] and q2 >= c["min_qty"]
        err = None
        for _ in range(3):  # reintento solo ante rechazo claro; un fallo de red podría haber creado la orden
            try:
                if split:
                    if not rec["tp1_id"]:
                        rec["tp1_id"] = ex.exit_order(sym, long, "TAKE_PROFIT_MARKET", q1, sig["tp1"])
                    if not rec["tp2_id"]:
                        rec["tp2_id"] = ex.exit_order(sym, long, "TAKE_PROFIT_MARKET", q2, sig["tp2"])
                elif not rec["tp2_id"]:
                    rec["single"] = True  # posición demasiado pequeña para partirla: una sola salida
                    rec["tp2_id"] = ex.exit_order(sym, long, "TAKE_PROFIT_MARKET", amt,
                                                  sig["tp1"] if C.SINGLE_TP == "tp1" else sig["tp2"])
                err = None
                break
            except BingXError as e:
                err = e
                if str(e).startswith("red"):
                    break
                time.sleep(1.5)
        if err:
            self.tg.send(f"⚠ {sym}: TP no colocado ({err}). El SL sí está puesto; cierra o coloca el TP a mano.")
        self.state["positions"][sym] = rec
        self.save_state()
        slip = (entry - sig["entry"]) / sig["entry"] * 100 * d
        self.tg.send(txt + f"\n✅ <b>LIVE</b> abierta {amt} @ {self.fp(entry, sym)} · posición {amt * entry:.2f} USDT · "
                     f"riesgo ≈ {amt * abs(entry - sig['sl']):.2f} USDT"
                     + (" · sin TP1 parcial (tamaño mínimo)" if rec.get("single") else "") + f" (desliz. {slip:+.3f}%)"
                     + (" · SL en la orden" if self.attach_ok else ""))

    def guard(self):
        """Cortacircuitos de equity: si cae MAX_DD_PCT % desde su máximo, pausa las aperturas."""
        if not C.LIVE or C.MAX_DD_PCT <= 0:
            return
        try:
            eq, _ = self.ex.balance()
        except BingXError as e:
            log.warning("guard: %s", e)
            return
        hw = max(self.state.get("hwm", 0.0), eq)
        if hw != self.state.get("hwm"):
            self.state["hwm"] = hw
            self.save_state()
        dd = (hw - eq) / hw * 100 if hw > 0 else 0.0
        if dd >= C.MAX_DD_PCT and not self.state["paused"]:
            self.state["paused"] = True
            self.save_state()
            self.tg.send(f"🛑 <b>Equity {eq:.2f} USDT: -{dd:.1f}% desde el máximo ({hw:.2f})</b> ≥ {C.MAX_DD_PCT}%. "
                         f"Bot PAUSADO. Si has retirado fondos usa /hwm; si no, revisa antes de /reanudar.")

    def weekend_close(self):
        """Viernes tarde: CIERRA las TradFi abiertas (con el mercado cerrado el SL no se ejecuta y el lunes hay gap)."""
        n = datetime.now(timezone.utc)
        if not (C.LIVE and C.TRADFI_CLOSE_FRI_UTC >= 0 and n.weekday() == 4 and n.hour >= C.TRADFI_CLOSE_FRI_UTC):
            return
        for sym, rec in list(self.state["positions"].items()):
            if not is_tradfi(sym) or rec.get("wk_tries", 0) >= 3:
                continue
            long = rec["side"] == "LONG"
            rec["wk_tries"] = rec.get("wk_tries", 0) + 1
            try:
                pos = self.find_pos(sym, long)
                if pos is None:
                    continue
                self.ex.market_close(sym, long, abs(float(pos["positionAmt"])))
                rec["force_reason"] = "fin de semana"
                self.save_state()
                self.tg.send(f"📆 {sym}: cerrada a mercado antes del fin de semana (TRADFI_CLOSE_FRI_UTC={C.TRADFI_CLOSE_FRI_UTC}).")
            except BingXError as e:
                self.tg.send(f"⚠ {sym}: no se pudo cerrar antes del fin de semana ({e}). Intento {rec['wk_tries']}/3; "
                             f"si falla, ciérrala a mano.")

    def manual_close(self, which):
        if not C.LIVE:
            return "Modo SIGNAL: no hay posiciones reales."
        w = which.upper().replace("-USDT", "").replace("USDT", "")
        syms = [s for s in self.state["positions"] if which == "all" or s.split("-")[0] == w]
        if not syms:
            return "No hay posiciones del bot que coincidan."
        out = []
        for sym in syms:
            rec = self.state["positions"][sym]
            long = rec["side"] == "LONG"
            try:
                pos = self.find_pos(sym, long)
                if pos is None:
                    out.append(f"{sym}: ya no estaba abierta")
                    continue
                self.ex.market_close(sym, long, abs(float(pos["positionAmt"])))
                rec["force_reason"] = "manual (Telegram)"
                self.save_state()
                out.append(f"{sym}: cierre a mercado enviado")
            except BingXError as e:
                out.append(f"{sym}: ERROR {e} → ciérrala a mano")
        return "🧯 " + "\n".join(out)

    def real_r(self, sym, rec):
        """R neta con el PnL real (realizado + comisiones + funding) del libro de ingresos de BingX."""
        try:
            tot = self.ex.realized_pnl(sym, int(rec["open_ts"] * 1000) - 60_000)
        except BingXError as e:
            log.warning("income %s: %s", sym, e)
            return None
        if tot is None:
            return None
        return tot / max(rec["qty"] * rec["risk"], 1e-12)

    def manage_bar(self, sym, tf, c, atr):
        """Al cierre de cada vela del TF de la posición: salida por tiempo y trailing tras TP1 (si están activos)."""
        rec = self.state["positions"].get(sym)
        if not C.LIVE or not rec or rec.get("tf") != tf:
            return
        rec["nbars"] = rec.get("nbars", 0) + 1
        long = rec["side"] == "LONG"
        d = 1 if long else -1
        try:
            pos = self.find_pos(sym, long)
        except BingXError:
            return
        if pos is None:
            return  # el ciclo de gestión registrará el cierre
        amt = abs(float(pos["positionAmt"]))
        if C.TIME_STOP_BARS and not rec.get("half") and rec["nbars"] >= C.TIME_STOP_BARS:
            try:
                self.ex.market_close(sym, long, amt)
                rec["force_reason"] = "tiempo"
                self.tg.send(f"⏱ {sym}: {rec['nbars']} velas sin tocar TP1 → cierre por tiempo")
            except BingXError as e:
                self.tg.send(f"⚠ {sym}: no se pudo cerrar por tiempo ({e})")
            return
        if C.TRAIL_ATR > 0 and rec.get("half") and atr == atr:
            cur = rec.get("trail_stop") or (rec["entry_real"] if rec["be"] else rec["sl"])
            new = self.ex.fmt_px(sym, c - d * C.TRAIL_ATR * atr)
            if (new - cur) * d > self.ex.contracts[sym]["tick"]:
                for o in self._stops(sym, long):
                    self.ex.cancel(sym, o.get("orderId"))
                rec["trail_stop"], rec["sl_id"] = new, ""
                self.ensure_sl(sym, rec, amt, quiet=True)
                self.save_state()

    def ensure_sl(self, sym, rec, amt, at_open=False, quiet=False):
        """Garantiza que la posición tiene stop. Si no lo tiene, lo pone; si no puede, cierra (al abrir) o avisa."""
        long = rec["side"] == "LONG"
        try:
            stops = self.ex.stop_orders(sym, long)
        except BingXError as e:
            log.warning("stop_orders %s: %s", sym, e)
            return True  # no se pudo comprobar: se reintenta en el próximo ciclo
        if stops:
            rec["sl_id"] = str(stops[0].get("orderId", rec.get("sl_id", "")))
            return True
        level = rec.get("trail_stop") or (rec["entry_real"] if rec["be"] else rec["sl"])
        try:
            rec["sl_id"] = self.ex.exit_order(sym, long, "STOP_MARKET", amt, level)
            if not at_open and not quiet:
                self.tg.send(f"🛡 {sym}: faltaba el stop y se ha repuesto en {self.fp(level, sym)}")
            return True
        except BingXError as e:
            if str(e).startswith("red"):  # sin respuesta: puede que el stop sí se creara
                time.sleep(2)
                again = self._stops(sym, long)
                if again:
                    rec["sl_id"] = str(again[0].get("orderId", ""))
                    return True
            if at_open:
                try:
                    self.ex.market_close(sym, long, amt)
                    self.tg.send(f"🚨 {sym}: no se pudo poner el SL ({e}). Posición cerrada a mercado por seguridad.")
                except BingXError as e2:
                    self.tg.send(f"🚨 {sym}: SIN SL y no se pudo cerrar ({e2}). CIERRA A MANO YA.")
                return False
            self.tg.send(f"🚨 {sym}: SIN STOP ({e}). Pon el SL a mano en BingX YA.")
            return True

    def _stops(self, sym, long):
        try:
            return self.ex.stop_orders(sym, long)
        except BingXError:
            return []

    def find_pos(self, sym, long):
        for p in self.ex.positions(sym):
            amt = float(p.get("positionAmt", 0))
            ps = str(p.get("positionSide", "BOTH")).upper()
            if (ps == "LONG" and long) or (ps == "SHORT" and not long):
                return p
            if ps == "BOTH" and ((amt > 0) == long):
                return p
        return None

    def order_fill(self, sym, oid):
        if not oid:
            return None
        try:
            o = self.ex.get_order(sym, order_id=oid)
            if str(o.get("status", "")).upper() == "FILLED":
                return float(o.get("avgPrice", 0) or o.get("stopPrice", 0) or 0) or None
        except BingXError:
            pass
        return None

    def manage(self):
        if not C.LIVE or not self.state["positions"]:
            return
        ex = self.ex
        for sym, rec in list(self.state["positions"].items()):
            long = rec["side"] == "LONG"
            try:
                pos = self.find_pos(sym, long)
            except BingXError as e:
                log.warning("manage %s: %s", sym, e)
                continue
            if pos is None:
                # una lectura vacía puntual de la API NO puede cancelar el SL de una posición viva
                rec["gone"] = rec.get("gone", 0) + 1
                if rec["gone"] >= C.GONE_CONFIRMS:
                    self.close_record(sym, rec)
                continue
            rec["gone"] = 0
            amt = abs(float(pos["positionAmt"]))
            if not rec.get("half") and amt <= rec["qty"] * (1 - C.TP1_FRACTION) * 1.02:
                rec["half"] = True
                # el SL original cubre la cantidad completa: se sustituye por uno de la cantidad restante
                for o in [{"orderId": rec.get("sl_id")}] + self._stops(sym, long):
                    if o.get("orderId"):
                        ex.cancel(sym, o["orderId"])
                rec["sl_id"], rec["be"] = "", C.MOVE_SL_TO_BE
                self.tg.send(f"🎯 {sym} TP1 tocado" + (f" · SL a breakeven {self.fp(rec['entry_real'], sym)}"
                                                        if C.MOVE_SL_TO_BE else ""))
                self.save_state()
                self.ensure_sl(sym, rec, amt, quiet=True)  # nuevo SL (BE) con la cantidad restante
            self.ensure_sl(sym, rec, amt)  # guardián: repone el stop si falta
            hours = (time.time() - rec["open_ts"]) / 3600
            if hours > C.ZOMBIE_ALERT_HOURS and not rec.get("zombie"):
                rec["zombie"] = True
                self.save_state()
                self.tg.send(f"🧟 {sym} lleva {hours:.0f}h abierta. Decide tú si cerrarla.")

    def close_record(self, sym, rec):
        ex = self.ex
        long = rec["side"] == "LONG"
        d = 1 if long else -1
        e, risk = rec["entry_real"], max(rec["risk"], 1e-12)
        f = C.TP1_FRACTION
        r1 = (rec["tp1"] - e) * d / risk
        px_tp2 = self.order_fill(sym, rec.get("tp2_id"))
        px_sl = self.order_fill(sym, rec.get("sl_id"))
        if px_tp2:
            exit_px, reason = px_tp2, ("TP (salida única)" if rec.get("single") else "TP2")
        elif px_sl:
            exit_px, reason = px_sl, ("trailing" if rec.get("trail_stop") else "BE" if rec.get("be") else "SL")
        else:
            try:
                exit_px = ex.price(sym)
            except BingXError:
                exit_px = e
            reason = rec.get("force_reason", "externo/manual")
        r_exit = (exit_px - e) * d / risk
        r = (f * r1 + (1 - f) * r_exit) if rec.get("half") else r_exit
        r -= 2.0 * C.FEE_PCT / 100.0 * e / risk
        real = self.real_r(sym, rec) if C.REAL_PNL else None
        if real is not None:
            if abs(real - r) > 0.3:
                self.tg.send(f"ℹ {sym}: R estimada {r:+.2f} vs R real de la cuenta {real:+.2f} (se usa la real)")
            r = real
        for oid in (rec.get("sl_id"), rec.get("tp1_id"), rec.get("tp2_id")):
            if oid:
                ex.cancel(sym, oid)  # no dejar órdenes huérfanas
        try:
            for o in ex.stop_orders(sym, long):
                ex.cancel(sym, o.get("orderId"))
        except BingXError:
            pass
        del self.state["positions"][sym]
        self.register_result(r)
        mins = (time.time() - rec["open_ts"]) / 60
        self.journal.write({"open_time": iso(rec["open_ts"]), "close_time": iso(time.time()), "symbol": sym,
                            "tf": rec.get("tf"), "side": rec["side"], "kind": rec["kind"],
                            "entry_expected": rec["entry"], "entry_real": e,
                            "slippage_pct": round((e - rec["entry"]) / rec["entry"] * 100 * d, 4),
                            "sl": rec["sl"], "tp1": rec["tp1"], "tp2": rec["tp2"], "rr_plan": round(rec["rr"], 2),
                            "qty": rec["qty"], "exit_reason": reason, "r_net": round(r, 3), "minutes": round(mins),
                            "conf": rec["conf"], "val": rec["val"], "against_trend": rec.get("against_trend"),
                            "ctx_align": rec.get("ctx_align"), "ctx_label": rec.get("ctx_label"),
                            "btc_align": rec.get("btc_align"), "funding": rec.get("funding"),
                            "range_atr": rec.get("range_atr"), "b_bars": rec.get("b_bars"),
                            "flow": rec.get("flow"), "flow_exc": rec.get("flow_exc"), "breadth": rec.get("breadth"),
                            "adx": rec.get("adx"), "rsi_gain": rec.get("rsi_gain"), "avwap_align": rec.get("avwap_align"),
                            "meta_p": rec.get("meta_p"), "mode": "LIVE"})
        self.tg.send(f"{'✅' if r > 0 else '❌'} {rec['side']} {sym} cerrada por {reason}: {r:+.2f}R "
                     f"en {mins:.0f} min (hoy {self.state['daily']['r']:+.2f}R)")

    def reconcile(self):
        if not C.LIVE:
            if self.state["positions"]:
                self.tg.send(f"ℹ Modo SIGNAL: limpio {len(self.state['positions'])} posición(es) LIVE del estado "
                             f"anterior (revisa BingX por si siguen abiertas).")
                self.state["positions"] = {}
                self.save_state()
            return
        try:
            allpos = self.ex.positions()
        except BingXError as e:
            self.tg.send(f"⚠ reconcile: no se pudo leer posiciones ({e})")
            return
        for sym, rec in list(self.state["positions"].items()):
            try:
                gone = self.find_pos(sym, rec["side"] == "LONG") is None
            except BingXError as e:
                log.warning("reconcile %s: %s", sym, e)
                continue
            if gone:
                self.close_record(sym, rec)
        # órdenes huérfanas: SOLO en símbolos sin posición real (nunca tocar el SL/TP de una posición viva)
        live_syms = {p.get("symbol") for p in allpos}
        stateless = set()
        try:
            n = 0
            for o in self.ex.open_orders():
                cid = str(o.get("clientOrderId", o.get("clientOrderID", "")))
                s = o.get("symbol")
                if not cid.startswith("wyk"):
                    continue
                if s in live_syms:
                    if s not in self.state["positions"]:
                        stateless.add(s)
                else:
                    n += self.ex.cancel(s, o.get("orderId"))
            if n:
                self.tg.send(f"🧹 Canceladas {n} órdenes huérfanas de un despliegue anterior.")
        except BingXError as e:
            log.warning("limpieza de huérfanas: %s", e)
        if stateless:
            self.tg.send(f"🚨 Posición(es) del bot SIN estado guardado (¿falta el volumen /data?): {', '.join(sorted(stateless))}. "
                         f"Conservan sus órdenes en BingX pero el bot ya no las gestiona (TP1→BE, cierre). Revísalas a mano.")
        foreign = [p["symbol"] for p in allpos if p["symbol"] not in self.state["positions"]]
        if foreign:
            self.tg.send(f"ℹ En la cuenta hay {len(foreign)} posición(es) ajenas a este bot: {', '.join(foreign[:10])}. "
                         f"Cuentan para MAX_TOTAL_POSITIONS.")

    def weekend_watch(self):
        """Viernes: aviso único si hay TradFi abiertas, porque el fin de semana el SL no se ejecuta
        y el precio puede reabrir el lunes al otro lado del stop (gap)."""
        if not friday_cutoff() or self.state.get("weekend_warned") == utc_day():
            return
        self.state["weekend_warned"] = utc_day()
        self.save_state()
        open_tf = [f"{k} {s}" for k, book in (("LIVE", self.state["positions"]), ("virtual", self.state["sims"]))
                   for s in book if is_tradfi(s)]
        if open_tf:
            self.tg.send("📆 Viernes: TradFi abiertas que pasarán el fin de semana con el mercado cerrado: "
                         + ", ".join(open_tf) + ". El SL no protege de un gap del lunes; decide tú si cerrar.")

    # ── Telegram ──
    def commands(self):
        if not C.TG_COMMANDS:
            return
        for cmd in self.tg.poll():
            c = cmd.split()[0].split("@")[0].lower()
            if c in ("/estado", "/status"):
                self.status(force=True)
            elif c in ("/posiciones", "/pos"):
                self.tg.send(self.positions_text())
            elif c == "/stats":
                self.tg.send(f"📈 {self.stats_text()}\nHoy {self.state['daily']['r']:+.2f}R ({self.state['daily']['n']} ops)")
            elif c == "/pausa":
                self.state["paused"] = True
                self.save_state()
                self.tg.send("⏸ Pausado: no se abren operaciones nuevas. Las abiertas se siguen gestionando.")
            elif c == "/reanudar":
                self.state["paused"], self.state["streak"] = False, 0
                try:
                    if C.LIVE:
                        self.state["hwm"] = self.ex.balance()[0]  # el cortacircuitos vuelve a medir desde hoy
                except BingXError:
                    pass
                self.save_state()
                self.tg.send("▶ Reanudado (racha y máximo de equity reiniciados).")
            elif c == "/hwm":
                try:
                    self.state["hwm"] = self.ex.balance()[0]
                    self.save_state()
                    self.tg.send(f"Máximo de equity reiniciado a {self.state['hwm']:.2f} USDT.")
                except BingXError as e:
                    self.tg.send(f"No se pudo leer el equity: {e}")
            elif c == "/cerrar":
                args = cmd.split()[1:]
                self.tg.send(self.manual_close(args[0]) if args else "Uso: /cerrar BTC")
            elif c == "/cerrartodo":
                args = cmd.split()[1:]
                if args and args[0].upper() in ("SI", "SÍ"):
                    self.state["paused"] = True
                    self.save_state()
                    self.tg.send("⏸ Pausado.\n" + self.manual_close("all"))
                else:
                    self.tg.send("⚠ Cierra TODAS las posiciones del bot a mercado y pausa. Confirma con: /cerrartodo SI")
            else:
                self.tg.send("Comandos: /estado /posiciones /stats /pausa /reanudar /cerrar BTC /cerrartodo SI /hwm")

    def positions_text(self):
        lines = []
        for kind, book in (("LIVE", self.state["positions"]), ("virtual", self.state["sims"])):
            for sym, r in book.items():
                try:
                    px = self.ex.price(sym)
                    d = 1 if r["side"] == "LONG" else -1
                    e = r.get("entry_real", r["entry"])
                    upnl = (px - e) * d / max(abs(e - r["sl"]), 1e-12)
                    lines.append(f"{kind} {r['side']} {sym} {r.get('tf', '')} · {upnl:+.2f}R"
                                 + (" · TP1 hecho" if r.get("half") else ""))
                except BingXError:
                    lines.append(f"{kind} {r['side']} {sym}")
        return "📂 " + ("\n".join(lines) if lines else "Sin operaciones abiertas")

    def status(self, force=False):
        if not force and time.time() - self.last_status < C.STATUS_EVERY_H * 3600:
            return
        self.last_status = time.time()
        prep, counts = [], {}
        for (sym, tf), eng in self.engines.items():
            if tf not in C.TIMEFRAMES:
                continue
            d = eng.last or {}
            ph = d.get("phase", 0)
            counts[ph] = counts.get(ph, 0) + 1
            if ph in (PHASE_C, PHASE_D) and d.get("outcome"):
                cdir, clabel = self.context(sym, tf)
                prep.append(f"{sym} {tf}: {eng.status_text()} · ctx {clabel}")
        dist = " · ".join(f"{PHASE_NAMES[k]}:{v}" for k, v in sorted(counts.items()) if k)
        self.tg.send(f"📊 <b>Estado</b>{' ⏸ PAUSADO' if self.state['paused'] else ''} · {len(self.symbols)} símbolos"
                     f" · fases {dist or 'ninguna activa'}\n"
                     f"Abiertas: {len(self.state['positions'])} live · {len(self.state['sims'])} virtuales\n"
                     f"Hoy {self.state['daily']['r']:+.2f}R · {self.stats_text()}\n"
                     + ("\n".join(["Preparando:"] + prep[:15]) if prep else "Ninguna estructura en Fase C/D"))
        if C.LIVE and time.time() - self.state["last_trade"] > C.IDLE_ALERT_DAYS * 86400 and not self.state["idle_warned"]:
            self.state["idle_warned"] = True
            self.save_state()
            self.tg.send(f"💤 {C.IDLE_ALERT_DAYS:.0f} días sin operaciones. El bot vive, pero no opera: revisa universo y exigencia.")

    # ── bucle ──
    def run(self):
        self.tg.send(f"🚀 <b>Wyckoff Bot</b> · {C.CODE_VERSION}\n{C.summary()}")
        if C.MODE == "LIVE" and not C.LIVE:
            self.tg.send("⚠ MODE=LIVE pero CONFIRM_LIVE≠SI → arrancando en SIGNAL.")
        if C.LIVE and not (C.API_KEY and C.API_SECRET):
            self.tg.send("❌ LIVE sin BINGX_API_KEY/SECRET. Parado.")
            return
        if C.LIVE:
            if not os.path.ismount(C.DATA_DIR):
                self.tg.send(f"⚠ {C.DATA_DIR} no es un volumen persistente: el estado (posiciones, TP1→BE) se pierde en cada "
                             f"despliegue. Monta un volumen en /data antes de operar con dinero real.")
            try:
                eq, av = self.ex.balance()
            except BingXError as e:
                self.tg.send(f"❌ LIVE: no se puede leer el balance ({e}). Revisa la API key (permisos de Futuros/Trade) y su lista de IPs. Parado.")
                return
            if not self.state.get("hwm"):
                self.state["hwm"] = eq
                self.save_state()
            self.tg.send(f"💰 Cuenta {'VST (demo)' if C.VST else 'REAL'}: equity {eq:.2f} USDT · disponible {av:.2f} · "
                         f"{('posición fija ' + format(C.NOTIONAL_USDT, 'g') + ' USDT') if C.NOTIONAL_USDT > 0 else ('riesgo/operación ≈ %.2f USDT' % (eq * C.RISK_PCT / 100))} · pausa si cae {C.MAX_DD_PCT}% o {C.MAX_LOSS_STREAK} pérdidas seguidas")
        t0 = time.time()
        self.refresh_universe(force=True)
        self.reconcile()
        self.tg.send(f"✅ {len(self.engines)} estructuras reconstruidas ({len(self.symbols)} símbolos × "
                     f"{'/'.join(ALL_TFS)}) en {time.time() - t0:.0f}s.")
        next_close = {tf: (now_ms() // tf_ms(tf) + 1) * tf_ms(tf) for tf in ALL_TFS}
        last_manage = last_cmd = last_guard = 0
        while self.running:
            try:
                self.roll_day()
                self.weekend_watch()
                due = [tf for tf in ALL_TFS if now_ms() >= next_close[tf] + C.CANDLE_DELAY_S * 1000]
                if due:
                    self.refresh_universe()
                    for tf in sorted(due, key=C.tf_seconds, reverse=True):  # contexto antes que entradas
                        self.process_tf(tf)
                        next_close[tf] = (now_ms() // tf_ms(tf) + 1) * tf_ms(tf)
                    self.status()
                if time.time() - last_manage >= C.MANAGE_EVERY_S:
                    self.manage()
                    last_manage = time.time()
                if time.time() - last_guard >= 60:
                    self.guard()
                    self.weekend_close()
                    last_guard = time.time()
                if time.time() - last_cmd >= 3:
                    self.commands()
                    last_cmd = time.time()
            except Exception as e:  # un error puntual no debe tumbar el bot
                log.error("ciclo: %s\n%s", e, traceback.format_exc())
                self.tg.send(f"⚠ Error en el ciclo: {e}")
                time.sleep(10)
            time.sleep(1)


def preflight():
    """python main.py --preflight → comprueba claves, permisos y cuenta SIN enviar ninguna orden."""
    ex, ok = BingX(C.API_KEY, C.API_SECRET, C.VST), True
    print(f"{C.CODE_VERSION}\n{C.summary()}\ncuenta: {'VST (demo)' if C.VST else 'REAL'}")
    for name, fn in (("contratos", lambda: f"{len(ex.load_contracts())} perpetuos USDT"),
                     ("balance", lambda: "equity %.2f · disponible %.2f" % ex.balance()),
                     ("modo de posición", lambda: "Hedge" if ex.hedge_mode() else "One-Way"),
                     ("posiciones abiertas", lambda: ", ".join(p["symbol"] for p in ex.positions()) or "ninguna"),
                     ("órdenes abiertas", lambda: str(len(ex.open_orders())))):
        try:
            print(f"  ✓ {name}: {fn()}")
        except Exception as e:
            ok = False
            print(f"  ✗ {name}: {e}")
    print("MODE=%s CONFIRM_LIVE=%s → %s" % (C.MODE, C.CONFIRM_LIVE, "operará con órdenes REALES" if C.LIVE else "solo SIGNAL"))
    print("Telegram:", "configurado" if (C.TG_TOKEN and C.TG_CHAT) else "SIN configurar (no habrá avisos)")
    print("Volumen persistente en %s: %s" % (C.DATA_DIR, "sí" if os.path.ismount(C.DATA_DIR) else "NO"))
    return 0 if ok else 1


def main():
    if "--preflight" in sys.argv:
        return preflight()
    log.info("%s | %s", C.CODE_VERSION, C.summary())
    bot = Bot()

    def stop(*_):
        bot.running = False
        log.info("parada solicitada")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    bot.run()


if __name__ == "__main__":
    sys.exit(main())
