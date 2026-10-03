# Wyckoff Bot v5.2 (BingX · Railway · Telegram)

Ejecuta en BingX las entradas del indicador **Wyckoff ES [theUltimator5]**. El motor (`wyckoff_engine.py`) es una traducción 1:1 de `f_engine()` del Pine: mismas constantes, fases A→E, resets y lógica de entrada (una por campaña según exigencia).

## Evidencia que fija la configuración por defecto (2 oct 2026)
Investigación con datos reales de BingX: 30 símbolos · 1h · 365 días · coste 0.08 %/lado.

| | Operaciones | Media | PF | t |
|---|---|---|---|---|
| Indicador tal cual | 53 | −0.06R | 0.91 | −0.27 |
| A favor de la EMA50 1h | 23 | **+0.42R** | 1.83 | +1.10 |
| En contra de la EMA50 1h | 30 | −0.43R | 0.44 | −1.90 |
| Trampa (estructura rota) | 45 | +0.00R | 1.01 | +0.02 |
| 15m (solo 96 días en BingX) | 21 | −0.47R | 0.49 | −1.33 |

Decisiones de la v5: **1h**, **TREND_FILTER=bloquea**, trampa **off**, meta-modelo **off** (no superó la prueba de azar).
El filtro EMA es la mejor pista, **no** una certeza: salió de mirar ~15 desgloses (alguno destaca por azar) y con 23 operaciones.
Por eso el bot arranca en SIGNAL y cada 20 operaciones manda un **veredicto** (media, t): solo con t≥2 en datos
nuevos tiene sentido pasar a LIVE, y con riesgo mínimo.

## v5.1 — ideas nuevas (todas medibles, todas OFF hasta que pasen la prueba)
La evidencia de arriba dice que el indicador solo, sin filtrar, no tiene ventaja demostrada. Por eso lo nuevo **no se activa por fe**:
cada idea se registra en cada señal (Telegram, `journal.csv`, backtest) y solo se usa para bloquear si el barrido 70/30 con Bonferroni lo avala.

| Idea | Qué mide | Dónde se ve |
|---|---|---|
| `squeeze` | ATR(5)/ATR(50): compresión de volatilidad antes de la entrada (energía acumulada en Fase B) | desglose backtest, diario, Telegram |
| `er` | Eficiencia de Kaufman 20 velas: serrucho vs tramo direccional agotado | idem |
| `dry` | Volumen 5 velas / media 50: secado (“no hay oferta”) vs actividad | idem |
| `clv`, `body` | Cierre y cuerpo de la vela de entrada con signo del lado: confirmación vs persecución | idem |
| `wick_exc`, `vol_exc` | Mecha de rechazo y volumen de la vela del Spring/UTAD | idem |
| `risk_atr` | Distancia al stop en ATR | idem |
| `session`, `dow` | Sesión UTC (Asia/Europa/EEUU/Noche) y día de la semana | idem |
| **Acelerador de capital** | Si las últimas `THROTTLE_N` operaciones suman < 0 R, el riesgo se multiplica por `THROTTLE_MULT` | línea “acelerador” del backtest; `risk_mult` en el diario |

Cómo se prueba (en este orden):
1. `python backtest.py ... --days 365` → mira los desgloses `[v5.1]` y la línea del acelerador (DD con y sin).
2. `python sweep.py --modo edge --symbols ... --tf 1h --days 365` → 25 reglas, una a una; solo cuenta la columna **PRUEBA** y la ✓ exige ≥15 operaciones en prueba y Bonferroni.
3. Si alguna pasa: `EDGE_FILTER=bloquea` y `EDGE_RULES=squeeze<0.85` (varias con coma = AND) en **SIGNAL** unas semanas y compara con el veredicto del bot.
4. El acelerador es gestión de riesgo (no alfa): `THROTTLE_N=5`, `THROTTLE_MULT=0.5` recorta el tamaño en rachas malas. Mide su efecto en el DD del backtest antes de activarlo.
`meta.py` ya incluye `squeeze, er, dry, clv, wick_exc` como features: un modelo antiguo sigue funcionando (guarda sus propias features).

## v5.2 — más ideas medibles + estadística honesta
Seis features nuevas (misma regla: se registran siempre, solo bloquean si `sweep.py --modo edge` las avala; **sin variables nuevas**):

| Idea | Qué mide |
|---|---|
| `vr` | Razón de varianzas de Lo-MacKinlay (120 retornos, q=4): <1 reversión (rango real), >1 tendencia. Misma familia que tu z* de Wavelet Confirm |
| `atr_pct` | Percentil de la volatilidad actual frente a las últimas 250 velas |
| `depth` | Profundidad del Spring/UTAD más allá del borde del rango, en ATR |
| `confirm` | Velas entre el Spring/UTAD y la entrada (rapidez de la recuperación) |
| `avwap` | (VWAP anclado al clímax − entrada)/ATR con signo del lado: entrar con descuento o con recargo sobre el coste medio de la campaña |
| `fund_h` | Horas hasta el próximo funding (00/08/16 UTC) |

