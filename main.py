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
# ── Autorreparación de dependencias ──
# Si Railway no instaló requirements.txt (archivo ausente/mal nombrado en el repo), se instala aquí en el arranque
# en vez de caer en bucle con "No module named 'requests'".
try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    import subprocess
    import sys as _sys
    for _extra in ([], ["--break-system-packages"]):
        try:
            subprocess.check_call([_sys.executable, "-m", "pip", "install", "--no-cache-dir", "-q", "requests>=2.31"] + _extra)
            break
        except (subprocess.CalledProcessError, OSError):
            continue
    import requests  # noqa: F401

import hashlib
import json
import logging
import os
import pickle
import signal
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

import config as C
import wyckoff_engine
from bingx import BingX, BingXError, walk_book
from notify import EventLog, Journal, Telegram
from universe import is_tradfi
from strategy import (FAIL_KIND, MetaModel, TradeSim, alignment, breadth_label, build_fail_signal, build_signal,
                      context_of, ema_last, filters, flow_features, trend_dir, wyckoff_state)
from wyckoff_engine import (BIT_AR, BIT_BC, BIT_CTEST, BIT_E, BIT_LPS, BIT_LPSY, BIT_SC, BIT_SOS, BIT_SOW, BIT_SPRING,
                            BIT_ST, BIT_TEST, BIT_UTAD, DIR_ACCUM, PHASE_C, PHASE_D, PHASE_NAMES, WyckoffEngine, has_bit)

