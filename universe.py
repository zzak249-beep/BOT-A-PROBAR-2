"""
Universo: TODOS los perpetuos USDT de BingX, cripto y TradFi (forex, materias primas, acciones, índices).

BingX lista los TradFi en el mismo mercado de perpetuos USDT-M, con nombres tipo NCFXEUR2USD-USDT
(NC = non-crypto, FX = forex). La clasificación sale del propio símbolo; lo que no encaja en un
patrón conocido se marca "tradfi" genérico en vez de darlo por cripto en silencio.
"""
import re

TRADFI_CODES = {
    "FX": "forex", "CO": "materia prima", "CM": "materia prima", "SK": "acción", "ST": "acción",
    "SI": "índice", "IX": "índice", "ID": "índice",
}
# Nombres TradFi sin prefijo NC que BingX también usa (materias primas, índices, acciones tokenizadas)
PLAIN_TRADFI = {
    "GOLD": "materia prima", "SILVER": "materia prima", "XAU": "materia prima", "XAG": "materia prima",
    "OIL": "materia prima", "WTI": "materia prima", "BRENT": "materia prima", "NATGAS": "materia prima",
    "COPPER": "materia prima", "PLATINUM": "materia prima",
    "SP500": "índice", "SPX": "índice", "NASDAQ100": "índice", "NDX": "índice", "US30": "índice", "DJI": "índice",
    "DAX": "índice", "NIKKEI": "índice", "HSI": "índice",
}
CLASS_KEYS = {"cripto": "crypto", "forex": "forex", "materia prima": "commodity", "acción": "stock",
              "índice": "index", "tradfi": "tradfi"}


def classify(symbol):
    """Devuelve (clase_legible, clave) para un símbolo tipo XXX-USDT."""
    base = symbol.split("-")[0].upper()
    m = re.match(r"^NC([A-Z]{2})", base)
    if m:
        label = TRADFI_CODES.get(m.group(1), "tradfi")
        return label, CLASS_KEYS[label]
    if base in PLAIN_TRADFI:
        label = PLAIN_TRADFI[base]
        return label, CLASS_KEYS[label]
    return "cripto", "crypto"


def is_tradfi(symbol):
    return classify(symbol)[1] != "crypto"


def pretty(symbol):
    """NCFXEUR2USD-USDT → EUR/USD (forex); BTC-USDT → BTC."""
    base = symbol.split("-")[0]
    m = re.match(r"^NC[A-Z]{2}(.+?)2(.+)$", base)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    m = re.match(r"^NC[A-Z]{2}(.+)$", base)
    return m.group(1) if m else base