Herramientas de rigor (en el informe del backtest):
- **IC 95 % de la media por bootstrap** y P(media>0): si el intervalo cruza 0, no se descarta la suerte.
- **Drawdown Monte Carlo** por nivel de riesgo (mediana / peor 5 % / P(DD≥25 %)): sirve para elegir `RISK_PCT` con datos, no a ojo.
- El motor ahora expone `clxT/clxP` (tiempo y precio del clímax) en su estado: solo lectura, la detección no cambia (`test_engine.py` da el mismo resultado).
- `sweep.py --modo edge` pasa de 25 a 37 reglas y la corrección de Bonferroni lo tiene en cuenta (más reglas = umbral más exigente).

## Archivos

| Archivo | Qué hace |
|---|---|
| `wyckoff_engine.py` | Motor Wyckoff vela a vela (pivotes con la regla de empates de TradingView) |
| `strategy.py` | Plan SL/TP1/TP2, filtros, contexto de TF superior, simulación de la gestión, selección de operaciones |
| `main.py` | Bucle multi-TF: velas cerradas → motor → señal → SIGNAL (virtual) o LIVE (órdenes) |
| `bingx.py` | Cliente BingX swap v2 (firma sobre el string exacto enviado, Hedge/One-Way, SL adjunto) |
| `notify.py` | Telegram (avisos + comandos) y diario `journal.csv` |
| `universe.py` | Clasifica cada perpetuo: cripto, forex, materia prima, acción, índice |
| `config.py` | Variables de entorno (quita comillas) |
| `backtest.py` | Backtest con el mismo motor/filtros/gestión, coste incluido, partición 70/30 y desgloses |
| `meta.py` | Meta-etiquetado: un 2º modelo aprende qué señales del indicador tomar (walk-forward, purga, prueba de azar) |
| `research.py` | Servicio de investigación en Railway (`RUN_MODE=research`): backtest + sweep + meta con datos reales, resultados a Telegram |
| `sweep.py` | Barrido de variantes: elige en el 70% inicial, enseña el 30% final, corrige por nº de pruebas |
| `edge.py` | **v5.1** Laboratorio de ideas nuevas: features (squeeze, ER, secado, cierre, mecha, sesión…), reglas `EDGE_RULES` y acelerador de capital |
| `test_engine.py` | Prueba sin red con ciclos sintéticos |
| `railway.env.txt` | Plantilla para el Raw Editor de Railway |

## Gestión de cada operación
- Entrada a mercado al cierre de la vela que valida el motor (no persigue si ya avanzó `CHASE_MAX_R`).
- **SL dentro de la orden de entrada** (`ATTACH_SL`); si BingX no lo acepta, se pone aparte al instante.
- **Guardián del stop**: cada `MANAGE_EVERY_S` comprueba que la posición tiene stop y lo repone si falta.
- TP1 = borde opuesto del rango (`TP1_FRACTION`), TP2 = proyección de la altura. Tras TP1 el SL se sustituye por uno a breakeven con la cantidad restante.
- Tamaño por riesgo (`RISK_PCT`), margen aislado, topes por bot, por cuenta y por pérdida diaria.
- Al arrancar cancela órdenes huérfanas `wyk*` de despliegues anteriores.

## Universo: todos los perpetuos de BingX
`UNIVERSE=all` analiza **todos** los perpetuos USDT de BingX, cripto y TradFi (BingX los lista en el mismo mercado, p. ej. `NCFXEUR2USD-USDT`).
- `CATEGORIES` elige clases: `crypto,forex,commodity,stock,index,tradfi`.
- `MIN_QUOTE_VOL` (cripto) y `MIN_QUOTE_VOL_TRADFI` quitan lo que no tiene liquidez para entrar y salir.
- Velas con mercado cerrado (precio plano y volumen 0) no alimentan al motor.
- TradFi: no abre los viernes desde `TRADFI_NO_ENTRY_FRI_UTC` y avisa de las abiertas antes del fin de semana (el SL no se ejecuta con el mercado cerrado).
- `TRADFI_EFFORT=rango` usa el rango de la vela como esfuerzo en vez del volumen de BingX.
- `BINGX_MAX_RPS` limita peticiones/segundo; con cientos de símbolos la vuelta tarda decenas de segundos.
- Backtest de TradFi: `python backtest.py --symbols NCFXEUR2USD-USDT,NCCOGOLD2USD-USDT --tf 1h --days 180` (datos de BingX).

