"""
Motor Wyckoff — port 1:1 de f_engine() del indicador "Wyckoff [theUltimator5]" (Pine v6).

Se alimenta vela a vela con velas CERRADAS (equivale a _commit = barstate.isconfirmed).
NaN hace de `na` de Pine: cualquier comparación con NaN es False, igual que en Pine.
No incluye nada visual (esquemas, etiquetas, tablas): solo detección y entradas.
Tampoco el override MTF: el bot opera las entradas del timeframe propio, que son
las únicas que disparan las alertas "entrada larga/corta" del indicador.
"""
import math

NaN = float("nan")

# ── Parámetros fijos del indicador (idénticos al Pine) ──
pivotLen = 4
adaptiveSwingMin, adaptiveSwingMax, adaptiveSwingVolLen = 2, 10, 50
trendLen, priorTrendMaxBars, priorTrendPivotFactor = 25, 200, 8.0
priorTrendStrong, priorTrendNeutral = 35.0, 15.0
continuationTrendLen, oppositeCycleMinBars, extremeLen = 200, 25, 30
phaseAMinBars, phaseAAfterArMaxBars, phaseBIdleMaxBars, phaseCIdleMaxBars = 6, 45, 125, 50
phaseAPrematureDepartureBars, phaseAPrematureDepartureTRFrac, phaseAPrematureDepartureATR = 3, 0.30, 0.75
structureInvalidationATR, structureInvalidationBars = 0.50, 2
phaseBDepartureBars, phaseBDepartureTRFrac, phaseBDepartureATR = 3, 0.35, 0.75
phaseDFailBars, phaseEMaxBars = 3, 300
volLen, atrLen = 50, 14
climaxVolMult, climaxSpreadMult, relaxedClimaxFactor = 1.8, 1.5, 0.80
prelimVolMult, prelimExtremeATR, prelimEfficiencyLookback, prelimEfficiencyRatio, prelimMinScore = 1.25, 0.75, 10, 0.80, 4
scCloseMin, bcCloseMax, climaxMinScore = 0.35, 0.65, 5
climaxAbsorptionMinBars, climaxAbsorptionMaxBars = 3, 8
climaxAbsorptionExtremeATR, climaxAbsorptionReboundATR, climaxAbsorptionVolFloor = 0.50, 0.35, 0.90
minARATR, maxARBars, arMaxExtensionATR, boundaryTolATR = 2.0, 30, 15.0, 0.75
stMaxVolRatio, stMaxSpreadRatio, stMinScore = 0.90, 0.90, 4
minPhaseBBars, minPhaseBTests, minPhaseBOppositeTests, minPhaseBTraversals = 30, 2, 1, 2
phaseBZoneFrac, phaseBRangeMinATR, phaseBRangeMaxATR = 0.25, 1.5, 8.0
phaseBBadTestCooldownFactor, phaseBEdgeExpansionCap = 2.0, 0.15
terminalTestMinBarsAfterST, terminalTestVolRatio, terminalTestSpreadRatio, terminalTestOtherMaxRatio = 3, 0.85, 0.85, 1.05
excursionRecoveryBars, springMinPenATR, springMaxPenATR = 3, 0.15, 2.5
springCloseMin, utadCloseMax, springEffortMaxMult, excursionMinScore = 0.55, 0.45, 1.50, 3
testTolATR, testMaxVolRatio, testMaxSpreadRatio, testExtremeToleranceATR, testMinScore = 1.25, 0.80, 0.80, 0.15, 2
phaseCToDMinBars, phaseDMinBars, phaseDValidationMin, phaseDDominanceFrac = 3, 4, 60, 0.65
breakATR, strengthVolMult, strengthSpreadATR = 0.15, 1.15, 1.15
sosCloseMin, sowCloseMax, strengthMinScore = 0.65, 0.35, 3
sosMultiBarLen, sosMultiBarATR, sosMultiBarEffort = 5, 2.0, 1.10
lpsBoundaryATR, lpsVolMult, lpsSpreadATR, lpsMinScore = 1.50, 1.00, 1.00, 2
confirmBars, directAcceptanceBars, directAcceptanceConfidence = 3, 5, 75
minConfidencePhaseC, minConfidencePhaseD = 45, 55
ENTRY_CFG = {  # (confianza mín, checks mín)
    "Conservador": (75, 7), "Estándar": (60, 6), "Agresivo": (55, 5),
}

DIR_NONE, DIR_ACCUM, DIR_DIST = 0, 1, -1
TYPE_NONE, TYPE_ACCUM, TYPE_REACCUM, TYPE_DIST, TYPE_REDIST = 0, 1, 2, -1, -2
REGIME_NONE, REGIME_MARKUP, REGIME_MARKDOWN = 0, 1, -1
PHASE_NONE, PHASE_A, PHASE_B, PHASE_C, PHASE_D, PHASE_E = 0, 1, 2, 3, 4, 5
(EV_NONE, EV_SC, EV_BC, EV_AR, EV_ST, EV_SPRING, EV_UTAD, EV_TEST, EV_SOS, EV_SOW,
 EV_LPS, EV_LPSY, EV_CTEST_ACC, EV_CTEST_DST, EV_MARKUP, EV_MARKDOWN) = range(16)
TEST_NONE, TEST_GOOD, TEST_POOR, TEST_FAILED = 0, 1, 2, 3
ENTRY_NONE, ENTRY_TEST, ENTRY_STRENGTH, ENTRY_LPS, ENTRY_PHASE_E = 0, 1, 2, 3, 4
BIT_PS, BIT_SC, BIT_BC, BIT_AR, BIT_ST, BIT_SPRING, BIT_UTAD, BIT_TEST = 1, 2, 4, 8, 16, 32, 64, 128
BIT_SOS, BIT_SOW, BIT_LPS, BIT_LPSY, BIT_E, BIT_CTEST = 256, 512, 1024, 2048, 4096, 16384
RS_INVALID, RS_ABSORB, RS_NORANGE, RS_DEPART, RS_STALE, RS_DEMOTE, RS_EEND = range(7)

PHASE_NAMES = {0: "-", 1: "A", 2: "B", 3: "C", 4: "D", 5: "E"}
EV_NAMES = {0: "-", 1: "SC", 2: "BC", 3: "AR", 4: "ST", 5: "Spring", 6: "UTAD", 7: "Test", 8: "SOS", 9: "SOW",
            10: "LPS", 11: "LPSY", 12: "Test terminal", 13: "Test terminal", 14: "Markup", 15: "Markdown"}
ENTRY_NAMES = {0: "-", 1: "Test Fase C", 2: "SOS/SOW", 3: "LPS/LPSY", 4: "Fase E"}
TYPE_NAMES = {0: "-", 1: "ACUMULACIÓN", 2: "REACUMULACIÓN", -1: "DISTRIBUCIÓN", -2: "REDISTRIBUCIÓN"}


# ── utilidades con semántica de Pine ──
def na(x):
    return x is None or (isinstance(x, float) and math.isnan(x))


def nz(x, y=0.0):
    return y if na(x) else x


def fmax(*v):
    return NaN if any(na(x) for x in v) else max(v)


def fmin(*v):
    return NaN if any(na(x) for x in v) else min(v)


