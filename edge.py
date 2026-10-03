"""
Laboratorio de ideas nuevas (v5.1). Todo se calcula con velas CERRADAS hasta la vela de entrada, igual en
backtest y en el bot, y NADA bloquea operaciones a menos que lo actives y lo hayas medido con `sweep.py --modo edge`.

Ideas (poco usadas en bots minoristas; ninguna se da por buena sin pasar la prueba 70/30 con Bonferroni):

  squeeze   ATR(5)/ATR(50). Wyckoff: la Fase B acumula "energía"; un rango que se comprime antes del Spring/SOS
            debería soltar más que uno que sigue agitado. <0.7 = compresión fuerte.
  er        Eficiencia de Kaufman (20 velas): |Δprecio| / camino recorrido. ~0 = serrucho puro, ~1 = tendencia limpia.
            Mide si la entrada llega tras un tramo direccional agotado o en un rango real.
  dry       Volumen medio de las últimas 5 velas / media de 50. <1 = "no hay oferta/demanda" (secado, test limpio).
  clv       Ubicación del cierre de la vela de entrada en su rango, con signo del lado (+1 = cierra en el extremo
            a favor, -1 = en contra). Evita comprar una vela que ya se ha ido o que cierra débil.
  body      Cuerpo de la vela de entrada en ATR, con signo del lado (vela de persecución vs. de confirmación).
  wick_exc  Mecha de rechazo de la vela del Spring/UTAD (fracción del rango): un Spring "de libro" deja cola larga.
  vol_exc   Volumen de la vela del Spring/UTAD / media 50: clímax de absorción vs. rotura sin esfuerzo.
  risk_atr  Distancia al stop en ATR (stops absurdamente cortos o largos).
  vr        (v5.2) Razón de varianzas de Lo-MacKinlay, 120 retornos, q=4. <1 = reversión a la media (rango real),
            >1 = tendencia. Misma familia que tu z* de Wavelet Confirm, aquí como etiqueta de régimen de la entrada.
  atr_pct   (v5.2) Percentil (0-100) del ATR actual frente a las últimas 250 velas: entrar en volatilidad baja o alta.
  depth     (v5.2) Profundidad del Spring/UTAD más allá del borde del rango, en ATR (sacudida corta vs. rotura profunda).
  confirm   (v5.2) Velas entre el Spring/UTAD y la entrada (rapidez de la recuperación).
  avwap     (v5.2) (VWAP anclado al clímax − entrada)/ATR con signo del lado: + = entras con descuento frente al
            coste medio de toda la campaña, − = por encima. Coste base de la "mano fuerte" de Wyckoff.
  fund_h    (v5.2) Horas hasta el próximo cobro de funding (00/08/16 UTC): barridos de stops alrededor del funding.
  session   Sesión UTC de la entrada (Asia / Europa / EEUU / Noche) y día de la semana: la liquidez cambia
            por franja y el motor Wyckoff no la conoce.

Gestión (no es señal, es riesgo): `throttle_mult` recorta el riesgo tras una racha en rojo (acelerador de curva de
capital). Reduce el tamaño del drawdown sin tocar la entrada; el backtest muestra cuánto.
"""
import math
import random
import re
from datetime import datetime, timezone

EDGE_KEYS = ("squeeze", "er", "dry", "clv", "body", "wick_exc", "vol_exc", "risk_atr", "session", "dow",
             "vr", "atr_pct", "depth", "confirm", "avwap", "fund_h")
NUM_KEYS = ("squeeze", "er", "dry", "clv", "body", "wick_exc", "vol_exc", "risk_atr", "vr", "atr_pct", "depth", "confirm",
            "avwap", "fund_h")


def _mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def _tr(rows, k):
    h, l, pc = rows[k][2], rows[k][3], rows[k - 1][4]
    return max(h - l, abs(h - pc), abs(l - pc))


def session_of(ts_ms):
    h = datetime.fromtimestamp(ts_ms / 1000, timezone.utc).hour
    return "Asia" if h < 8 else "Europa" if h < 13 else "EEUU" if h < 21 else "Noche"