## Ideas nuevas para probar (v3) — todas medibles, ninguna activada a ciegas
| Idea | Variable | Por defecto | Cómo se mide |
|---|---|---|---|
| TP2 más lejos (altura × N) | `TP2_MULT` | 1.0 (indicador) | `sweep.py --modo salidas` |
| Trailing tras TP1 (cierre − N×ATR) | `TRAIL_ATR` | 0 = off (empeoró en el sweep) | `sweep.py --modo salidas` |
| Salida por tiempo si no llega a TP1 | `TIME_STOP_BARS` | 0 = off | `sweep.py --modo salidas` |
| Estructura de BTC a favor/en contra | `BTC_FILTER` | aviso | desglose "por BTC" del backtest |
| Funding en la señal (lado amontonado) | — | registro | columna `funding` del diario |
| Tope de posiciones en la misma dirección | `MAX_SAME_SIDE` | 0 = sin tope | gestión de riesgo |
| ¿Predice algo la validación del indicador? | — | — | desglose por validación, R:R, altura del rango y duración de Fase B |

La gestión LIVE (trailing, tiempo, TP1→BE) se ha comprobado contra un mini-exchange que ejecuta las órdenes con el high/low de cada vela: da exactamente el mismo resultado que la simulación del backtest.

## Ideas v4 — lo que casi nadie hace con Wyckoff (todo medible, nada activado a ciegas)
| Idea | Variable | Por defecto | Dónde se mide |
|---|---|---|---|
| **Trampa**: cuando una estructura en Fase C/D se rompe por el nivel duro, operar en contra (los del Spring/UTAD quedan atrapados con el stop ahí) | `FAIL_TRADES` off/aviso/on, `FAIL_SL_ATR` | aviso | sección TRAMPA del backtest |
| **Flujo agresor en el Spring**: compra/venta agresora (taker) de Binance en la vela del Spring/UTAD y en las 10 últimas. ¿Spring con venta absorbida o con compra a favor? | `FLOW_SOURCE` | binance | desgloses "flujo agresor" |
| **Amplitud Wyckoff**: cuántos símbolos del universo están a la vez en acumulación/distribución avanzada | `BREADTH_FILTER` | aviso | desglose "amplitud Wyckoff" |
| **Meta-etiquetado**: el indicador da la dirección; un modelo entrenado decide qué señales tomar | `META_FILTER`, `meta_model.json` | aviso | `python meta.py` |

Binance bloquea IPs de EE. UU. (HTTP 451): para tener flujo agresor en vivo, pon el servicio de Railway en una región de Europa.

## Contexto de TF superior (`CONTEXT_TF`)
Un segundo motor Wyckoff corre en 4h. Cada señal sale marcada **a favor / en contra / neutral** según la estructura mayor, y va al diario y al backtest. `CONTEXT_FILTER=aviso` por defecto: se mide antes de usarlo para filtrar.

## Comandos de Telegram
`/estado` `/posiciones` `/stats` `/pausa` `/reanudar` (solo desde `TELEGRAM_CHAT_ID`).

## Despliegue
1. Sube **todos** los archivos juntos al repo.
2. Railway → Variables → Raw Editor → pega `railway.env.txt`.
3. Volumen montado en `/data`.
4. Arranca en `MODE=SIGNAL`. Para operar: `MODE=LIVE` **y** `CONFIRM_LIVE=SI`.

## Investigación en Railway (sin ordenador)
1. En el mismo proyecto: **New → GitHub Repo → el mismo repo del bot** (servicio nuevo, p. ej. `wyckoff-research`).
2. Variables → Raw Editor → pega `railway.research.env.txt` (con tu token y chat de Telegram). Sin claves de BingX: no opera.
3. Settings → región **Europa** (en EE. UU. Binance devuelve 451 y no hay flujo agresor; funciona igual con datos de BingX).
4. Deploy. En 10-30 min llega a Telegram el resumen + el informe completo (+ `meta_model.json` solo si supera la prueba de azar).
5. Al terminar queda en reposo. Para repetir con otros parámetros: cambia variables y Redeploy. Cuando acabes, borra el servicio.

## Antes de LIVE
```
pip install requests
python test_engine.py
python backtest.py --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT --tf 15m --days 365
python sweep.py --modo entradas --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT --tf 15m --days 365
python sweep.py --modo salidas  --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT --tf 15m --days 365
python meta.py --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT,AVAXUSDT,DOTUSDT --tf 15m --days 365 --incluir-trampas
```
Manda la columna de **prueba** del sweep y la **prueba de azar** de meta.py. `meta.py --guardar` solo si la prueba de azar da <5%.