def pround(x):  # math.round de Pine: mitades lejos de cero
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def has_bit(m, b):
    return (m // b) % 2 == 1


def add_bit(m, b):
    return m if has_bit(m, b) else m + b


class Ser:
    """Serie con índice absoluto de barra y recorte del histórico antiguo."""
    __slots__ = ("d", "off")

    def __init__(self):
        self.d, self.off = [], 0

    def push(self, v):
        self.d.append(v)

    def at(self, i):
        j = i - self.off
        return self.d[j] if 0 <= j < len(self.d) else NaN

    def trim(self, keep):
        if len(self.d) > keep:
            cut = len(self.d) - keep
            del self.d[:cut]
            self.off += cut


class WS:
    """Estado de la campaña (type WS del Pine). Las barras usan NaN como na."""

    def __init__(self):
        s = self
        s.stopSide = s.outcome = s.phase = s.ev = 0
        s.startBar = s.pivLen = NaN
        s.birthTrend = NaN
        s.ctxBull = s.ctxBear = False
        s.climaxBar = s.climaxTime = s.climaxPrice = s.climaxEff = s.climaxSpr = s.climaxATR = NaN
        s.climaxScore = 0
        s.absorbed = False
        s.psTime = s.psPrice = NaN
        s.psScore = 0
        s.origHigh = s.origLow = s.rangeHigh = s.rangeLow = NaN
        s.arRunPrice = s.arRunBar = s.arRunTime = NaN
        s.arOK = False
        s.arBar = s.arTime = s.arPrice = NaN
        s.stCount = s.stN = 0
        s.stEffSum = s.stSprSum = 0.0
        s.lastGoodEff = s.lastGoodSpr = NaN
        s.stBar = s.stTime = s.stPrice = NaN
        s.stScore = 0
        s.edge1 = s.edge2 = s.edge3 = NaN
        s.oppCount = s.opN = 0
        s.opEffSum = s.opSprSum = 0.0
        s.opLastBar = s.opLastPrice = NaN
        s.travCount = s.lastZone = 0
        s.lastZoneBar = s.lastBadBar = NaN
        s.bStartBar = s.bStartTime = s.cStartTime = s.cReadyBar = NaN
        s.dStartBar = s.dStartTime = s.eStartBar = s.eStartTime = NaN
        s.pend = False
        s.pendEdge = 0
        s.pendStartBar = s.pendExtBar = s.pendExt = s.pendExtTime = s.pendEff = s.pendSpr = s.pendCoolBar = NaN
        s.provEdge = 0
        s.provBar = s.provPrice = s.provTime = s.provEff = s.provSpr = NaN
        s.provScore = 0
        s.exc = s.excTested = False
        s.excBar = s.excPrice = s.excTime = s.excEff = s.excSpr = NaN
        s.excScore = 0
        s.testTime = s.testPrice = NaN
        s.testScore = 0
        s.strBar = s.strTime = s.strPrice = NaN
        s.strScore = 0
        s.lpsTime = s.lpsPrice = s.lpsBar = NaN
        s.lpsScore = 0
        s.entryTime = s.entryPrice = NaN
        s.entryKind = 0
        s.outCount = 0
        s.lastEventBar = s.lastEventTime = s.lastEventPrice = s.lastActBar = NaN
        s.cfPrior = s.cfClimax = s.cfAR = s.cfST = s.cfExc = s.cfTest = False
        s.cfStrength = s.cfLPS = s.cfAccept = False


# ── funciones puras del Pine ──
def structure_confidence(prior, climax, ar, st, cDone, strength, lps, accept):
    return ((10 if prior else 0) + (15 if climax else 0) + (10 if ar else 0) + (15 if st else 0)
            + (15 if cDone else 0) + (15 if strength else 0) + (10 if lps else 0) + (10 if accept else 0))


def validation_score(trend, absorbed, rangeATR, st, opp, trav, clean, cause, terminal, strength, lps, accept):
    v = 0
    v += 10 if abs(trend) >= priorTrendStrong else 5 if abs(trend) >= priorTrendNeutral else 0
    v += 15 if absorbed else 0
    if phaseBRangeMinATR <= rangeATR <= phaseBRangeMaxATR:
        v += 10
    elif phaseBRangeMinATR * 0.75 <= rangeATR <= phaseBRangeMaxATR * 1.5:
        v += 5
    v += 15 if st >= minPhaseBTests else 8 if st >= 1 else 0
    v += 15 if (opp >= 1 and trav >= 2) else 8 if (opp >= 1 or trav >= 1) else 0
    v += 10 if clean else 0
    v += 10 if cause >= 3.0 else 5 if cause >= 1.5 else 0
    v += 10 if terminal else 0
    v += 3 if strength else 0
    v += 2 if lps else 0
    return min(v, 100)


def cause_units(trav, bAge, p, rangeATR):
    timeUnits = float(bAge) / max(float(p) * 6.0, 1.0)
    return timeUnits + float(trav) * 0.5 + (0.5 if rangeATR >= phaseBRangeMinATR else 0.0)


def test_class(near, holds, closeOK, e, sp, cE, cS):
    if not near:
        return TEST_NONE
    if not holds:
        return TEST_FAILED
    if closeOK and e <= cE * stMaxVolRatio and sp <= cS * stMaxSpreadRatio:
        return TEST_GOOD
    return TEST_POOR


def phaseA_min(p):
    return max(phaseAMinBars, max(p, 1) * 2)


def phaseB_min(p, rangeATR):
    p = max(p, 1)
    raw = pround(max(float(p * 6), rangeATR * float(p)))
    return max(minPhaseBBars, min(raw, minPhaseBBars * 2))


def phaseCtoD_min(p):
    return max(phaseCToDMinBars, int(max(p, 1) / 2))


def phaseD_min(p):
    return max(phaseDMinBars, max(p, 1))


def exc_recovery_limit(p):
    return max(excursionRecoveryBars, pround(max(p, 1) * 0.75))


def phase_idle_limit(phase, hasAR, p):
    p = max(p, 1)
    if phase == PHASE_A:
        return max(phaseAAfterArMaxBars, p * 6) if hasAR else max(maxARBars, p * 4)
    if phase == PHASE_B:
        return max(phaseBIdleMaxBars, p * 20)
    if phase == PHASE_C:
        return max(phaseCIdleMaxBars, p * 8)
    if phase == PHASE_D:
        return max(phaseCIdleMaxBars * 2, p * 16)
    return NaN


def character_improving(n, effSum, sprSum, lastE, lastS):
    if n >= 2 and not na(lastE) and not na(lastS):
        return lastE <= effSum / n and lastS <= (sprSum / n) * 1.05
    return False


def robust_edge(e1, e2, e3, orig, isLow):
    med = NaN
    if not na(e1) and not na(e2) and not na(e3):
        med = e1 + e2 + e3 - max(e1, e2, e3) - min(e1, e2, e3)
    elif not na(e1) and not na(e2):
        med = ((e1 + e2) / 2.0 + orig) / 2.0
    elif not na(e1):
        med = (e1 + orig) / 2.0
    if na(med):
        return orig
    return fmax(orig, med) if isLow else fmin(orig, med)


def type_from(stop, out, bull, bear):
    if out == DIR_ACCUM:
        return TYPE_REACCUM if (stop == DIR_DIST or bull) else TYPE_ACCUM
    if out == DIR_DIST:
        return TYPE_REDIST if (stop == DIR_ACCUM or bear) else TYPE_DIST
    return TYPE_NONE


class WyckoffEngine:
    def __init__(self, tf_seconds, mintick, strictness="Estándar", keep_bars=400, range_effort=False):
        self.tfSecs = float(tf_seconds)
        self.tick = float(mintick) if mintick and mintick > 0 else 1e-8
        self.strict = strictness if strictness in ENTRY_CFG else "Estándar"
        self.keep = max(keep_bars, 300)  # el motor mira como mucho ~230 velas atrás
        self.range_effort = range_effort  # True: esfuerzo = rango de la vela (sin volumen fiable)
        self.i = -1
        self.names = ("o", "h", "l", "c", "v", "t", "eff", "avgEffRaw", "atrRaw", "a", "live", "ph", "pl",
                      "lsma", "dnE", "upE", "tr")
        for n in self.names:
            setattr(self, "_" + n, Ser())
        self._cumVol = 0.0
        self._atrSeed = []
        self.s = WS()
        self.regime = REGIME_NONE
        self.regimeBar = NaN
        self.prelimBar = self.prelimTime = self.prelimPrice = NaN
        self.prelimDir = DIR_NONE
        self.prelimScore = 0
        self.lastPLBar = self.lastPHBar = NaN
        self.rsCounts = [0] * 7
        self.lastResetWhy = -1
        self.last = {}

    # ── acceso a series: x(name, k) = name[k] en Pine ──
    def x(self, name, k=0):
        return getattr(self, "_" + name).at(self.i - k)

    def win(self, name, n, k=0):
        if n <= 0:
            return None
        out = []
        for j in range(k, k + n):
            v = self.x(name, j)
            if na(v):
                return None
            out.append(v)
        return out

    def sma(self, name, n, k=0):
        w = self.win(name, n, k)
        return NaN if w is None else sum(w) / n

    def hi(self, name, n, k=0):
        w = self.win(name, n, k)
        return NaN if w is None else max(w)

    def lo(self, name, n, k=0):
        w = self.win(name, n, k)
        return NaN if w is None else min(w)

    def pivot(self, name, L, high):
        """ta.pivothigh/pivotlow(L, L): valor en la vela central confirmada L velas después.
        Empates como TradingView: un igual a la IZQUIERDA no veta; un igual a la DERECHA sí
        (así, en un techo plano, el pivote es la última vela igual)."""
        c = self.x(name, L)
        if na(c):
            return NaN
        for j in range(1, L + 1):
            left, right = self.x(name, L + j), self.x(name, L - j)
            if na(left) or na(right):
                return NaN
            if high:
                if left > c or right >= c:
                    return NaN
            else:
                if left < c or right <= c:
                    return NaN
        return c

    def trend_score(self, n, e):
        n, e = max(n, 2), max(e, 0)
        cEnd, cStart = self.x("c", e), self.x("c", e + n)
        if na(cEnd) or na(cStart):
            return 0.0
        unitA = max(nz(self.x("a", e), self.x("a")), self.tick)
        net = cEnd - cStart
        disp = clamp(net / (unitA * math.sqrt(n)), -3.0, 3.0) * 15.0
        path = 0.0
        hh = ll = lh = hl = 0
        lastPH = lastPL = NaN
        for k in range(n - 1, -1, -1):
            o = e + k
            co = self.x("c", o)
            path += abs(nz(co) - nz(self.x("c", o + 1), nz(co)))
            vph, vpl = self.x("ph", o), self.x("pl", o)
            if not na(vph):
                if not na(lastPH):
                    hh += 1 if vph > lastPH else 0
                    lh += 1 if vph < lastPH else 0
                lastPH = vph
            if not na(vpl):
                if not na(lastPL):
                    hl += 1 if vpl > lastPL else 0
                    ll += 1 if vpl < lastPL else 0
                lastPL = vpl
        er = abs(net) / path if path > 0 else 0.0
        erPart = (1.0 if net >= 0 else -1.0) * er * 25.0
        sw = hh + ll + lh + hl
        swingPart = float(hh + hl - lh - ll) / sw * 20.0 if sw > 0 else 0.0
        smaEnd, smaStart = self.x("lsma", e), self.x("lsma", e + n)
        smaPart = 0.0
        if not na(smaEnd) and not na(smaStart):
            smaPart = 10.0 if smaEnd > smaStart else -10.0 if smaEnd < smaStart else 0.0
        return clamp(disp + erPart + swingPart + smaPart, -100.0, 100.0)

    # ── utilidades de campaña ──
    def set_event(self, s, ev):
        s.ev = ev
        s.lastEventBar = self.i
        s.lastEventTime = self.t
        s.lastEventPrice = self.c
        s.lastActBar = self.i

    @staticmethod
    def bias_dir(s):
        if s.stopSide == DIR_ACCUM:
            return DIR_DIST if s.ctxBear else DIR_ACCUM
        if s.stopSide == DIR_DIST:
            return DIR_ACCUM if s.ctxBull else DIR_DIST
        return DIR_NONE

    def type_ws(self, s):
        o = s.outcome if s.outcome != DIR_NONE else self.bias_dir(s)
        return TYPE_NONE if s.stopSide == DIR_NONE else type_from(s.stopSide, o, s.ctxBull, s.ctxBear)

    def range_atr(self, s, atrNow):
        if na(s.rangeHigh) or na(s.rangeLow):
            return 0.0
        return abs(s.rangeHigh - s.rangeLow) / max(nz(s.climaxATR, atrNow), self.tick)

    @staticmethod
    def hard_level(s, side):
        if side == DIR_ACCUM:
            if s.exc and s.outcome == DIR_ACCUM and not na(s.excPrice):
                return s.excPrice
            return s.rangeLow if na(s.origLow) else fmin(s.origLow, nz(s.rangeLow, s.origLow))
        if s.exc and s.outcome == DIR_DIST and not na(s.excPrice):
            return s.excPrice
        return s.rangeHigh if na(s.origHigh) else fmax(s.origHigh, nz(s.rangeHigh, s.origHigh))

    def seed(self, s, side, score, pLen, trend, bull, bear, atrNow, effNow, sprNow):
        s.stopSide = side
        s.phase = PHASE_A
        s.startBar = self.i
        s.pivLen = pLen
        s.birthTrend = trend
        s.ctxBull, s.ctxBear = bull, bear
        s.climaxBar = self.i
        s.climaxTime = self.t
        s.climaxPrice = self.l if side == DIR_ACCUM else self.h
        s.climaxEff, s.climaxSpr, s.climaxATR, s.climaxScore = effNow, sprNow, atrNow, score
        if side == DIR_ACCUM:
            s.origLow = s.rangeLow = self.l
        else:
            s.origHigh = s.rangeHigh = self.h
        s.cfPrior = True
        self.set_event(s, EV_SC if side == DIR_ACCUM else EV_BC)

    @staticmethod
    def freeze_ar(s):
        s.arBar, s.arTime, s.arPrice = s.arRunBar, s.arRunTime, s.arRunPrice
        s.cfAR = True
        if s.stopSide == DIR_ACCUM:
            s.origHigh = s.rangeHigh = s.arRunPrice
        else:
            s.origLow = s.rangeLow = s.arRunPrice

    def register_st(self, s, price, t, pb, e, sp, score):
        s.stCount += 1
        s.stN += 1
        s.stEffSum += e
        s.stSprSum += sp
        s.lastGoodEff, s.lastGoodSpr = e, sp
        s.stBar, s.stTime, s.stPrice, s.stScore = pb, t, price, score
        s.edge3, s.edge2, s.edge1 = s.edge2, s.edge1, price
        if s.stopSide == DIR_ACCUM:
            s.rangeLow = robust_edge(s.edge1, s.edge2, s.edge3, s.origLow, True)
        else:
            s.rangeHigh = robust_edge(s.edge1, s.edge2, s.edge3, s.origHigh, False)
        s.cfST = True
        self.set_event(s, EV_ST)

    def register_opp(self, s, price, t, pb, e, sp):
        s.oppCount += 1
        s.opN += 1
        s.opEffSum += e
        s.opSprSum += sp
        s.opLastBar, s.opLastPrice = pb, price
        s.lastActBar = self.i

    @staticmethod
    def clear_terminal(s):
        s.exc = s.excTested = False
        s.excBar = s.excPrice = s.excTime = s.excEff = s.excSpr = NaN
        s.excScore = 0
        s.testTime = s.testPrice = NaN
        s.testScore = 0
        s.cfExc = s.cfTest = False
        s.cReadyBar = s.cStartTime = NaN

    def exc_from_pend(self, s, score):
        s.exc, s.excTested = True, False
        s.excBar, s.excPrice, s.excTime = s.pendExtBar, s.pendExt, s.pendExtTime
        s.excEff, s.excSpr, s.excScore = s.pendEff, s.pendSpr, score
        s.cfExc = True
        s.outcome = s.pendEdge
        s.cStartTime = s.pendExtTime
        s.phase = PHASE_C
        s.cReadyBar = NaN
        s.pend = False
        s.provEdge = 0
        self.set_event(s, EV_SPRING if s.outcome == DIR_ACCUM else EV_UTAD)

    @staticmethod
    def adopt_prov(s):
        s.exc, s.excTested = True, False
        s.excBar, s.excPrice, s.excTime = s.provBar, s.provPrice, s.provTime
        s.excEff, s.excSpr, s.excScore = s.provEff, s.provSpr, s.provScore
        s.cfExc = True
        s.outcome = s.provEdge
        s.cStartTime = s.provTime
        s.provEdge = 0

    def enter_ctest(self, s, outc, t, price, score):
        s.phase = PHASE_C
        s.outcome = outc
        s.cStartTime = t
        s.cReadyBar = self.i
        s.testTime, s.testPrice, s.testScore = t, price, score
        s.cfTest = True
        s.pend = False
        self.set_event(s, EV_CTEST_ACC if outc == DIR_ACCUM else EV_CTEST_DST)

    def promote_d(self, s, outc, score, price):
        s.outcome = outc
        s.phase = PHASE_D
        s.dStartBar, s.dStartTime = self.i, self.t
        s.strBar, s.strTime, s.strPrice, s.strScore = self.i, self.t, price, score
        s.cfStrength = True
        s.pend = False
        s.outCount = 0
        self.set_event(s, EV_SOS if outc == DIR_ACCUM else EV_SOW)

    def demote_to_b(self, s):
        self.clear_terminal(s)
        s.outcome = DIR_NONE
        s.phase = PHASE_B
        s.strBar = s.strTime = s.strPrice = NaN
        s.strScore = 0
        s.lpsTime = s.lpsPrice = s.lpsBar = NaN
        s.lpsScore = 0
        s.dStartBar = s.dStartTime = NaN
        s.cfStrength = s.cfLPS = False
        s.provEdge = 0
        s.pend = False
        s.outCount = 0
        s.lastBadBar = self.i
        s.lastActBar = self.i

    @staticmethod
    def conf_ws(s):
        cDone = s.cfTest or (s.cfExc and s.cfStrength)
        return structure_confidence(s.cfPrior, s.cfClimax, s.cfAR, s.cfST, cDone, s.cfStrength, s.cfLPS, s.cfAccept)

    def validation_ws(self, s, p, trendNow, atrNow):
        if s.stopSide == DIR_NONE:
            return 0
        rATR = self.range_atr(s, atrNow)
        bAge = max(self.i - s.bStartBar, 0) if not na(s.bStartBar) else 0
        badAge = -1 if na(s.lastBadBar) else self.i - s.lastBadBar
        clean = badAge < 0 or badAge >= pround(p * phaseBBadTestCooldownFactor)
        cause = cause_units(s.travCount, bAge, p, rATR)
        terminal = (s.cfTest and (not s.exc or s.excTested)) or (s.cfExc and s.cfStrength)
        return validation_score(nz(s.birthTrend, trendNow), s.absorbed, rATR, s.stCount, s.oppCount, s.travCount,
                                clean, cause, terminal, s.cfStrength, s.cfLPS, s.cfAccept)

    def mature_ws(self, s, p, atrNow):
        rATR = self.range_atr(s, atrNow)
        bAge = self.i - s.bStartBar if not na(s.bStartBar) else 0
        bMin = phaseB_min(p, rATR)
        badAge = -1 if na(s.lastBadBar) else self.i - s.lastBadBar
        clean = badAge < 0 or badAge >= pround(p * phaseBBadTestCooldownFactor)
        opp = minPhaseBOppositeTests <= 0 or s.oppCount >= minPhaseBOppositeTests
        trav = minPhaseBTraversals <= 0 or s.travCount >= minPhaseBTraversals
        ch = character_improving(s.stN, s.stEffSum, s.stSprSum, s.lastGoodEff, s.lastGoodSpr)
        support = int(opp) + int(trav) + int(clean) + int(ch)
        sane = phaseBRangeMinATR * 0.75 <= rATR <= phaseBRangeMaxATR * 2.0
        return s.phase == PHASE_B and bAge >= bMin and s.stCount >= minPhaseBTests and sane and support >= 2

    def entry_readiness(self, s, minConf, valFloor, atrNow):
        bull = s.outcome == DIR_ACCUM
        u = max(atrNow, self.tick)
        if na(s.rangeHigh) or na(s.rangeLow):
            distATR = 99.0
        else:
            distATR = (self.c - s.rangeHigh) / u if bull else (s.rangeLow - self.c) / u
        ageBars = (self.t - s.lastEventTime) / max(self.tfSecs * 1000.0, 1.0) if not na(s.lastEventTime) else 999.0
        terminal = (s.exc and s.excTested) or (not s.exc and s.cfTest)
        conf = self.conf_ws(s)
        r = 0
        r += 1 if s.outcome != DIR_NONE else 0
        r += 1 if s.phase >= PHASE_C else 0
        r += 1 if terminal else 0
        r += 1 if s.cfStrength else 0
        r += 1 if s.cfLPS else 0
        r += 1 if conf >= minConf else 0
        r += 1 if self.validation_ws(s, int(nz(s.pivLen, pivotLen)), s.birthTrend, atrNow) >= valFloor else 0
        r += 1 if distATR <= 2.0 else 0
        r += 1 if ageBars <= 50.0 else 0
        return r

    def reset(self, why):
        self.rsCounts[why] += 1
        self.lastResetWhy = why
        self.s = WS()

    # ══════════════════════════════════════════════════════════════════════
    def update(self, t, o, h, l, c, v):
        """Procesa una vela CERRADA. t en ms (apertura de la vela). Devuelve dict de estado."""
        self.i += 1
        i = self.i
        tick = self.tick
        self.t, self.o, self.h, self.l, self.c = float(t), o, h, l, c
        for n, val in (("o", o), ("h", h), ("l", l), ("c", c), ("v", v), ("t", float(t))):
            getattr(self, "_" + n).push(val)

        # ── medidas de la vela ──
        rawVol = nz(v)
        self._cumVol += rawVol
        noVol = self.range_effort or self._cumVol <= 0.0
        spr = max(h - l, tick)
        eff = spr if noVol else rawVol
        self._eff.push(eff)
        avgEffRaw = self.sma("eff", volLen)
        self._avgEffRaw.push(avgEffRaw)
        avgEff = nz(avgEffRaw, eff)
        avgEffPrev = nz(self.x("avgEffRaw", 1), avgEff)

        # ATR (RMA de Pine, semilla SMA)
        c1 = self.x("c", 1)
        tr = h - l if na(c1) else max(h - l, abs(h - c1), abs(l - c1))
        prevAtr = self.x("atrRaw", 1)
        if na(prevAtr):
            self._atrSeed.append(tr)
            atrRaw = sum(self._atrSeed[-atrLen:]) / atrLen if len(self._atrSeed) >= atrLen else NaN
        else:
            atrRaw = (prevAtr * (atrLen - 1) + tr) / atrLen
        self._atrRaw.push(atrRaw)
        a = nz(atrRaw, spr)
        self._a.push(a)
        aPrev = nz(self.x("atrRaw", 1), a)
        unit = max(a, tick)
        cPos = (c - l) / spr
        relE = eff / max(avgEff, 1e-10)
        sprATR = spr / unit
        recentEff = self.sma("eff", climaxAbsorptionMinBars)
        recentLow = self.lo("l", climaxAbsorptionMinBars)
        recentHigh = self.hi("h", climaxAbsorptionMinBars)
        priorLo = self.lo("l", extremeLen, 1)
        priorHi = self.hi("h", extremeLen, 1)
        effHiPrior = self.hi("eff", extremeLen, 1)
        newLow = not na(priorLo) and l <= priorLo
        newHigh = not na(priorHi) and h >= priorHi
        aBase = self.sma("a", adaptiveSwingVolLen)
        aRatio = a / aBase if nz(aBase) > 0.0 else 1.0
        liveLen = max(adaptiveSwingMin, min(adaptiveSwingMax, pround(pivotLen * aRatio)))
        self._live.push(liveLen)
        preLen = int(nz(self.x("live", 3), liveLen))

        s = self.s
        pLen = int(nz(s.pivLen, liveLen)) if s.phase != PHASE_NONE else liveLen
        ph = self.pivot("h", pLen, True)
        pl = self.pivot("l", pLen, False)
        self._ph.push(ph)
        self._pl.push(pl)
        pivBar = i - pLen
        pA = max(nz(self.x("atrRaw", pLen), a), tick)
        pEff = nz(self.x("eff", pLen), eff)
        pSpr = max(nz(self.x("h", pLen), h) - nz(self.x("l", pLen), l), tick)
        pCPos = (nz(self.x("c", pLen), c) - nz(self.x("l", pLen), l)) / pSpr
        pAvgEff = nz(self.x("avgEffRaw", pLen), avgEff)
        pTime = float(nz(self.x("t", pLen), t))

        longSma = self.sma("c", continuationTrendLen)
        self._lsma.push(longSma)
        trendBars = max(trendLen, min(priorTrendMaxBars, pround(liveLen * priorTrendPivotFactor)))
        trendEnd = max(2, min(8, pround(liveLen * 0.75)))
        trendScore = self.trend_score(trendBars, trendEnd)
        downTrend = trendScore <= -priorTrendNeutral
        upTrend = trendScore >= priorTrendNeutral
        strongTrend = abs(trendScore) >= priorTrendStrong
        ls1, ls20 = self.x("lsma", 1), self.x("lsma", 20)
        longBull = nz(c1) > nz(ls1) and nz(ls1) > nz(ls20)
        longBear = (not na(ls20)) and nz(c1) < nz(ls1) and nz(ls1) < nz(ls20)

        effort2 = max(relE, 0.01) * max(sprATR, 0.01)
        dnEffc = max(nz(c1, c) - c, 0.0) / unit / effort2
        upEffc = max(c - nz(c1, c), 0.0) / unit / effort2
        self._dnE.push(dnEffc)
        self._upE.push(upEffc)
        dnBase = self.sma("dnE", prelimEfficiencyLookback, 1)
        upBase = self.sma("upE", prelimEfficiencyLookback, 1)
        dnAbsorb = not na(dnBase) and dnEffc <= dnBase * prelimEfficiencyRatio
        upAbsorb = not na(upBase) and upEffc <= upBase * prelimEfficiencyRatio
        nearRecentLow = not na(priorLo) and l <= priorLo + a * prelimExtremeATR
        nearRecentHigh = not na(priorHi) and h >= priorHi - a * prelimExtremeATR

        hiCInv, loCInv = self.hi("c", structureInvalidationBars), self.lo("c", structureInvalidationBars)
        hiCDep, loCDep = self.hi("c", phaseBDepartureBars), self.lo("c", phaseBDepartureBars)
        hiCA, loCA = self.hi("c", phaseAPrematureDepartureBars), self.lo("c", phaseAPrematureDepartureBars)
        hiCConf, loCConf = self.hi("c", confirmBars), self.lo("c", confirmBars)
        hiCDir, loCDir = self.hi("c", directAcceptanceBars), self.lo("c", directAcceptanceBars)
        hiCFail, loCFail = self.hi("c", phaseDFailBars), self.lo("c", phaseDFailBars)
        mbLow, mbHigh = self.lo("l", sosMultiBarLen), self.hi("h", sosMultiBarLen)
        wEff = self.win("eff", sosMultiBarLen)
        mbEffRatio = (sum(wEff) / max(avgEff * sosMultiBarLen, 1e-10)) if wEff else NaN
        mbSOS = (c - mbLow) / unit >= sosMultiBarATR and mbEffRatio >= sosMultiBarEffort and cPos >= 0.50
        mbSOW = (mbHigh - c) / unit >= sosMultiBarATR and mbEffRatio >= sosMultiBarEffort and cPos <= 0.50
        c3 = self.x("c", 3)
        res3Up = max(c - nz(c3, c), 0.0) / unit
        res3Dn = max(nz(c3, c) - c, 0.0) / unit
        sosScore = (int(relE >= strengthVolMult) + int(sprATR >= strengthSpreadATR) + int(cPos >= sosCloseMin)
                    + int(c > o and c - o >= a * 0.50))
        sowScore = (int(relE >= strengthVolMult) + int(sprATR >= strengthSpreadATR) + int(cPos <= sowCloseMax)
                    + int(c < o and o - c >= a * 0.50))
        close1, close2 = nz(c1, c), nz(self.x("c", 2), c)
        ctxBars = max(priorTrendMaxBars, continuationTrendLen) * 2
        sig = 0
        regime = self.regime

        # ═══ bloque _commit ═══
        p0 = int(nz(s.pivLen, liveLen))
        rk0 = not na(s.rangeHigh) and not na(s.rangeLow)
        rH0, rL0 = s.rangeHigh, s.rangeLow
        rHt0 = max(rH0 - rL0, tick) if rk0 else NaN
        rMid0 = (rH0 + rL0) / 2.0 if rk0 else NaN
        probeOpen = s.pend and i - s.pendStartBar <= exc_recovery_limit(p0)
        structAge = i - s.startBar if not na(s.startBar) else 0
        ageOKBull = regime != REGIME_MARKDOWN or structAge >= oppositeCycleMinBars
        ageOKBear = regime != REGIME_MARKUP or structAge >= oppositeCycleMinBars
        hardBull0 = self.hard_level(s, DIR_ACCUM)
        hardBear0 = self.hard_level(s, DIR_DIST)
        resetWhy = -1

        # Retira estructuras que ya no encajan
        if s.phase == PHASE_A:
            aClimaxAge = i - s.climaxBar
            aInvAcc = s.stopSide == DIR_ACCUM and hiCInv < s.climaxPrice - a * structureInvalidationATR
            aInvDst = s.stopSide == DIR_DIST and loCInv > s.climaxPrice + a * structureInvalidationATR
            aAbsorbExp = (not s.absorbed) and aClimaxAge > climaxAbsorptionMaxBars
            aNoAR = (not s.arOK) and aClimaxAge > maxARBars
            aOverExt = s.arOK and not na(s.arRunPrice) and \
                abs(s.arRunPrice - s.climaxPrice) / max(nz(s.climaxATR, a), tick) > arMaxExtensionATR
            aBuf = max(rHt0 * phaseAPrematureDepartureTRFrac, a * phaseAPrematureDepartureATR) if rk0 else NaN
            aDepart = aClimaxAge > maxARBars and rk0 and (
                (s.stopSide == DIR_ACCUM and loCA > rH0 + aBuf) or (s.stopSide == DIR_DIST and hiCA < rL0 - aBuf))
            if aInvAcc or aInvDst:
                resetWhy = RS_INVALID
            elif aAbsorbExp:
                resetWhy = RS_ABSORB
            elif aNoAR or aOverExt or aDepart:
                resetWhy = RS_NORANGE

        if s.phase == PHASE_C and not probeOpen and resetWhy < 0:
            if s.outcome == DIR_ACCUM and not na(hardBull0) and hiCInv < hardBull0 - a * structureInvalidationATR:
                resetWhy = RS_INVALID
            elif s.outcome == DIR_DIST and not na(hardBear0) and loCInv > hardBear0 + a * structureInvalidationATR:
                resetWhy = RS_INVALID

        if s.phase == PHASE_D and rk0 and resetWhy < 0:
            if s.outcome == DIR_ACCUM:
                if not na(hardBull0) and hiCInv < hardBull0 - a * structureInvalidationATR:
                    resetWhy = RS_INVALID
                elif hiCFail < rMid0:
                    self.demote_to_b(s)
                    self.rsCounts[RS_DEMOTE] += 1
            elif s.outcome == DIR_DIST:
                if not na(hardBear0) and loCInv > hardBear0 + a * structureInvalidationATR:
                    resetWhy = RS_INVALID
                elif loCFail > rMid0:
                    self.demote_to_b(s)
                    self.rsCounts[RS_DEMOTE] += 1

        if s.phase == PHASE_E and rk0 and resetWhy < 0:
            eFail = hiCFail < rMid0 if s.outcome == DIR_ACCUM else loCFail > rMid0
            eOld = phaseEMaxBars > 0 and not na(s.eStartBar) and i - s.eStartBar > phaseEMaxBars
            if eFail or eOld:
                resetWhy = RS_EEND

        idleLim = phase_idle_limit(s.phase, s.arOK, p0)
        if resetWhy < 0 and not probeOpen and not na(idleLim) and not na(s.lastActBar) and i - s.lastActBar > idleLim:
            resetWhy = RS_STALE

        # Estructura que se rompe por el nivel duro estando en Fase C/D con dirección ya decidida:
        # los que operaron el Spring/UTAD quedan atrapados. Se publica para la idea "trampa".
        fail = None
        if resetWhy == RS_INVALID and s.phase in (PHASE_C, PHASE_D) and s.outcome != DIR_NONE:
            fail = {"side": "SHORT" if s.outcome == DIR_ACCUM else "LONG",
                    "hard": hardBull0 if s.outcome == DIR_ACCUM else hardBear0,
                    "rh": s.rangeHigh, "rl": s.rangeLow, "phase": s.phase, "had_entry": not na(s.entryTime),
                    "conf": self.conf_ws(s), "val": self.validation_ws(s, p0, trendScore, a),
                    "range_atr": self.range_atr(s, a),
                    "b_bars": (i - s.bStartBar) if not na(s.bStartBar) else 0}
        if resetWhy >= 0:
            self.reset(resetWhy)
            s = self.s

        # Soporte/oferta preliminar y nuevos clímax
        if s.phase in (PHASE_NONE, PHASE_E):
            psSc = (int(downTrend) + int(nearRecentLow) + int(relE >= prelimVolMult) + int(sprATR >= 0.80)
                    + int(cPos >= 0.45) + int(dnAbsorb))
            psySc = (int(upTrend) + int(nearRecentHigh) + int(relE >= prelimVolMult) + int(sprATR >= 0.80)
                     + int(cPos <= 0.55) + int(upAbsorb))
            psStale = na(self.prelimBar) or i - self.prelimBar > extremeLen
            if downTrend and nearRecentLow and relE >= prelimVolMult and psSc >= prelimMinScore and \
                    (self.prelimDir != DIR_ACCUM or psStale or psSc >= self.prelimScore):
                self.prelimBar, self.prelimTime, self.prelimPrice = i, self.t, l
                self.prelimDir, self.prelimScore = DIR_ACCUM, psSc
            elif upTrend and nearRecentHigh and relE >= prelimVolMult and psySc >= prelimMinScore and \
                    (self.prelimDir != DIR_DIST or psStale or psySc >= self.prelimScore):
                self.prelimBar, self.prelimTime, self.prelimPrice = i, self.t, h
                self.prelimDir, self.prelimScore = DIR_DIST, psySc

        relaxK = relaxedClimaxFactor if strongTrend else 1.0
        volHit = eff >= avgEffPrev * climaxVolMult * relaxK
        sprHit = spr >= aPrev * climaxSpreadMult * relaxK
        exceptional = (not na(effHiPrior) and eff >= effHiPrior) or spr >= aPrev * climaxSpreadMult * 1.5
        scSc = int(downTrend) + int(newLow) + int(volHit) + int(sprHit) + int(cPos >= scCloseMin) + int(exceptional)
        bcSc = int(upTrend) + int(newHigh) + int(volHit) + int(sprHit) + int(cPos <= bcCloseMax) + int(exceptional)
        seedOpen = s.phase in (PHASE_NONE, PHASE_E)
        canSC = seedOpen or (s.phase == PHASE_A and s.stopSide == DIR_ACCUM and l < s.climaxPrice)
        canBC = seedOpen or (s.phase == PHASE_A and s.stopSide == DIR_DIST and h > s.climaxPrice)
        isSC = canSC and downTrend and newLow and volHit and sprHit and scSc >= climaxMinScore
        isBC = canBC and upTrend and newHigh and volHit and sprHit and bcSc >= climaxMinScore
        if isSC != isBC:
            seedSide = DIR_ACCUM if isSC else DIR_DIST
            recentMarkup = regime == REGIME_MARKUP and not na(self.regimeBar) and i - self.regimeBar <= ctxBars
            recentMarkdown = regime == REGIME_MARKDOWN and not na(self.regimeBar) and i - self.regimeBar <= ctxBars
            self.s = s = WS()
            self.seed(s, seedSide, scSc if isSC else bcSc, preLen, trendScore, recentMarkup or longBull,
                      recentMarkdown or longBear, a, eff, spr)
            if self.prelimDir == seedSide and not na(self.prelimBar) and self.prelimBar < i and \
                    i - self.prelimBar <= extremeLen:
                s.psTime, s.psPrice, s.psScore = self.prelimTime, self.prelimPrice, self.prelimScore

        # Confirma la parada y construye el AR
        if s.phase == PHASE_A and not s.absorbed and not na(s.climaxBar):
            absAge = i - s.climaxBar
            if climaxAbsorptionMinBars <= absAge <= climaxAbsorptionMaxBars:
                effortOK = recentEff >= avgEff * climaxAbsorptionVolFloor
                accAbs = s.stopSide == DIR_ACCUM and \
                    recentLow >= s.climaxPrice - s.climaxATR * climaxAbsorptionExtremeATR and \
                    c >= s.climaxPrice + s.climaxATR * climaxAbsorptionReboundATR
                dstAbs = s.stopSide == DIR_DIST and \
                    recentHigh <= s.climaxPrice + s.climaxATR * climaxAbsorptionExtremeATR and \
                    c <= s.climaxPrice - s.climaxATR * climaxAbsorptionReboundATR
                if effortOK and (accAbs or dstAbs):
                    s.absorbed = True
                    s.cfClimax = True
                    s.lastActBar = i
                    sig = add_bit(sig, BIT_SC if s.stopSide == DIR_ACCUM else BIT_BC)
                    if not na(s.psTime):
                        sig = add_bit(sig, BIT_PS)

        if s.phase == PHASE_A and na(s.arBar) and not na(s.climaxBar) and i > s.climaxBar and \
                i - s.climaxBar <= maxARBars:
            if s.stopSide == DIR_ACCUM:
                if na(s.arRunPrice) or h > s.arRunPrice:
                    s.arRunPrice, s.arRunBar, s.arRunTime = h, i, self.t
                s.arOK = s.arOK or s.arRunPrice - s.climaxPrice >= a * minARATR
            else:
                if na(s.arRunPrice) or l < s.arRunPrice:
                    s.arRunPrice, s.arRunBar, s.arRunTime = l, i, self.t
                s.arOK = s.arOK or s.climaxPrice - s.arRunPrice >= a * minARATR
        if s.phase == PHASE_A and s.arOK and s.absorbed and na(s.arBar):
            if s.stopSide == DIR_ACCUM:
                s.rangeHigh = s.origHigh = s.arRunPrice
            else:
                s.rangeLow = s.origLow = s.arRunPrice

        p1 = int(nz(s.pivLen, liveLen))
        rk1 = not na(s.rangeHigh) and not na(s.rangeLow)
        rH1, rL1 = s.rangeHigh, s.rangeLow
        origHt1 = max(nz(s.origHigh, rH1) - nz(s.origLow, rL1), tick) if rk1 else NaN
        mature1 = self.mature_ws(s, p1, a)
        newPL = not na(pl) and (na(self.lastPLBar) or pivBar > self.lastPLBar)
        newPH = not na(ph) and (na(self.lastPHBar) or pivBar > self.lastPHBar)
        if newPL:
            self.lastPLBar = pivBar
        if newPH:
            self.lastPHBar = pivBar
        plUsed = phUsed = False

        # Tests terminales (Fase C sin Spring/UTAD)
        if newPL and s.phase == PHASE_B and mature1 and rk1:
            loRefST = s.stopSide == DIR_ACCUM
            tRefN = s.stN if loRefST else s.opN
            tRefBar = s.stBar if loRefST else s.opLastBar
            tRefPrice = s.stPrice if loRefST else s.opLastPrice
            if tRefN > 0 and not na(tRefBar):
                tRefEff = (s.stEffSum if loRefST else s.opEffSum) / tRefN
                tRefSpr = (s.stSprSum if loRefST else s.opSprSum) / tRefN
                tLater = pivBar >= tRefBar + terminalTestMinBarsAfterST
                tNear = pl <= rL1 + pA * testTolATR
                tHolds = pl >= min(nz(s.origLow, rL1), rL1) - pA * testExtremeToleranceATR
                tHigher = na(tRefPrice) or pl >= tRefPrice - pA * testExtremeToleranceATR
                tqV, tqS = pEff <= tRefEff * terminalTestVolRatio, pSpr <= tRefSpr * terminalTestSpreadRatio
                tnV, tnS = pEff <= tRefEff * terminalTestOtherMaxRatio, pSpr <= tRefSpr * terminalTestOtherMaxRatio
                tSc = int(tqV) + int(tqS) + int(pCPos >= 0.50)
                if tLater and tNear and tHolds and tHigher and ((tqV and tnS) or (tqS and tnV)) and tSc >= testMinScore:
                    self.enter_ctest(s, DIR_ACCUM, pTime, pl, tSc)
                    sig = add_bit(sig, BIT_CTEST)
                    plUsed = True
        if newPH and s.phase == PHASE_B and mature1 and rk1:
            hiRefST = s.stopSide == DIR_DIST
            uRefN = s.stN if hiRefST else s.opN
            uRefBar = s.stBar if hiRefST else s.opLastBar
            uRefPrice = s.stPrice if hiRefST else s.opLastPrice
            if uRefN > 0 and not na(uRefBar):
                uRefEff = (s.stEffSum if hiRefST else s.opEffSum) / uRefN
                uRefSpr = (s.stSprSum if hiRefST else s.opSprSum) / uRefN
                uLater = pivBar >= uRefBar + terminalTestMinBarsAfterST
                uNear = ph >= rH1 - pA * testTolATR
                uHolds = ph <= max(nz(s.origHigh, rH1), rH1) + pA * testExtremeToleranceATR
                uLower = na(uRefPrice) or ph <= uRefPrice + pA * testExtremeToleranceATR
                uqV, uqS = pEff <= uRefEff * terminalTestVolRatio, pSpr <= uRefSpr * terminalTestSpreadRatio
                unV, unS = pEff <= uRefEff * terminalTestOtherMaxRatio, pSpr <= uRefSpr * terminalTestOtherMaxRatio
                uSc = int(uqV) + int(uqS) + int(pCPos <= 0.50)
                if uLater and uNear and uHolds and uLower and ((uqV and unS) or (uqS and unV)) and uSc >= testMinScore:
                    self.enter_ctest(s, DIR_DIST, pTime, ph, uSc)
                    sig = add_bit(sig, BIT_CTEST)
                    phUsed = True

        # Fase A → B (AR fijado + ST)
        if newPL and not plUsed and s.phase == PHASE_A and s.stopSide == DIR_ACCUM and s.absorbed and s.arOK \
                and not na(s.arRunBar) and pivBar > s.arRunBar:
            aZoneTop = s.origLow + (s.arRunPrice - s.origLow) * phaseBZoneFrac
            aNearL = abs(pl - s.origLow) <= pA * boundaryTolATR or pl <= aZoneTop
            aHoldsL = pl >= s.origLow - pA * boundaryTolATR
            aCloseL = pCPos >= 0.45
            aTcL = test_class(aNearL, aHoldsL, aCloseL, pEff, pSpr, s.climaxEff, s.climaxSpr)
            aScL = (int(aNearL) + int(pEff <= s.climaxEff * stMaxVolRatio) + int(pSpr <= s.climaxSpr * stMaxSpreadRatio)
                    + int(aHoldsL) + int(aCloseL) + 1)
            aAgeL = pivBar - s.startBar >= phaseA_min(p1)
            aSpaceL = pivBar - s.arRunBar >= max(2, p1)
            if aNearL:
                s.lastActBar = i
            if aTcL == TEST_GOOD and aScL >= stMinScore and aAgeL and aSpaceL:
                self.freeze_ar(s)
                self.register_st(s, pl, pTime, pivBar, pEff, pSpr, aScL)
                s.phase = PHASE_B
                s.bStartBar, s.bStartTime = pivBar, pTime
                sig = add_bit(add_bit(sig, BIT_AR), BIT_ST)
                plUsed = True
        if newPH and not phUsed and s.phase == PHASE_A and s.stopSide == DIR_DIST and s.absorbed and s.arOK \
                and not na(s.arRunBar) and pivBar > s.arRunBar:
            aZoneBot = s.origHigh - (s.origHigh - s.arRunPrice) * phaseBZoneFrac
            aNearH = abs(ph - s.origHigh) <= pA * boundaryTolATR or ph >= aZoneBot
            aHoldsH = ph <= s.origHigh + pA * boundaryTolATR
            aCloseH = pCPos <= 0.55
            aTcH = test_class(aNearH, aHoldsH, aCloseH, pEff, pSpr, s.climaxEff, s.climaxSpr)
            aScH = (int(aNearH) + int(pEff <= s.climaxEff * stMaxVolRatio) + int(pSpr <= s.climaxSpr * stMaxSpreadRatio)
                    + int(aHoldsH) + int(aCloseH) + 1)
            aAgeH = pivBar - s.startBar >= phaseA_min(p1)
            aSpaceH = pivBar - s.arRunBar >= max(2, p1)
            if aNearH:
                s.lastActBar = i
            if aTcH == TEST_GOOD and aScH >= stMinScore and aAgeH and aSpaceH:
                self.freeze_ar(s)
                self.register_st(s, ph, pTime, pivBar, pEff, pSpr, aScH)
                s.phase = PHASE_B
                s.bStartBar, s.bStartTime = pivBar, pTime
                sig = add_bit(add_bit(sig, BIT_AR), BIT_ST)
                phUsed = True

        # Fase B: ST en el lado del clímax y tests del borde opuesto
        rk1 = not na(s.rangeHigh) and not na(s.rangeLow)
        if newPL and not plUsed and s.phase == PHASE_B and rk1 and pivBar > s.bStartBar:
            bHt = max(s.rangeHigh - s.rangeLow, tick)
            if s.stopSide == DIR_ACCUM:
                bNear = abs(pl - s.rangeLow) <= pA * boundaryTolATR or abs(pl - s.origLow) <= pA * boundaryTolATR \
                    or pl <= s.rangeLow + bHt * phaseBZoneFrac
                bHolds = pl >= s.origLow - pA * boundaryTolATR
                bClose = pCPos >= 0.45
                bTc = test_class(bNear, bHolds, bClose, pEff, pSpr, s.climaxEff, s.climaxSpr)
                bSc = (int(bNear) + int(pEff <= s.climaxEff * stMaxVolRatio) + int(pSpr <= s.climaxSpr * stMaxSpreadRatio)
                       + int(bHolds) + int(bClose) + 1)
                bIsProv = s.provEdge == DIR_ACCUM and not na(s.provBar) and abs(pivBar - s.provBar) <= p1
                if bNear:
                    s.lastActBar = i
                if bTc == TEST_FAILED and not bIsProv:
                    s.lastBadBar = pivBar
                elif bTc == TEST_GOOD and bSc >= stMinScore:
                    self.register_st(s, pl, pTime, pivBar, pEff, pSpr, bSc)
                    sig = add_bit(sig, BIT_ST)
            else:
                if pl < s.rangeLow and pl >= nz(s.origLow, s.rangeLow) - origHt1 * phaseBEdgeExpansionCap:
                    s.rangeLow = pl
                oHtL = max(s.rangeHigh - s.rangeLow, tick)
                oNearL = pl <= s.rangeLow + pA * boundaryTolATR or pl <= s.rangeLow + oHtL * phaseBZoneFrac
                oInsideL = pl >= nz(s.origLow, s.rangeLow) - origHt1 * phaseBEdgeExpansionCap
                oCtrlL = pEff <= s.climaxEff * 1.20 and pSpr <= s.climaxSpr * 1.20
                if oNearL and oInsideL and oCtrlL and (na(s.opLastBar) or pivBar > s.opLastBar):
                    self.register_opp(s, pl, pTime, pivBar, pEff, pSpr)
        if newPH and not phUsed and s.phase == PHASE_B and rk1 and pivBar > s.bStartBar:
            bHtH = max(s.rangeHigh - s.rangeLow, tick)
            if s.stopSide == DIR_DIST:
                hNear = abs(ph - s.rangeHigh) <= pA * boundaryTolATR or abs(ph - s.origHigh) <= pA * boundaryTolATR \
                    or ph >= s.rangeHigh - bHtH * phaseBZoneFrac
                hHolds = ph <= s.origHigh + pA * boundaryTolATR
                hClose = pCPos <= 0.55
                hTc = test_class(hNear, hHolds, hClose, pEff, pSpr, s.climaxEff, s.climaxSpr)
                hSc = (int(hNear) + int(pEff <= s.climaxEff * stMaxVolRatio) + int(pSpr <= s.climaxSpr * stMaxSpreadRatio)
                       + int(hHolds) + int(hClose) + 1)
                hIsProv = s.provEdge == DIR_DIST and not na(s.provBar) and abs(pivBar - s.provBar) <= p1
                if hNear:
                    s.lastActBar = i
                if hTc == TEST_FAILED and not hIsProv:
                    s.lastBadBar = pivBar
                elif hTc == TEST_GOOD and hSc >= stMinScore:
                    self.register_st(s, ph, pTime, pivBar, pEff, pSpr, hSc)
                    sig = add_bit(sig, BIT_ST)
            else:
                if ph > s.rangeHigh and ph <= nz(s.origHigh, s.rangeHigh) + origHt1 * phaseBEdgeExpansionCap:
                    s.rangeHigh = ph
                oHtH = max(s.rangeHigh - s.rangeLow, tick)
                oNearH = ph >= s.rangeHigh - pA * boundaryTolATR or ph >= s.rangeHigh - oHtH * phaseBZoneFrac
                oInsideH = ph <= nz(s.origHigh, s.rangeHigh) + origHt1 * phaseBEdgeExpansionCap
                oCtrlH = pEff <= s.climaxEff * 1.20 and pSpr <= s.climaxSpr * 1.20
                if oNearH and oInsideH and oCtrlH and (na(s.opLastBar) or pivBar > s.opLastBar):
                    self.register_opp(s, ph, pTime, pivBar, pEff, pSpr)

        # Cruces del rango
        if s.phase == PHASE_B and not na(s.rangeHigh) and not na(s.rangeLow):
            tvHt = max(s.rangeHigh - s.rangeLow, tick)
            loZoneP = newPL and pl <= s.rangeLow + tvHt * phaseBZoneFrac
            hiZoneP = newPH and ph >= s.rangeHigh - tvHt * phaseBZoneFrac
            zoneNow = (-1 if loZoneP else 1) if loZoneP != hiZoneP else 0
            if zoneNow != 0 and (na(s.lastZoneBar) or pivBar > s.lastZoneBar):
                if s.lastZone != 0 and zoneNow != s.lastZone:
                    s.travCount += 1
                    s.lastActBar = i
                s.lastZone = zoneNow
                s.lastZoneBar = pivBar

        # Test del Spring / UTAD
        if newPL and s.phase == PHASE_C and s.outcome == DIR_ACCUM and s.exc and not s.excTested and \
                pivBar > s.excBar and not na(s.rangeLow):
            xNearL = pl <= s.rangeLow + pA * testTolATR
            xKeepL = pl >= s.excPrice - pA * testExtremeToleranceATR
            xScL = int(pEff <= s.excEff * testMaxVolRatio) + int(pSpr <= s.excSpr * testMaxSpreadRatio) + int(pCPos >= 0.50)
            if xNearL and xKeepL and xScL >= testMinScore:
                s.excTested = True
                s.cReadyBar = i
                s.testTime, s.testPrice, s.testScore = pTime, pl, xScL
                s.cfTest = True
                self.set_event(s, EV_TEST)
                sig = add_bit(sig, BIT_TEST)
        if newPH and s.phase == PHASE_C and s.outcome == DIR_DIST and s.exc and not s.excTested and \
                pivBar > s.excBar and not na(s.rangeHigh):
            xNearH = ph >= s.rangeHigh - pA * testTolATR
            xKeepH = ph <= s.excPrice + pA * testExtremeToleranceATR
            xScH = int(pEff <= s.excEff * testMaxVolRatio) + int(pSpr <= s.excSpr * testMaxSpreadRatio) + int(pCPos <= 0.50)
            if xNearH and xKeepH and xScH >= testMinScore:
                s.excTested = True
                s.cReadyBar = i
                s.testTime, s.testPrice, s.testScore = pTime, ph, xScH
                s.cfTest = True
                self.set_event(s, EV_TEST)
                sig = add_bit(sig, BIT_TEST)

        # LPS / LPSY
        if newPL and s.phase == PHASE_D and s.outcome == DIR_ACCUM and not na(s.strBar) and pivBar > s.strBar \
                and not na(s.rangeHigh):
            lHard = self.hard_level(s, DIR_ACCUM)
            lSide = not na(lHard) and pl > lHard - pA * testExtremeToleranceATR
            lCreek = pl >= s.rangeHigh - pA * lpsBoundaryATR
            lSc = int(pEff <= pAvgEff * lpsVolMult) + int(pSpr <= pA * lpsSpreadATR) + int(pCPos >= 0.45)
            if lSide and lCreek and lSc >= lpsMinScore:
                s.lpsTime, s.lpsPrice, s.lpsBar, s.lpsScore = pTime, pl, i, lSc
                s.cfLPS = True
                self.set_event(s, EV_LPS)
                sig = add_bit(sig, BIT_LPS)
        if newPH and s.phase == PHASE_D and s.outcome == DIR_DIST and not na(s.strBar) and pivBar > s.strBar \
                and not na(s.rangeLow):
            yHard = self.hard_level(s, DIR_DIST)
            ySide = not na(yHard) and ph < yHard + pA * testExtremeToleranceATR
            yIce = ph <= s.rangeLow + pA * lpsBoundaryATR
            ySc = int(pEff <= pAvgEff * lpsVolMult) + int(pSpr <= pA * lpsSpreadATR) + int(pCPos <= 0.55)
            if ySide and yIce and ySc >= lpsMinScore:
                s.lpsTime, s.lpsPrice, s.lpsBar, s.lpsScore = pTime, ph, i, ySc
                s.cfLPS = True
                self.set_event(s, EV_LPSY)
                sig = add_bit(sig, BIT_LPSY)

        p2 = int(nz(s.pivLen, liveLen))
        rk2 = not na(s.rangeHigh) and not na(s.rangeLow)
        rH2, rL2 = s.rangeHigh, s.rangeLow
        recLimit2 = exc_recovery_limit(p2)
        mature2 = self.mature_ws(s, p2, a)
        bAge2 = i - s.bStartBar if not na(s.bStartBar) else 0

        # Spring, UTAD
        probeB = s.phase == PHASE_B and s.stCount >= 1 and bAge2 >= max(3, p2 * 2)
        probeCLo = s.phase == PHASE_C and s.outcome == DIR_ACCUM and not s.exc
        probeCHi = s.phase == PHASE_C and s.outcome == DIR_DIST and not s.exc
        if rk2 and not s.pend and (na(s.pendCoolBar) or i > s.pendCoolBar):
            penLo, penHi = rL2 - l, h - rH2
            okLo = (probeB or probeCLo) and a * springMinPenATR <= penLo <= a * springMaxPenATR
            okHi = (probeB or probeCHi) and a * springMinPenATR <= penHi <= a * springMaxPenATR
            if okLo != okHi:
                s.pend = True
                s.pendEdge = DIR_ACCUM if okLo else DIR_DIST
                s.pendStartBar = s.pendExtBar = i
                s.pendExt = l if okLo else h
                s.pendExtTime = self.t
                s.pendEff, s.pendSpr = eff, spr
        if s.pend and rk2 and s.phase in (PHASE_B, PHASE_C):
            if s.pendEdge == DIR_ACCUM and l < s.pendExt:
                s.pendExt, s.pendExtBar, s.pendExtTime = l, i, self.t
                s.pendEff, s.pendSpr = max(nz(s.pendEff), eff), max(nz(s.pendSpr), spr)
            elif s.pendEdge == DIR_DIST and h > s.pendExt:
                s.pendExt, s.pendExtBar, s.pendExtTime = h, i, self.t
                s.pendEff, s.pendSpr = max(nz(s.pendEff), eff), max(nz(s.pendSpr), spr)
            recAge = i - s.pendStartBar
            recovered = c > rL2 if s.pendEdge == DIR_ACCUM else c < rH2
            depth = rL2 - s.pendExt if s.pendEdge == DIR_ACCUM else s.pendExt - rH2
            depthOK = a * springMinPenATR <= depth <= a * springMaxPenATR
            if s.pendEdge == DIR_ACCUM:
                recSc = int(cPos >= springCloseMin) + int(eff <= avgEff * springEffortMaxMult) + int(c > o) + int(spr >= a * 0.50)
            else:
                recSc = int(cPos <= utadCloseMax) + int(eff <= avgEff * springEffortMaxMult) + int(c < o) + int(spr >= a * 0.50)
            if recovered and depthOK and recAge <= recLimit2 and recSc >= excursionMinScore:
                probeEdge = s.pendEdge
                if s.phase == PHASE_B and not mature2:
                    s.provEdge = probeEdge
                    s.provBar, s.provPrice, s.provTime = s.pendExtBar, s.pendExt, s.pendExtTime
                    s.provEff, s.provSpr, s.provScore = s.pendEff, s.pendSpr, recSc
                    s.lastActBar = i
                    s.pend = False
                else:
                    if s.phase == PHASE_C:
                        self.clear_terminal(s)
                    self.exc_from_pend(s, recSc)
                    sig = add_bit(sig, BIT_SPRING if probeEdge == DIR_ACCUM else BIT_UTAD)
            elif recAge > recLimit2 or (not depthOK and depth > a * springMaxPenATR):
                if s.phase == PHASE_B:
                    s.lastBadBar = i
                s.pend = False
                s.pendCoolBar = i + p2

        if s.phase == PHASE_C and s.exc and not s.excTested and rk2:
            if s.outcome == DIR_ACCUM and l < s.excPrice and l >= rL2 - a * springMaxPenATR:
                s.excPrice, s.excBar, s.excTime = l, i, self.t
                s.excEff, s.excSpr = max(nz(s.excEff), eff), max(nz(s.excSpr), spr)
            elif s.outcome == DIR_DIST and h > s.excPrice and h <= rH2 + a * springMaxPenATR:
                s.excPrice, s.excBar, s.excTime = h, i, self.t
                s.excEff, s.excSpr = max(nz(s.excEff), eff), max(nz(s.excSpr), spr)

        # SOS / SOW → Fase D
        if s.phase == PHASE_C and rk2 and not s.pend:
            cMinD = phaseCtoD_min(p2)
            cFormal = (not s.exc) or s.excTested
            cAnchor = s.cReadyBar if not na(s.cReadyBar) else s.excBar
            cAgeReady = not na(cAnchor) and i - cAnchor >= cMinD
            cVal = self.validation_ws(s, p2, trendScore, a)
            cValD = cVal if cFormal else min(100, cVal + 13)
            cConf = self.conf_ws(s)
            cConfD = cConf if cFormal else min(100, cConf + 30)
            cHt = max(rH2 - rL2, tick)
            if cAgeReady and cValD >= phaseDValidationMin and cConfD >= minConfidencePhaseC:
                if s.outcome == DIR_ACCUM and ageOKBull:
                    domUp = rL2 + cHt * phaseDDominanceFrac
                    brkUp = c > rH2 + a * breakATR
                    domOKUp = brkUp or ((c >= domUp and close1 >= domUp) if cFormal
                                        else (c >= domUp and close1 >= domUp and close2 >= domUp))
                    reqUp = strengthMinScore if cFormal else max(2, strengthMinScore - 1)
                    qUp = sosScore >= reqUp or mbSOS or ((not cFormal) and res3Up >= 1.0)
                    if domOKUp and qUp:
                        self.promote_d(s, DIR_ACCUM, sosScore, h)
                        sig = add_bit(sig, BIT_SOS)
                elif s.outcome == DIR_DIST and ageOKBear:
                    domDn = rH2 - cHt * phaseDDominanceFrac
                    brkDn = c < rL2 - a * breakATR
                    domOKDn = brkDn or ((c <= domDn and close1 <= domDn) if cFormal
                                        else (c <= domDn and close1 <= domDn and close2 <= domDn))
                    reqDn = strengthMinScore if cFormal else max(2, strengthMinScore - 1)
                    qDn = sowScore >= reqDn or mbSOW or ((not cFormal) and res3Dn >= 1.0)
                    if domOKDn and qDn:
                        self.promote_d(s, DIR_DIST, sowScore, l)
                        sig = add_bit(sig, BIT_SOW)

        # Salida del rango en Fase B
        resetLate = -1
        if s.phase == PHASE_B and rk2 and not s.pend:
            dHt = max(rH2 - rL2, tick)
            dBuf = max(dHt * phaseBDepartureTRFrac, a * phaseBDepartureATR)
            upAcc = c > rH2 + a * breakATR and close1 > rH2 + a * breakATR
            dnAcc = c < rL2 - a * breakATR and close1 < rL2 - a * breakATR
            upSus = loCDep > rH2 + dBuf
            dnSus = hiCDep < rL2 - dBuf
            upGo = (upAcc and (sosScore >= strengthMinScore or mbSOS)) or upSus
            dnGo = (dnAcc and (sowScore >= strengthMinScore or mbSOW)) or dnSus
            provLimit = phase_idle_limit(PHASE_C, True, p2)
            if upGo and not dnGo:
                upProv = s.provEdge == DIR_ACCUM and not na(s.provBar) and i - s.provBar <= provLimit
                if ageOKBull and upProv and mature2:
                    self.adopt_prov(s)
                    self.promote_d(s, DIR_ACCUM, sosScore, h)
                    sig = add_bit(add_bit(sig, BIT_SPRING), BIT_SOS)
                elif upSus and not upProv:
                    resetLate = RS_DEPART
            elif dnGo and not upGo:
                dnProv = s.provEdge == DIR_DIST and not na(s.provBar) and i - s.provBar <= provLimit
                if ageOKBear and dnProv and mature2:
                    self.adopt_prov(s)
                    self.promote_d(s, DIR_DIST, sowScore, l)
                    sig = add_bit(add_bit(sig, BIT_UTAD), BIT_SOW)
                elif dnSus and not dnProv:
                    resetLate = RS_DEPART
        if resetLate >= 0:
            self.reset(resetLate)
            s = self.s

        # Nuevo SOS/SOW tras el LPS
        if s.phase == PHASE_D and s.outcome == DIR_ACCUM and s.ev == EV_LPS and not na(s.rangeHigh):
            if c > s.rangeHigh + a * breakATR and relE >= strengthVolMult and cPos >= sosCloseMin:
                s.strBar, s.strTime, s.strPrice, s.strScore = i, self.t, h, sosScore
                self.set_event(s, EV_SOS)
                sig = add_bit(sig, BIT_SOS)
        if s.phase == PHASE_D and s.outcome == DIR_DIST and s.ev == EV_LPSY and not na(s.rangeLow):
            if c < s.rangeLow - a * breakATR and relE >= strengthVolMult and cPos <= sowCloseMax:
                s.strBar, s.strTime, s.strPrice, s.strScore = i, self.t, l, sowScore
                self.set_event(s, EV_SOW)
                sig = add_bit(sig, BIT_SOW)

        # Aceptación → Fase E
        if s.phase == PHASE_D and not na(s.rangeHigh) and not na(s.rangeLow):
            eConf = self.conf_ws(s)
            dAge = i - s.dStartBar if not na(s.dStartBar) else 0
            dMin = phaseD_min(int(nz(s.pivLen, liveLen)))
            if s.outcome == DIR_ACCUM:
                s.outCount = s.outCount + 1 if c > s.rangeHigh else 0
                eOppOK = self.regime != REGIME_MARKDOWN or s.cfLPS
                eStd = s.cfLPS and not na(s.lpsBar) and i - s.lpsBar >= confirmBars and dAge >= dMin and \
                    loCConf > s.rangeHigh and eConf >= minConfidencePhaseD
                eDir = (not s.cfLPS) and dAge >= max(dMin, directAcceptanceBars) and loCDir > s.rangeHigh and \
                    eConf >= directAcceptanceConfidence
                if (eStd or eDir) and eOppOK:
                    s.cfAccept = True
                    s.phase = PHASE_E
                    s.eStartBar, s.eStartTime = i, self.t
                    self.regime, self.regimeBar = REGIME_MARKUP, i
                    self.set_event(s, EV_MARKUP)
                    sig = add_bit(sig, BIT_E)
            elif s.outcome == DIR_DIST:
                s.outCount = s.outCount + 1 if c < s.rangeLow else 0
                eOppOK = self.regime != REGIME_MARKUP or s.cfLPS
                eStd = s.cfLPS and not na(s.lpsBar) and i - s.lpsBar >= confirmBars and dAge >= dMin and \
                    hiCConf < s.rangeLow and eConf >= minConfidencePhaseD
                eDir = (not s.cfLPS) and dAge >= max(dMin, directAcceptanceBars) and hiCDir < s.rangeLow and \
                    eConf >= directAcceptanceConfidence
                if (eStd or eDir) and eOppOK:
                    s.cfAccept = True
                    s.phase = PHASE_E
                    s.eStartBar, s.eStartTime = i, self.t
                    self.regime, self.regimeBar = REGIME_MARKDOWN, i
                    self.set_event(s, EV_MARKDOWN)
                    sig = add_bit(sig, BIT_E)

        # ═══ Entrada (una por campaña) ═══
        entry_now = False
        if na(s.entryTime) and s.outcome != DIR_NONE:
            eMinConf, eMinTests = ENTRY_CFG[self.strict]
            eValFloor = phaseDValidationMin if self.strict == "Conservador" else max(45, phaseDValidationMin - 10)
            eVal = self.validation_ws(s, int(nz(s.pivLen, liveLen)), trendScore, a)
            eReady = self.entry_readiness(s, eMinConf, eValFloor, a)
            eQuality = self.conf_ws(s) >= eMinConf and eVal >= eValFloor and eReady >= eMinTests
            testBit = has_bit(sig, BIT_TEST) or has_bit(sig, BIT_CTEST)
            strengthBit = has_bit(sig, BIT_SOS) or has_bit(sig, BIT_SOW)
            lpsBit = has_bit(sig, BIT_LPS) or has_bit(sig, BIT_LPSY)
            st = self.strict
            aggTest = st == "Agresivo" and testBit and eQuality
            aggStr = st == "Agresivo" and strengthBit and eQuality and na(s.testTime)
            stdTest = st == "Estándar" and testBit and eQuality and s.testScore >= testMinScore
            stdLps = st == "Estándar" and lpsBit and eQuality
            conLps = st == "Conservador" and lpsBit and eQuality
            if aggTest or aggStr or stdTest or stdLps or conLps:
                s.entryTime, s.entryPrice = self.t, c
                s.entryKind = ENTRY_LPS if (stdLps or conLps) else (ENTRY_STRENGTH if aggStr else ENTRY_TEST)
                entry_now = True

        # recorte de memoria
        if i % 100 == 0:
            for n in self.names:
                getattr(self, "_" + n).trim(self.keep)

        oP = int(nz(s.pivLen, liveLen))
        self.last = {
            "bar": i, "time": self.t, "close": c, "atr": a, "sig": sig, "trend": trendScore,
            "phase": s.phase, "stop": s.stopSide, "outcome": s.outcome, "ev": s.ev,
            "type": self.type_ws(s),
            "wdir": s.outcome if s.outcome != DIR_NONE else self.bias_dir(s),
            "conf": 0 if s.stopSide == DIR_NONE else self.conf_ws(s),
            "val": self.validation_ws(s, oP, trendScore, a),
            "rh": s.rangeHigh, "rl": s.rangeLow, "exc": s.exc, "excTested": s.excTested,
            "excP": s.excPrice, "testP": s.testPrice,
            "entry_now": entry_now, "entryKind": s.entryKind, "entryPrice": s.entryPrice, "entryTime": s.entryTime,
            "range_atr": self.range_atr(s, a), "b_bars": (i - s.bStartBar) if not na(s.bStartBar) else 0,
            "excT": s.excTime, "testT": s.testTime, "fail": fail,
        }
        return self.last

    def status_text(self):
        d = self.last
        if not d or d["phase"] == PHASE_NONE:
            return "buscando SC/BC"
        side = "LONG" if d["wdir"] == DIR_ACCUM else "SHORT" if d["wdir"] == DIR_DIST else "-"
        return (f"{TYPE_NAMES.get(d['type'], '-')} fase {PHASE_NAMES[d['phase']]} · último {EV_NAMES.get(d['ev'], '-')}"
                f" · sesgo {side} · madurez {d['conf']} · validación {d['val']}")