def edge_features(rows, side, atr, entry=None, sl=None, exc_ts=None, close_ts=None, rh=None, rl=None, exc_p=None,
                  clx_ts=None):
    """rows: velas [t,o,h,l,c,v,...] CERRADAS, la última es la vela de entrada. Devuelve dict con EDGE_KEYS
    (None si no hay datos suficientes). `close_ts` = instante de cierre de la vela de entrada (ms)."""
    out = {k: None for k in EDGE_KEYS}
    n = len(rows)
    d = 1 if side == "LONG" else -1
    if n < 12:
        return out
    t, o, h, l, c, v = rows[-1][:6]
    rng = h - l
    if rng > 0:
        out["clv"] = round(d * (2 * c - h - l) / rng, 3)
    if atr and atr == atr and atr > 0:
        out["body"] = round(d * (c - o) / atr, 3)
        if entry is not None and sl is not None:
            out["risk_atr"] = round(abs(entry - sl) / atr, 2)
    if n >= 51:
        a5 = _mean([_tr(rows, k) for k in range(n - 5, n)])
        a50 = _mean([_tr(rows, k) for k in range(n - 50, n)])
        if a50 > 0:
            out["squeeze"] = round(a5 / a50, 3)
        v50 = _mean([r[5] for r in rows[-50:]])
        if v50 > 0:
            out["dry"] = round(_mean([r[5] for r in rows[-5:]]) / v50, 3)
    if n >= 21:
        path = sum(abs(rows[k][4] - rows[k - 1][4]) for k in range(n - 20, n))
        if path > 0:
            out["er"] = round(abs(c - rows[n - 21][4]) / path, 3)
    if exc_ts is not None and exc_ts == exc_ts:
        for k in range(n - 1, -1, -1):
            if rows[k][0] == exc_ts:
                _, eo, eh, el, ec, ev = rows[k][:6]
                er_ = eh - el
                if er_ > 0:
                    wick = (min(eo, ec) - el) if side == "LONG" else (eh - max(eo, ec))
                    out["wick_exc"] = round(max(wick, 0) / er_, 3)
                base = rows[max(0, k - 50):k]
                vb = _mean([r[5] for r in base])
                if vb and vb == vb and vb > 0:
                    out["vol_exc"] = round(ev / vb, 2)
                break
    # ── v5.2 ──
    if n >= 122:
        cl = [rows[k][4] for k in range(n - 121, n)]
        rets = [math.log(cl[k] / cl[k - 1]) for k in range(1, len(cl)) if cl[k] > 0 and cl[k - 1] > 0]
        q, m_ = 4, len(rets)
        if m_ >= 100:
            mu = _mean(rets)
            v1 = sum((x - mu) ** 2 for x in rets) / (m_ - 1)
            sq = [sum(rets[k - q + 1:k + 1]) for k in range(q - 1, m_)]
            mm = q * (m_ - q + 1) * (1 - q / m_)
            vq = sum((x - q * mu) ** 2 for x in sq) / mm
            if v1 > 0:
                out["vr"] = round(vq / v1, 3)
    if n >= 270:
        tr = [_tr(rows, k) for k in range(n - 264, n)]
        roll = [_mean(tr[k - 13:k + 1]) for k in range(13, len(tr))]
        cur, hist = roll[-1], roll[-251:-1] if len(roll) > 251 else roll[:-1]
        if hist:
            out["atr_pct"] = round(100.0 * sum(1 for x in hist if x <= cur) / len(hist), 1)
    if exc_p is not None and exc_p == exc_p and atr and atr == atr and atr > 0:
        if side == "LONG" and rl is not None and rl == rl:
            out["depth"] = round(max(rl - exc_p, 0) / atr, 2)
        elif side == "SHORT" and rh is not None and rh == rh:
            out["depth"] = round(max(exc_p - rh, 0) / atr, 2)
    if exc_ts is not None and exc_ts == exc_ts:
        for k in range(n - 1, -1, -1):
            if rows[k][0] == exc_ts:
                out["confirm"] = n - 1 - k
                break
    if clx_ts is not None and clx_ts == clx_ts and atr and atr == atr and atr > 0:
        for k in range(n - 1, -1, -1):
            if rows[k][0] == clx_ts:
                seg = rows[k:]
                vv = sum(r[5] for r in seg)
                if vv > 0:
                    av = sum((r[2] + r[3] + r[4]) / 3.0 * r[5] for r in seg) / vv
                    px = entry if entry is not None else c
                    out["avwap"] = round(d * (av - px) / atr, 2)
                break
    ts = close_ts if close_ts is not None else t
    hh = datetime.fromtimestamp(ts / 1000, timezone.utc)
    out["fund_h"] = round(((hh.hour // 8) + 1) * 8 - (hh.hour + hh.minute / 60.0), 1)
    out["session"] = session_of(ts)
    out["dow"] = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%a")
    return out


# ── reglas de bloqueo configurables: EDGE_RULES="squeeze<0.8,er>0.25,session!=Noche" ──
_RULE = re.compile(r"^\s*([a-z_]+)\s*(<=|>=|!=|==|<|>)\s*([A-Za-z0-9_.+-]+)\s*$")


def parse_rules(text):
    rules = []
    for part in (text or "").split(","):
        if not part.strip():
            continue
        m = _RULE.match(part)
        if not m or m.group(1) not in EDGE_KEYS:
            raise ValueError(f"regla inválida: '{part}' (claves: {', '.join(EDGE_KEYS)})")
        rules.append((m.group(1), m.group(2), m.group(3)))
    return rules


def passes(sig, rule):
    k, op, val = rule
    x = sig.get(k)
    if x is None:
        return True  # sin dato no se bloquea (no inventar)
    if k in NUM_KEYS:
        try:
            y = float(val)
        except ValueError:
            return True
        return {"<": x < y, ">": x > y, "<=": x <= y, ">=": x >= y, "==": x == y, "!=": x != y}[op]
    return (x == val) if op == "==" else (x != val) if op == "!=" else True


def violations(sig, rules):
    """Motivos de bloqueo (lista vacía = pasa). Las reglas se COMBINAN con AND: debe cumplirlas todas."""
    return [f"edge {k}{op}{v} (valor {sig.get(k)})" for k, op, v in rules if not passes(sig, (k, op, v))]


# ── estadística de la muestra: ¿cuánto de lo medido puede ser suerte? ──
def bootstrap_ci(rs, n=5000, seed=11):
    """IC 95% de la media R por bootstrap y P(media>0). Supone operaciones independientes (las reales se
    agrupan en rachas de mercado: tómalo como cota optimista)."""
    if len(rs) < 5:
        return None
    rnd, m = random.Random(seed), len(rs)
    means = sorted(sum(rnd.choices(rs, k=m)) / m for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1], sum(1 for x in means if x > 0) / n


def mc_drawdown(rs, risk_pct, sims=3000, seed=13, ruin_pct=25.0):
    """Remuestrea las R con reemplazo y mide el drawdown máximo en % de equity (riesgo fijo por operación,
    aditivo). Devuelve (mediana, percentil 95, P(DD ≥ ruin_pct))."""
    if len(rs) < 10:
        return None
    rnd, m, dds = random.Random(seed), len(rs), []
    for _ in range(sims):
        eq = peak = dd = 0.0
        for r in rnd.choices(rs, k=m):
            eq += r * risk_pct
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        dds.append(dd)
    dds.sort()
    return dds[sims // 2], dds[int(sims * 0.95)], sum(1 for x in dds if x >= ruin_pct) / sims


# ── acelerador de curva de capital ──
def throttle_mult(rs, n, mult, floor_r=0.0):
    """Multiplicador de riesgo (1.0 normal, `mult` si las últimas n operaciones suman < floor_r)."""
    if not n or n <= 0 or len(rs) < n:
        return 1.0
    return float(mult) if sum(rs[-n:]) < floor_r else 1.0


def throttled_curve(rs, n, mult, floor_r=0.0):
    """Reaplica la serie de R con el acelerador: devuelve (total_R, drawdown_máx_R, nº_operaciones_recortadas)."""
    hist, eq, peak, dd, cut = [], 0.0, 0.0, 0.0, 0
    for r in rs:
        m = throttle_mult(hist, n, mult, floor_r)
        cut += m != 1.0
        eq += r * m
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        hist.append(r)
    return eq, dd, cut