os.makedirs(C.DATA_DIR, exist_ok=True)
logging.basicConfig(level=getattr(logging, C.LOG_LEVEL.upper(), logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s | %(message)s", stream=sys.stdout)
log = logging.getLogger("main")

STATE_PATH = os.path.join(C.DATA_DIR, "state.json")
CACHE_PATH = os.path.join(C.DATA_DIR, "engines.pkl")
OI_PATH = os.path.join(C.DATA_DIR, "oi_snap.json")
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
        self.symbols = []
        self.universe_ts = 0
        self.trend_cache = {}
        self.cooldown = {}
        self.running = True
        self.attach_ok = C.ATTACH_SL
        self.flow_ok = C.FLOW_SOURCE == "binance"
        self.meta = self.load_meta()
        self.state = self.load_state()
        self.events = EventLog(C.DATA_DIR)
        self.oi_snap = self.load_oi()
        self.oi_binance_fails = 0
        self.last_status = time.time()
        self.last_cache = time.time()

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


    # ── caché de motores: reiniciar sin repetir minutos de cálculo ──
    def engine_signature(self):
        h = hashlib.md5()
        with open(wyckoff_engine.__file__, "rb") as f:
            h.update(f.read())
        h.update(f"{C.ENTRY_STRICTNESS}|{C.ENGINE_KEEP_BARS}|{C.TRADFI_EFFORT}".encode())
        return h.hexdigest()

    def save_engines(self):
        if not C.ENGINE_CACHE or not self.engines:
            return
        try:
            tmp = CACHE_PATH + ".tmp"
            with open(tmp, "wb") as f:
                pickle.dump({"sig": self.engine_signature(), "ts": time.time(), "engines": self.engines}, f,
                            protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, CACHE_PATH)
            self.last_cache = time.time()
            log.info("caché de motores guardada (%d)", len(self.engines))
        except (OSError, pickle.PickleError) as e:
            log.warning("no se pudo guardar la caché de motores: %s", e)

    def load_engines(self):
        if not C.ENGINE_CACHE or not os.path.exists(CACHE_PATH):
            return 0
        try:
            with open(CACHE_PATH, "rb") as f:
                blob = pickle.load(f)
            if blob.get("sig") != self.engine_signature():
                log.info("caché de motores de otra versión/configuración: se descarta")
                return 0
            self.engines.update(blob["engines"])
            return len(blob["engines"])
        except Exception as e:  # caché corrupta: se reconstruye, nunca debe impedir el arranque
            log.warning("caché de motores ilegible (%s): se reconstruye", e)
            return 0

    def catch_up_many(self):
        """Pone al día los motores cargados de la caché con las velas que se perdieron mientras estaba parado."""
        def one(key):
            sym, tf = key
            eng, ms = self.engines[key], tf_ms(tf)
            gap = int((now_ms() - eng.last_t) // ms)
            if gap <= 1:
                return key, 0
            if gap > 1300:
                return key, -1  # demasiado viejo: se reconstruye entero
            try:
                rows = self.ex.klines(sym, tf, gap + 3)
            except BingXError:
                return key, -1
            n, cut = 0, now_ms()
            for t, o, h, l, c, v in rows:
                if t > eng.last_t and t + ms <= cut:
                    eng.last_t = t
                    if not dead_bar(o, h, l, c, v):
                        eng.update(t, o, h, l, c, v)
                    n += 1
            return key, n
        drop = 0
        for key, n in self.pool.map(one, list(self.engines)):
            if n < 0:
                del self.engines[key]
                drop += 1
        if drop:
            log.info("caché: %d motores demasiado antiguos, se reconstruyen", drop)


    # ── diagnóstico: ¿por qué no hay señales? Embudo de fases y de filtros ──
    HIST_KEYS = ("bars", "climax", "faseB", "exc", "test", "strength", "lps", "faseE", "entries", "pass", "rr", "far", "near")

    def tally(self, sym, eng, d):
        h = getattr(eng, "hist", None)
        if h is None:
            h = eng.hist = dict.fromkeys(self.HIST_KEYS, 0)
        h["bars"] += 1
        s = d["sig"]
        if s:
            h["climax"] += has_bit(s, BIT_SC) or has_bit(s, BIT_BC)
            h["faseB"] += has_bit(s, BIT_AR) and has_bit(s, BIT_ST)
            h["exc"] += has_bit(s, BIT_SPRING) or has_bit(s, BIT_UTAD)
            h["test"] += has_bit(s, BIT_TEST) or has_bit(s, BIT_CTEST)
            h["strength"] += has_bit(s, BIT_SOS) or has_bit(s, BIT_SOW)
            h["lps"] += has_bit(s, BIT_LPS) or has_bit(s, BIT_LPSY)
            h["faseE"] += has_bit(s, BIT_E)
        if d["entry_now"]:
            h["entries"] += 1
            sg = build_signal(d, C, self.ex.contracts[sym]["tick"])
            if sg is None:
                return
            why = filters(sg, C, 0)  # contexto neutro: solo R:R y distancia del stop
            if not why:
                h["pass"] += 1
            for w in why:
                if w.startswith("R:R"):
                    h["rr"] += 1
                elif "(>" in w:
                    h["far"] += 1
                elif "(<" in w:
                    h["near"] += 1

    def diagnostic_text(self):
        tot = dict.fromkeys(self.HIST_KEYS, 0)
        by_cls = {}
        resets, phases, n_eng, span = [0] * 7, {}, 0, 0.0
        for (sym, tf), eng in self.engines.items():
            if tf not in C.TIMEFRAMES:
                continue
            h = getattr(eng, "hist", None)
            if not h:
                continue
            n_eng += 1
            cl = by_cls.setdefault(self.ex.contracts.get(sym, {}).get("cls_label", "?"), [0, 0, 0, 0])
            cl[0] += 1
            cl[1] += h.get("climax", 0)
            cl[2] += h.get("entries", 0)
            cl[3] += h.get("pass", 0)
            for k in self.HIST_KEYS:
                tot[k] += h.get(k, 0)
            span = max(span, h["bars"] * C.tf_seconds(tf) / 86400.0)
            for i, v in enumerate(getattr(eng, "rsCounts", [0] * 7)):
                resets[i] += v
            ph = (eng.last or {}).get("phase", 0)
            phases[ph] = phases.get(ph, 0) + 1
        names = ["inválida", "sin absorción", "sin rango", "salió sin confirmar", "caducada", "degradada a B", "fin de E"]
        rs_txt = " · ".join(f"{n} {v}" for n, v in zip(names, resets) if v) or "ninguno"
        ph_txt = " · ".join(f"{PHASE_NAMES[k]}:{v}" for k, v in sorted(phases.items()) if k) or "ninguna activa"
        lines = [f"🔬 <b>Diagnóstico</b> · {len(self.symbols)} símbolos · {n_eng} motores {'/'.join(C.TIMEFRAMES)} · "
                 f"{span:.0f} días de historia",
                 f"Embudo (eventos): clímax {tot['climax']} → fase B {tot['faseB']} → Spring/UTAD {tot['exc']} → "
                 f"test {tot['test']} → SOS/SOW {tot['strength']} → LPS {tot['lps']} → fase E {tot['faseE']}",
                 f"Entradas del motor: <b>{tot['entries']}</b> · pasan R:R y stop: <b>{tot['pass']}</b> · "
                 f"bloqueadas por R:R {tot['rr']} · stop lejos {tot['far']} · stop cerca {tot['near']}",
                 "Por clase: " + " · ".join(f"{k} {v[0]} símb → {v[1]} clímax, {v[2]} entradas ({v[3]} pasan)"
                                            for k, v in sorted(by_cls.items(), key=lambda x: -x[1][0])),
                 f"Estructuras abandonadas: {rs_txt}",
                 f"Ahora en fase: {ph_txt}",
                 getattr(self, "universe_note", "")]
        if len(self.symbols) < 5:
            lines.append("⚠ Universo casi vacío: revisa MIN_QUOTE_VOL, CATEGORIES y SYMBOLS. Sin símbolos no hay señales.")
        elif n_eng and tot["climax"] == 0:
            lines.append("⚠ Ni un clímax en todo el histórico: el motor no está detectando nada. Ejecuta parity.py contra "
                         "TradingView; si el indicador sí marca SC/BC en esos gráficos, hay una diferencia de traducción.")
        elif tot["entries"] == 0:
            lines.append("⚠ Hay estructuras pero ninguna llega a entrada. Prueba ENTRY_STRICTNESS=Agresivo y/o "
                         "TIMEFRAMES=15m,1h para ver si el cuello está en la exigencia.")
        elif tot["pass"] == 0:
            lines.append("⚠ El motor da entradas pero TODAS se bloquean por R:R o distancia del stop: "
                         "baja MIN_RR (p. ej. 1.0) o sube MAX_RISK_DIST_PCT.")
        elif span > 0:
            per_week = tot["pass"] / span * 7
            lines.append(f"✓ Ritmo esperable: ≈ {per_week:.1f} señales/semana en este universo. Es una estrategia de pocas "
                         f"entradas por diseño (una por estructura); si pasan más de {max(3, int(14 / max(per_week, 0.1)))} días "
                         f"sin ninguna, avísame.")
        return "\n".join(x for x in lines if x)

    # ── v5.1 · salida por estructura ──
    @staticmethod
    def mark_r(r, c):
        """R neta (con costes) si la operación `r` (virtual o real) se cerrara a mercado al precio c."""
        e = r.get("entry_real", r["entry"])
        risk = max(r.get("risk") or abs(e - r["sl"]), 1e-12)
        d = 1 if r["side"] == "LONG" else -1
        f = C.TP1_FRACTION
        r1 = (r["tp1"] - e) * d / risk
        rc = (c - e) * d / risk
        out = f * r1 + (1 - f) * rc if r.get("half") else rc
        return out - 2.0 * C.FEE_PCT / 100.0 * e / risk

    def on_break(self, sym, tf, d, c):
        """La estructura que sostiene una operación abierta se rompe (D→B, nivel duro o caducada).
        Siempre registra cuánto daría cerrar ahora (struct_r) para medirlo; con STRUCT_EXIT=cierra además cierra."""
        b = d.get("broke")
        if not b or C.STRUCT_EXIT == "off" or b["why"] not in C.STRUCT_EXIT_REASONS:
            return
        s = self.state["sims"].get(sym)
        if s and s.get("tf") == tf and s.get("kind") != FAIL_KIND and s["side"] == b["side"] \
                and d["time"] > s["bar_t"] and "struct_r" not in s:
            s["struct_r"], s["struct_why"] = round(self.mark_r(s, c), 3), b["why"]
            if C.STRUCT_EXIT == "cierra":
                self.finish_virtual(sym, tf, s, s["struct_r"], f"estructura ({b['why'].lower()})")
            else:
                self.tg.send(f"🧱 Virtual {sym}: estructura rota ({b['why']}). Cerrar ahora daría {s['struct_r']:+.2f}R · "
                             f"sigue abierta (STRUCT_EXIT=aviso)")
                self.save_state()
        rec = self.state["positions"].get(sym)
        if C.LIVE and rec and rec.get("tf") == tf and rec.get("kind") != FAIL_KIND and rec["side"] == b["side"] \
                and "struct_r" not in rec:
            rec["struct_r"], rec["struct_why"] = round(self.mark_r(rec, c), 3), b["why"]
            if C.STRUCT_EXIT == "cierra":
                self.struct_close_live(sym, rec, b)
            else:
                self.tg.send(f"🧱 {sym}: estructura rota ({b['why']}). Cerrar ahora daría {rec['struct_r']:+.2f}R · "
                             f"sigue abierta (STRUCT_EXIT=aviso)")
            self.save_state()

    def struct_close_live(self, sym, rec, b):
        long = rec["side"] == "LONG"
        try:
            pos = self.find_pos(sym, long)
            if pos is None:
                return
            self.ex.market_close(sym, long, abs(float(pos["positionAmt"])))
            rec["force_reason"] = f"estructura ({b['why'].lower()})"
            self.tg.send(f"🧱 {sym}: estructura rota ({b['why']}) → cierro a mercado ({rec['struct_r']:+.2f}R estimados)")
        except BingXError as e:
            self.tg.send(f"⚠ {sym}: no se pudo cerrar por estructura ({e})")

    # ── v5.1 · registro de eventos: funding, prima y open interest ──
    @staticmethod
    def event_names(d):
        s, out = d["sig"], []
        for bit, name in ((BIT_SC, "SC"), (BIT_BC, "BC"), (BIT_SPRING, "SPRING"), (BIT_UTAD, "UTAD"),
                          (BIT_TEST, "TEST"), (BIT_CTEST, "CTEST"), (BIT_SOS, "SOS"), (BIT_SOW, "SOW"),
                          (BIT_LPS, "LPS"), (BIT_LPSY, "LPSY"), (BIT_E, "FASE_E")):
            if has_bit(s, bit):
                out.append(name)
        if has_bit(s, BIT_AR) and has_bit(s, BIT_ST):
            out.append("FASE_B")
        if d["entry_now"]:
            out.append("ENTRADA")
        if d.get("broke"):
            out.append("ROTURA_" + d["broke"]["why"])
        return out

    def load_oi(self):
        try:
            with open(OI_PATH) as f:
                return {k: [tuple(x) for x in v] for k, v in json.load(f).items()}
        except (OSError, ValueError):
            return {}

    def save_oi(self):
        try:
            tmp = OI_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.oi_snap, f)
            os.replace(tmp, OI_PATH)
        except OSError as e:
            log.debug("oi_snap: %s", e)

    def snap_oi(self, sym):
        """Guarda el OI actual de BingX. Con la serie propia se calculan variaciones sin depender de Binance."""
        try:
            oi = self.ex.open_interest(sym)
        except Exception as e:  # es solo registro
            log.debug("oi %s: %s", sym, e)
            return None
        if oi is None:
            return None
        now = time.time()
        h = self.oi_snap.setdefault(sym, [])
        h.append((now, oi))
        while h and h[0][0] < now - 30 * 3600:
            h.pop(0)
        return oi

    def oi_local_changes(self, sym, oi_now):
        now, h, out = time.time(), self.oi_snap.get(sym, []), {}
        for hrs in (1, 6, 24):
            target, tol = now - hrs * 3600, min(0.25 * hrs * 3600, 7200)
            cand = [(abs(ts - target), v) for ts, v in h if abs(ts - target) <= tol and ts < now - 60]
            out[hrs] = round((oi_now / min(cand)[1] - 1) * 100, 3) if cand and min(cand)[1] > 0 else None
        return out

    def binance_oi(self, sym):
        """Variación del OI (valor) en 1h/6h/24h con el historial de Binance (solo últimos 30 días; EE. UU. = bloqueado)."""
        if not C.EVENT_OI_BINANCE or self.oi_binance_fails >= 5 or self.ex.contracts.get(sym, {}).get("cls") != "crypto":
            return None
        try:
            r = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
                             params={"symbol": sym.replace("-", ""), "period": "1h", "limit": 30}, timeout=6)
            if r.status_code != 200:
                if r.status_code in (403, 429, 451):
                    self.oi_binance_fails += 1
                    if self.oi_binance_fails == 5:
                        log.warning("Binance OI no disponible (HTTP %s): se usa solo la serie propia de BingX", r.status_code)
                return None
            vals = [float(x["sumOpenInterestValue"]) for x in r.json()]
        except (requests.RequestException, ValueError, KeyError, TypeError):
            return None
        self.oi_binance_fails = 0
        if len(vals) < 2:
            return None
        now = vals[-1]
        return {k: (round((now / vals[-1 - k] - 1) * 100, 3) if len(vals) > k and vals[-1 - k] > 0 else None)
                for k in (1, 6, 24)}

    def event_features(self, sym):
        out = {"funding_pct": None, "premium_pct": None, "oi_now": None, "oi_chg_1h": None, "oi_chg_6h": None,
               "oi_chg_24h": None, "oi_src": ""}
        try:
            fr, pr = self.ex.premium_info(sym)
            out["funding_pct"] = None if fr is None else round(fr, 4)
            out["premium_pct"] = None if pr is None else round(pr, 4)
        except Exception as e:
            log.debug("premium %s: %s", sym, e)
        oi = self.snap_oi(sym)
        out["oi_now"] = oi
        loc = self.oi_local_changes(sym, oi) if oi is not None else {}
        bnb = self.binance_oi(sym)
        if bnb:
            out.update(oi_chg_1h=bnb[1], oi_chg_6h=bnb[6], oi_chg_24h=bnb[24], oi_src="binance")
        elif any(v is not None for v in loc.values()):
            out.update(oi_chg_1h=loc.get(1), oi_chg_6h=loc.get(6), oi_chg_24h=loc.get(24), oi_src="bingx")
        return out

    def log_events(self, batch):
        feats = list(self.pool.map(lambda b: self.event_features(b[0]), batch))
        for (sym, tf, d, names), f in zip(batch, feats):
            cls = self.ex.contracts.get(sym, {}).get("cls_label", "?")
            for ev in names:
                direction = "LONG" if ev == "SC" else "SHORT" if ev == "BC" else \
                    ("LONG" if d["wdir"] == DIR_ACCUM else "SHORT" if d["wdir"] else "-")
                if ev.startswith("ROTURA"):
                    direction = d["broke"]["side"]
                close_ms = int(d["time"]) + tf_ms(tf)
                self.events.write({"ts_ms": close_ms, "time_utc": iso(close_ms / 1000), "symbol": sym, "tf": tf,
                                   "cls": cls, "event": ev, "dir": direction, "price": d["close"], "atr": d["atr"],
                                   "phase": PHASE_NAMES[d["phase"]], "conf": d["conf"], "val": d["val"],
                                   "rh": d["rh"], "rl": d["rl"], **f})

    def fp(self, x, sym):
        pp = self.ex.contracts.get(sym, {}).get("pp", 6)
        return f"{x:.{pp}f}"

    # ── estado persistente ──
    def load_state(self):
        base = {"positions": {}, "sims": {}, "daily": {"day": utc_day(), "r": 0.0, "n": 0},
                "stats": {"n": 0, "wins": 0, "sum_r": 0.0, "gw": 0.0, "gl": 0.0},
                "last_trade": time.time(), "idle_warned": False, "paused": False}
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
        self.save_state()
        if dl["r"] <= -C.MAX_DAILY_LOSS_R:
            self.tg.send(f"🛑 Pérdida diaria {dl['r']:+.2f}R ≥ {C.MAX_DAILY_LOSS_R}R: sin nuevas aperturas hasta 00:00 UTC.")

    # ── universo y calentamiento ──
    def refresh_universe(self, force=False):
        if not force and time.time() - self.universe_ts < C.UNIVERSE_REFRESH_H * 3600:
            return
        self.ex.load_contracts()
        norm = lambda s: s if "-" in s else s.replace("USDT", "-USDT")
        if C.SYMBOLS:
            syms = [norm(s) for s in C.SYMBOLS]
        else:
            rows, skipped = [], {"categoría": 0, "volumen": 0, "API cerrada": 0}
            vols = {t.get("symbol", ""): float(t.get("quoteVolume", 0) or 0) for t in self.ex.tickers()}
            for s, c in self.ex.contracts.items():
                if c["cls"] not in C.CATEGORIES:
                    skipped["categoría"] += 1
                    continue
                if not c["api_open"]:
                    skipped["API cerrada"] += 1
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
        if not self.engines and C.ENGINE_CACHE:
            n = self.load_engines()
            if n:
                self.catch_up_many()
                log.info("caché: %d motores cargados y al día", len(self.engines))
        jobs = [(s, tf) for s in syms for tf in ALL_TFS if (s, tf) not in self.engines]
        self.warmup_many(jobs)
        for key in list(self.engines):
            if key[0] not in syms:
                del self.engines[key]
        self.symbols = [s for s in syms if all((s, tf) in self.engines for tf in C.TIMEFRAMES)]
        self.universe_ts = time.time()
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
            try:
                return job, self.ex.klines_history(s, tf, n + 1, tf_ms(tf))
            except BingXError as e:
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
                    d = eng.update(t, o, h, l, c, v)
                    if tf in C.TIMEFRAMES:
                        self.tally(sym, eng, d)
                last = t
        eng.last_t = last
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
        syms = sorted((s for s in self.symbols if (s, tf) in self.engines),
                      key=lambda s: 0 if (self.engines[(s, tf)].last or {}).get("phase", 0) in (PHASE_C, PHASE_D) else 1)

        def fetch(sym):
            try:
                return sym, self.ex.klines(sym, tf, 6)
            except BingXError as e:
                log.warning("klines %s %s: %s", sym, tf, e)
                return sym, None

        trading = tf in C.TIMEFRAMES
        watch, rebuild, ev_batch = [], [], []
        for sym, rows in self.pool.map(fetch, syms):
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
                if dead_bar(o, h, l, c, v):
                    continue  # mercado cerrado (TradFi): no alimenta al motor
                d = eng.update(t, o, h, l, c, v)
                if trading:
                    self.tally(sym, eng, d)
                    self.step_sim(sym, tf, t, h, l, c, d["atr"])
                    self.manage_bar(sym, tf, c, d["atr"])
                    self.on_break(sym, tf, d, c)
                    if C.EVENT_LOG and t + 2 * ms > cut:  # solo eventos en tiempo real: funding/OI "de ahora" no valen para el pasado
                        names = self.event_names(d)
                        if names:
                            ev_batch.append((sym, tf, d, names))
            if d is None or not trading:
                continue
            note = self.event_note(d)
            if note:
                watch.append(f"{sym} {tf} {note}")
            if d["entry_now"] and d["time"] == fresh[-1][0]:
                self.handle_entry(sym, tf, d)
            elif d.get("fail") and d["time"] == fresh[-1][0] and C.FAIL_TRADES != "off":
                self.handle_entry(sym, tf, d, fail=True)
        if trading and C.OI_SNAPSHOTS:
            logged = {b[0] for b in ev_batch}
            snap = [s for s in syms if s not in logged and (self.engines[(s, tf)].last or {}).get("phase", 0) >= 1]
            if snap:
                list(self.pool.map(self.snap_oi, snap[:150]))
        if ev_batch:
            self.log_events(ev_batch)
        if trading and C.OI_SNAPSHOTS:
            self.save_oi()
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
        self.finish_virtual(sym, tf, s, r, sim.reason)

    def finish_virtual(self, sym, tf, s, r, reason):
        s["exit_reason"] = reason
        self.state["sims"].pop(sym, None)
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
                            "meta_p": s.get("meta_p"),
                            "struct_r": s.get("struct_r"), "struct_why": s.get("struct_why"),
                            "exit_reason": reason, "mode": "SIGNAL"})
        self.tg.send(f"{'✅' if r > 0 else '❌'} Virtual {s['side']} {sym} cerrada por {reason}: {r:+.2f}R "
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
        qty = equity * C.RISK_PCT / 100.0 / abs(price - sig["sl"])
        qty = ex.fmt_qty(sym, min(qty, avail * C.LEVERAGE * 0.9 / price))
        c = ex.contracts[sym]
        if qty <= 0 or qty < c["min_qty"] or qty * price < max(c["min_usdt"], 2.0):
            self.tg.send(txt + f"\n⏸ Tamaño insuficiente ({qty}) para equity {equity:.2f}")
            return
        if C.MAX_SLIP_R > 0:
            ok_book, why_book = self.book_guard(sym, long, qty, risk)
            if not ok_book:
                self.tg.send(txt + f"\n⏸ {why_book}")
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
        rec = {**sig, "qty": amt, "entry_real": entry, "risk": abs(entry - sig["sl"]), "be": False,
               "open_ts": time.time(), "cid": cid, "sl_id": "", "tp1_id": "", "tp2_id": ""}
        if not self.ensure_sl(sym, rec, amt, at_open=True):
            return
        q1 = ex.fmt_qty(sym, amt * C.TP1_FRACTION)
        q2 = ex.fmt_qty(sym, amt - q1)
        try:
            if q1 > 0 and q2 > 0 and q1 >= c["min_qty"] and q2 >= c["min_qty"]:
                rec["tp1_id"] = ex.exit_order(sym, long, "TAKE_PROFIT_MARKET", q1, sig["tp1"])
                rec["tp2_id"] = ex.exit_order(sym, long, "TAKE_PROFIT_MARKET", q2, sig["tp2"])
            else:
                rec["tp2_id"] = ex.exit_order(sym, long, "TAKE_PROFIT_MARKET", amt, sig["tp2"])
        except BingXError as e:
            self.tg.send(f"⚠ {sym}: TP no colocado ({e}). El SL sí está puesto.")
        self.state["positions"][sym] = rec
        self.save_state()
        slip = (entry - sig["entry"]) / sig["entry"] * 100 * d
        self.tg.send(txt + f"\n✅ <b>LIVE</b> abierta {amt} @ {self.fp(entry, sym)} (desliz. {slip:+.3f}%)"
                     + (" · SL en la orden" if self.attach_ok else ""))


    def book_guard(self, sym, long, qty, risk):
        """Estima el coste de llenar `qty` a mercado recorriendo el libro (spread + impacto) en R."""
        try:
            bids, asks = self.ex.depth(sym, C.DEPTH_LEVELS)
        except BingXError as e:
            log.warning("libro %s: %s", sym, e)
            return True, ""  # sin libro no se bloquea: es una protección, no un requisito
        if not bids or not asks:
            return True, ""
        mid = (bids[0][0] + asks[0][0]) / 2.0
        avg = walk_book(asks if long else bids, qty)
        if avg is None:
            return False, f"libro con poca profundidad para {qty} (primeros {C.DEPTH_LEVELS} niveles)"
        cost_r = (avg - mid) * (1 if long else -1) / max(risk, 1e-12)
        if cost_r > C.MAX_SLIP_R:
            return False, f"impacto estimado {cost_r:.2f}R > {C.MAX_SLIP_R}R (spread + profundidad del libro)"
        return True, ""

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
                if self.replace_stop(sym, rec, amt, new):
                    rec["trail_stop"] = new
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


    def replace_stop(self, sym, rec, amt, level):
        """Coloca el stop NUEVO antes de cancelar el viejo: nunca hay una ventana sin stop.
        Si el nuevo no se puede colocar (p. ej. ya está del lado equivocado del precio), el viejo se queda."""
        long = rec["side"] == "LONG"
        old = [str(o.get("orderId")) for o in self._stops(sym, long) if o.get("orderId")]
        try:
            new_id = str(self.ex.exit_order(sym, long, "STOP_MARKET", amt, level))
        except BingXError as e:
            log.warning("replace_stop %s @%s: %s", sym, level, e)
            return False
        for oid in old:
            if oid != new_id:
                self.ex.cancel(sym, oid)
        rec["sl_id"] = new_id
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
                self.close_record(sym, rec)
                continue
            amt = abs(float(pos["positionAmt"]))
            if not rec.get("half") and amt <= rec["qty"] * (1 - C.TP1_FRACTION) * 1.02:
                rec["half"] = True
                rec["be"] = C.MOVE_SL_TO_BE
                self.tg.send(f"🎯 {sym} TP1 tocado" + (f" · SL a breakeven {self.fp(rec['entry_real'], sym)}"
                                                        if C.MOVE_SL_TO_BE else ""))
                self.save_state()
                # el SL original cubre la cantidad completa: se sustituye por uno de la cantidad restante,
                # colocando el nuevo ANTES de cancelar el viejo
                level = rec["entry_real"] if rec["be"] else rec["sl"]
                if not self.replace_stop(sym, rec, amt, level) and rec["be"]:
                    rec["be"] = False  # el BE ya no es válido (precio al otro lado): mismo SL, cantidad restante
                    if not self.replace_stop(sym, rec, amt, rec["sl"]):
                        self.tg.send(f"⚠ {sym}: no pude ajustar el stop tras TP1; el SL original sigue puesto.")
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
            exit_px, reason = px_tp2, "TP2"
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
                            "meta_p": rec.get("meta_p"), "struct_r": rec.get("struct_r"),
                            "struct_why": rec.get("struct_why"), "mode": "LIVE"})
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
            if self.find_pos(sym, rec["side"] == "LONG") is None:
                self.close_record(sym, rec)
        # órdenes huérfanas de despliegues anteriores (marcadas con prefijo wyk)
        try:
            n = 0
            for o in self.ex.open_orders():
                cid = str(o.get("clientOrderId", o.get("clientOrderID", "")))
                if cid.startswith("wyk") and o.get("symbol") not in self.state["positions"]:
                    n += self.ex.cancel(o.get("symbol"), o.get("orderId"))
            if n:
                self.tg.send(f"🧹 Canceladas {n} órdenes huérfanas de un despliegue anterior.")
        except BingXError as e:
            log.warning("limpieza de huérfanas: %s", e)
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
            elif c in ("/diagnostico", "/diag"):
                self.tg.send(self.diagnostic_text())
            elif c == "/stats":
                self.tg.send(f"📈 {self.stats_text()}\nHoy {self.state['daily']['r']:+.2f}R ({self.state['daily']['n']} ops)")
            elif c == "/pausa":
                self.state["paused"] = True
                self.save_state()
                self.tg.send("⏸ Pausado: no se abren operaciones nuevas. Las abiertas se siguen gestionando.")
            elif c == "/reanudar":
                self.state["paused"] = False
                self.save_state()
                self.tg.send("▶ Reanudado.")
            else:
                self.tg.send("Comandos: /estado /posiciones /stats /diagnostico /pausa /reanudar")

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
        t0 = time.time()
        self.refresh_universe(force=True)
        self.reconcile()
        self.tg.send(f"✅ {len(self.engines)} estructuras reconstruidas ({len(self.symbols)} símbolos × "
                     f"{'/'.join(ALL_TFS)}) en {time.time() - t0:.0f}s.")
        self.tg.send(self.diagnostic_text())
        self.status(force=True)  # sin esperar 12 h al primer informe
        next_close = {tf: (now_ms() // tf_ms(tf) + 1) * tf_ms(tf) for tf in ALL_TFS}
        last_manage = last_cmd = 0
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
                    if time.time() - self.last_cache > C.ENGINE_CACHE_EVERY_MIN * 60:
                        self.save_engines()
                if time.time() - last_manage >= C.MANAGE_EVERY_S:
                    self.manage()
                    last_manage = time.time()
                if time.time() - last_cmd >= 3:
                    self.commands()
                    last_cmd = time.time()
            except Exception as e:  # un error puntual no debe tumbar el bot
                log.error("ciclo: %s\n%s", e, traceback.format_exc())
                self.tg.send(f"⚠ Error en el ciclo: {e}")
                time.sleep(10)
            time.sleep(1)
        self.save_engines()  # parada limpia (SIGTERM de Railway): el siguiente arranque es inmediato


def main():
    log.info("%s | %s", C.CODE_VERSION, C.summary())
    bot = Bot()

    def stop(*_):
        bot.running = False
        log.info("parada solicitada")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    bot.run()


if __name__ == "__main__":
    main()
