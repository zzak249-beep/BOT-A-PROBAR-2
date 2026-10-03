"""Configuración desde variables de entorno. Todos los parsers quitan comillas (lección de Railway)."""
import os

CODE_VERSION = "wyckoff-bot 5.0.0 (2026-10-02)"


def _raw(name, default):
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip().strip('"').strip("'").strip()
    return default if v == "" else v


def _s(name, default):
    return str(_raw(name, default))


def _f(name, default):
    try:
        return float(_raw(name, default))
    except (TypeError, ValueError):
        return float(default)


def _i(name, default):
    try:
        return int(float(_raw(name, default)))
    except (TypeError, ValueError):
        return int(default)


def _b(name, default):
    return str(_raw(name, default)).lower() in ("1", "true", "si", "sí", "yes", "on")


def _list(name, default=""):
    v = _s(name, default)
    return [x.strip().upper() for x in v.split(",") if x.strip()]


# ── Modo ──
MODE = _s("MODE", "SIGNAL").upper()              # SIGNAL = solo avisa · LIVE = opera
CONFIRM_LIVE = _s("CONFIRM_LIVE", "NO").upper()  # segundo cerrojo: LIVE solo si CONFIRM_LIVE=SI
LIVE = MODE == "LIVE" and CONFIRM_LIVE in ("SI", "SÍ", "YES")
VST = _b("BINGX_VST", False)                     # true = cuenta demo VST

# ── BingX ──
API_KEY = _s("BINGX_API_KEY", "")
API_SECRET = _s("BINGX_API_SECRET", "")

