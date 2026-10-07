# Estrategia Wyckoff v4.3 — reglas completas

## 1. Base (ya implementada, motor 1:1 del indicador)
Estructura SC/BC → AR → ST → Fase B (causa) → Spring/UTAD o test terminal (C) → SOS/SOW (D) → LPS/LPSY → E.
Entrada a mercado al cierre de la vela que valida el motor (exigencia Estándar). Una posición por símbolo.

## 2. Filtros (cada uno: aviso = solo se registra · bloquea = filtra)
| Filtro | Qué evita | Estado |
|---|---|---|
| R:R ≥ 1.5, stop 0.30–6% | operaciones donde el coste se come la R | ACTIVO |
| `CHASE_MAX_R` 0.3 | perseguir una entrada que ya avanzó | ACTIVO |
| EMA 1h, contexto 4h, BTC, amplitud | operar contra el mercado mayor | aviso |
| **ADX ≤ `ADX_MAX`** (nuevo) | reversión contra una tendencia fuerte | aviso |
| **RSI agotado desde el clímax ≥ `DIV_MIN`** (nuevo) | entrar con el impulso previo aún vivo | aviso |
| **AVWAP anclado al clímax** (nuevo) | operar contra quien controla desde el clímax | aviso |

## 3. Salidas
SL detrás del Spring/UTAD (+0.25 ATR) · TP1 = borde opuesto del rango (50%) · SL a breakeven · TP2 = altura del rango.
Con posición de 10 USDT no se puede partir: sale entera en TP1 (`SINGLE_TP`). Variantes a medir: `TP2_MULT`, `TRAIL_ATR`, `TIME_STOP_BARS`.

## 4. Riesgo
Posición fija `NOTIONAL_USDT` o `RISK_PCT`; x3 aislado; 1 posición; pausa por `MAX_DAILY_LOSS_R`, `MAX_DD_PCT`, `MAX_LOSS_STREAK`; cierre manual `/cerrar`, `/cerrartodo SI`.

## 5. Cómo se decide qué activar (en este orden, nunca todo a la vez)
1. `python backtest.py --symbols <tus pares> --tf 15m --days 365` → mira los desgloses nuevos (ADX, RSI, AVWAP).
2. `python sweep.py --modo indicadores --symbols <tus pares> --tf 15m --days 365` (18 variantes, Bonferroni).
3. Un complemento pasa a `bloquea` SOLO si en la columna PRUEBA mejora la media R y t supera el umbral de Bonferroni.
4. Después `--modo salidas`. Después en VST 2–4 semanas con 30+ operaciones. Después dinero real.
5. Si ninguno pasa, se queda todo en `aviso`: no hay nada que "mejorar" con esos datos.