# ── Telegram ──
TG_TOKEN = _s("TELEGRAM_TOKEN", "") or _s("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = _s("TELEGRAM_CHAT_ID", "")

# ── Universo ──
TIMEFRAMES = [x.strip().lower() for x in _s("TIMEFRAMES", _s("TIMEFRAME", "15m")).split(",") if x.strip()]
TIMEFRAME = TIMEFRAMES[0]                        # compatibilidad
SYMBOLS = _list("SYMBOLS", "")                   # vacío = automático
UNIVERSE = _s("UNIVERSE", "all").lower()         # all = todos los perpetuos · top = los AUTO_TOP_N con más volumen
AUTO_TOP_N = _i("AUTO_TOP_N", 30)
MAX_SYMBOLS = _i("MAX_SYMBOLS", 1000)            # tope de seguridad de memoria/peticiones
CATEGORIES = [x.strip().lower() for x in _s("CATEGORIES", "crypto,forex,commodity,stock,index,tradfi").split(",")]
MIN_QUOTE_VOL = _f("MIN_QUOTE_VOL", 2_000_000)   # USDT 24h mínimo para cripto
MIN_QUOTE_VOL_TRADFI = _f("MIN_QUOTE_VOL_TRADFI", 200_000)  # USDT 24h mínimo para TradFi (volumen solo de BingX)
TRADFI_EFFORT = _s("TRADFI_EFFORT", "volumen").lower()  # volumen | rango (esfuerzo = rango de la vela)
TRADFI_NO_ENTRY_FRI_UTC = _i("TRADFI_NO_ENTRY_FRI_UTC", 17)  # viernes desde esta hora UTC no abre TradFi (-1 = off)
ENGINE_KEEP_BARS = _i("ENGINE_KEEP_BARS", 400)   # velas que guarda cada motor (memoria con cientos de símbolos)
BLACKLIST = _list("BLACKLIST", "")
WARMUP_BARS = _i("WARMUP_BARS", 1000)            # velas de histórico para reconstruir la estructura
FETCH_WORKERS = _i("FETCH_WORKERS", 8)           # descargas en paralelo
UNIVERSE_REFRESH_H = _f("UNIVERSE_REFRESH_H", 6)

# ── Motor Wyckoff ──
ENTRY_STRICTNESS = _s("ENTRY_STRICTNESS", "Estándar")  # Conservador | Estándar | Agresivo
if ENTRY_STRICTNESS.lower().startswith("con"):
    ENTRY_STRICTNESS = "Conservador"
elif ENTRY_STRICTNESS.lower().startswith("agr"):
    ENTRY_STRICTNESS = "Agresivo"
else:
    ENTRY_STRICTNESS = "Estándar"

# ── Plan y filtros (los del indicador v2) ──
SL_BUFFER_ATR = _f("SL_BUFFER_ATR", 0.25)
MIN_RR = _f("MIN_RR", 1.5)                       # R:R mínimo hasta TP2 (0 = sin filtro)
TREND_FILTER = _s("TREND_FILTER", "aviso").lower()  # off | aviso | bloquea
TREND_TF = _s("TREND_TF", "1h")
TREND_EMA = _i("TREND_EMA", 50)
CONTEXT_TF = _s("CONTEXT_TF", "4h").lower()      # estructura Wyckoff de TF superior (vacío = sin contexto)
CONTEXT_FILTER = _s("CONTEXT_FILTER", "aviso").lower()  # off | aviso | bloquea (bloquea solo "en contra")
CONTEXT_WARMUP = _i("CONTEXT_WARMUP", 700)
CHASE_MAX_R = _f("CHASE_MAX_R", 0.5)             # no perseguir si el precio ya avanzó X R
MIN_RISK_DIST_PCT = _f("MIN_RISK_DIST_PCT", 0.30)  # stop demasiado cerca: el coste se come la R
MAX_RISK_DIST_PCT = _f("MAX_RISK_DIST_PCT", 8.0)
TP1_FRACTION = _f("TP1_FRACTION", 0.5)
TP2_MULT = _f("TP2_MULT", 1.0)                  # TP2 = altura del rango × esto (1.0 = indicador)
TRAIL_ATR = _f("TRAIL_ATR", 0.0)                # tras TP1, stop a cierre − X×ATR (0 = off, indicador)
TIME_STOP_BARS = _i("TIME_STOP_BARS", 0)        # cierra si en N velas no toca TP1 (0 = off, indicador)
BTC_FILTER = _s("BTC_FILTER", "aviso").lower()  # off | aviso | bloquea: estructura de BTC en CONTEXT_TF (solo cripto)
MAX_SAME_SIDE = _i("MAX_SAME_SIDE", 0)          # máx. posiciones en la misma dirección (0 = sin tope)
# ── v4: ideas nuevas ──
FAIL_TRADES = _s("FAIL_TRADES", "aviso").lower()  # off | aviso | on: operar a los atrapados cuando la estructura se rompe
FAIL_SL_ATR = _f("FAIL_SL_ATR", 1.0)              # stop de la trampa: nivel duro ± X×ATR
FAIL_NEEDS_ENTRY = _b("FAIL_NEEDS_ENTRY", False)  # true = solo estructuras que llegaron a dar entrada
FLOW_SOURCE = _s("FLOW_SOURCE", "binance").lower() # binance | off: compra/venta agresora (no hay en BingX)
BREADTH_FILTER = _s("BREADTH_FILTER", "aviso").lower()  # off | aviso | bloquea: amplitud Wyckoff en contra
META_MODEL = _s("META_MODEL", "meta_model.json")  # modelo entrenado con meta.py --guardar
META_FILTER = _s("META_FILTER", "aviso").lower()  # off | aviso | bloquea (bloquea por debajo del umbral)
# ── v5 ──
MAX_SLIP_R = _f("MAX_SLIP_R", 0.10)             # no entra si spread + profundidad del libro cuestan más de X R (0 = off)
DEPTH_LEVELS = _i("DEPTH_LEVELS", 20)
ENGINE_CACHE = _b("ENGINE_CACHE", True)         # guarda los motores en /data: reinicio en segundos, no en minutos
ENGINE_CACHE_EVERY_MIN = _i("ENGINE_CACHE_EVERY_MIN", 30)
RISK_TARGET_DD = _f("RISK_TARGET_DD", 20.0)     # % de caída máxima tolerada (95% de los casos) para recomendar RISK_PCT
ATTACH_SL = _b("ATTACH_SL", True)               # SL dentro de la orden de entrada (sin ventana desnuda)
MOVE_SL_TO_BE = _b("MOVE_SL_TO_BE", True)

# ── Riesgo ──
RISK_PCT = _f("RISK_PCT", 0.5)                   # % del equity arriesgado por operación
LEVERAGE = _i("LEVERAGE", 5)
MARGIN_MODE = _s("MARGIN_MODE", "ISOLATED").upper()
MAX_CONCURRENT = _i("MAX_CONCURRENT", 2)         # posiciones de ESTE bot
MAX_TOTAL_POSITIONS = _i("MAX_TOTAL_POSITIONS", 4)  # posiciones de TODA la cuenta (otros bots y manuales)
MAX_DAILY_LOSS_R = _f("MAX_DAILY_LOSS_R", 3.0)
FEE_PCT = _f("FEE_PCT", 0.05)                    # comisión por lado (%) para R neta
SLIPPAGE_PCT = _f("SLIPPAGE_PCT", 0.03)          # deslizamiento por lado (%) que descuenta el backtest

# ── Operativa ──
CANDLE_DELAY_S = _i("CANDLE_DELAY_S", 8)         # espera tras el cierre de vela
MANAGE_EVERY_S = _i("MANAGE_EVERY_S", 30)
SIGNAL_COOLDOWN_MIN = _i("SIGNAL_COOLDOWN_MIN", 60)
STATUS_EVERY_H = _f("STATUS_EVERY_H", 12)
ZOMBIE_ALERT_HOURS = _f("ZOMBIE_ALERT_HOURS", 72)
IDLE_ALERT_DAYS = _f("IDLE_ALERT_DAYS", 7)
DATA_DIR = _s("DATA_DIR", "/data" if os.path.isdir("/data") else "./data")
LOG_LEVEL = _s("LOG_LEVEL", "INFO")
TG_COMMANDS = _b("TG_COMMANDS", True)            # /estado /posiciones /stats /pausa /reanudar

TF_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200,
              "4h": 14400, "6h": 21600, "12h": 43200, "1d": 86400}


def tf_seconds(tf):
    return TF_SECONDS.get(tf, 900)


def summary():
    return (f"modo {'LIVE' if LIVE else 'SIGNAL'}{' (VST)' if VST else ''} · TF {','.join(TIMEFRAMES)} · contexto {CONTEXT_TF or '-'} ({CONTEXT_FILTER})"
            f" · exigencia {ENTRY_STRICTNESS}"
            f" · riesgo {RISK_PCT}% · x{LEVERAGE} {MARGIN_MODE} · máx {MAX_CONCURRENT} (cuenta {MAX_TOTAL_POSITIONS})"
            f" · R:R≥{MIN_RR} · tendencia {TREND_FILTER} {TREND_TF}")
