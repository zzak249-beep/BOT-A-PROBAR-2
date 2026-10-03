#!/usr/bin/env python3
"""
combine_portfolio.py
=====================
Combina 2 o 3 sistemas de trading a partir de la exportación REAL de
TradingView (pestaña "Lista de operaciones" / "List of trades" del
Strategy Tester -> botón de exportar a CSV). No inventa ni estima nada:
si no le das un CSV real, no produce números.

USO
---
    python combine_portfolio.py DEF.csv TREND.csv REBOTE.csv --out informe

Puedes pasar 2 o 3 archivos (o más). Cada archivo es la exportación de
UN script/símbolo. Si probaste el mismo script en varias monedas, exporta
cada backtest por separado y pásalos todos (el nombre del archivo se usa
como etiqueta, así que llámalos "DEF_ETH.csv", "DEF_SOL.csv", etc.).

QUÉ HACE
--------
1. Lee cada CSV (acepta cabeceras en español o en inglés).
2. Se queda con las operaciones CERRADAS (filas de salida) y su fecha
   y ganancia/pérdida en moneda.
3. Agrupa el P&L por día para cada sistema.
4. Calcula, por sistema y para la SUMA de todos (la cartera combinada):
   - nº de operaciones, % de acierto, factor de beneficio (PF)
   - drawdown máximo (de la curva de equity)
   - "peor semana": la peor ventana de 7 días natural, para saber a
     qué racha de verdad te enfrentas usando todos los sistemas a la vez
5. Calcula la correlación entre los rendimientos DIARIOS de cada pareja
   de sistemas. Una correlación baja o negativa es la señal real de que
   "no fallan a la vez" — no basta con afirmarlo, hay que medirlo.
6. Guarda un PNG con las curvas de equity (individuales y combinada) y
   un CSV con el resumen numérico.

Lo que NO hace: no ejecuta ningún backtest, no rellena huecos con
suposiciones, no "arregla" un CSV mal exportado — si faltan columnas,
avisa y se detiene, en vez de inventar una cifra.
"""

import argparse
import sys
from pathlib import Path

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ───────────────────────── lectura flexible del CSV ─────────────────────────

DATE_KEYS = ["date/time", "fecha/hora", "fecha", "date"]
TYPE_KEYS = ["type", "tipo"]
PROFIT_KEYS = ["profit", "ganancia y pérdida", "ganancia y perdida", "p&l", "p & l"]
EXIT_WORDS = ["exit", "salida", "close"]


def _find_col(columns, keys, exclude_pct=False):
    cols = list(columns)
    lower = {c: c.lower() for c in cols}
    best = None
    for c in cols:
        lc = lower[c]
        if exclude_pct and "%" in lc:
            continue
        if any(k in lc for k in keys):
            if best is None or len(lc) < len(lower[best]):
                best = c
    return best


def load_tv_trades(path: Path) -> pd.DataFrame:
    """Lee un CSV de 'Lista de operaciones' de TradingView y devuelve
    una tabla con columnas: fecha, pnl (una fila por operación cerrada)."""
    df = pd.read_csv(path)
    if df.empty:
        raise ValueError(f"{path.name}: el CSV está vacío.")

    date_col = _find_col(df.columns, DATE_KEYS)
    type_col = _find_col(df.columns, TYPE_KEYS)
    profit_col = _find_col(df.columns, PROFIT_KEYS, exclude_pct=True)

    missing = [n for n, v in [("fecha", date_col), ("tipo", type_col), ("ganancia/pérdida", profit_col)] if v is None]
    if missing:
        raise ValueError(
            f"{path.name}: no encuentro columna(s) de {', '.join(missing)}. "
            f"Columnas disponibles: {list(df.columns)}"
        )

    df = df.copy()
    df["_date"] = pd.to_datetime(df[date_col], errors="coerce", dayfirst=False)
    if df["_date"].isna().all():
        df["_date"] = pd.to_datetime(df[date_col], errors="coerce", dayfirst=True)

    # Solo filas de salida (donde TradingView escribe el P&L de la operación)
    is_exit = df[type_col].astype(str).str.lower().apply(lambda s: any(w in s for w in EXIT_WORDS))
    df = df[is_exit].copy()
    df["_pnl"] = pd.to_numeric(df[profit_col], errors="coerce")
    df = df.dropna(subset=["_date", "_pnl"])

    if df.empty:
        raise ValueError(
            f"{path.name}: no quedan operaciones cerradas tras el filtrado. "
            f"Revisa que sea la 'Lista de operaciones' completa, no un resumen."
        )

    return df[["_date", "_pnl"]].rename(columns={"_date": "date", "_pnl": "pnl"}).sort_values("date")


# ───────────────────────── métricas ─────────────────────────

def trade_stats(trades: pd.DataFrame) -> dict:
    n = len(trades)
    wins = trades[trades["pnl"] > 0]["pnl"]
    losses = trades[trades["pnl"] <= 0]["pnl"]
    gross_profit = wins.sum()
    gross_loss = -losses.sum()
    pf = gross_profit / gross_loss if gross_loss > 0 else float("nan")
    winrate = 100 * len(wins) / n if n else float("nan")
    net = trades["pnl"].sum()
    return {"operaciones": n, "acierto_%": round(winrate, 1), "PF": round(pf, 2), "neto": round(net, 2)}


def equity_curve(daily_pnl: pd.Series) -> pd.Series:
    return daily_pnl.cumsum()


def max_drawdown(equity: pd.Series) -> float:
    running_max = equity.cummax()
    dd = equity - running_max
    return round(dd.min(), 2)


def worst_week(daily_pnl: pd.Series) -> float:
    if daily_pnl.empty:
        return float("nan")
    full_idx = pd.date_range(daily_pnl.index.min(), daily_pnl.index.max(), freq="D")
    s = daily_pnl.reindex(full_idx, fill_value=0.0)
    rolling7 = s.rolling(7, min_periods=1).sum()
    return round(rolling7.min(), 2)


def to_daily(trades: pd.DataFrame) -> pd.Series:
    s = trades.groupby(trades["date"].dt.floor("D"))["pnl"].sum()
    s.index.name = "date"
    return s


# ───────────────────────── programa principal ─────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Combina backtests reales exportados de TradingView.")
    ap.add_argument("csvs", nargs="+", type=Path, help="CSV de 'Lista de operaciones' de cada sistema (2 o más).")
    ap.add_argument("--out", type=Path, default=Path("portfolio_report"), help="Prefijo de los ficheros de salida.")
    args = ap.parse_args()

    if len(args.csvs) < 2:
        sys.exit("Necesito al menos 2 CSV para combinar algo (si solo tienes 1, no hay nada que combinar).")

    systems = {}
    for path in args.csvs:
        label = path.stem
        if label in systems:
            label = f"{path.stem}_{path.parent.name}"
        if not path.exists():
            sys.exit(f"ERROR: no encuentro el archivo {path} (revisa la ruta/nombre).")
        try:
            trades = load_tv_trades(path)
        except Exception as e:
            sys.exit(f"ERROR en {path}: {e}")
        systems[label] = trades
        print(f"[{label}] {len(trades)} operaciones cerradas leídas de {path.name}")

    # ── stats individuales ──
    print("\n=== RESULTADOS POR SISTEMA (de tu CSV, no estimados) ===")
    rows = []
    daily = {}
    for label, trades in systems.items():
        st = trade_stats(trades)
        d = to_daily(trades)
        daily[label] = d
        eq = equity_curve(d)
        st["dd_maximo"] = max_drawdown(eq)
        st["peor_semana"] = worst_week(d)
        st["sistema"] = label
        rows.append(st)
        print(f"  {label}: {st}")

    summary = pd.DataFrame(rows).set_index("sistema")

    # ── combinado (misma cuenta, todo a la vez) ──
    all_daily = pd.concat(daily.values(), axis=1, keys=daily.keys(), sort=True).fillna(0.0)
    combined_daily = all_daily.sum(axis=1)
    combined_eq = equity_curve(combined_daily)
    combined_trades_n = sum(len(t) for t in systems.values())
    combined_net = combined_daily.sum()
    combined_dd = max_drawdown(combined_eq)
    combined_worst_week = worst_week(combined_daily)

    print("\n=== CARTERA COMBINADA (los sistemas juntos, misma cuenta) ===")
    print(f"  operaciones totales: {combined_trades_n}")
    print(f"  neto combinado: {round(combined_net, 2)}")
    print(f"  drawdown máximo combinado: {combined_dd}")
    print(f"  peor semana combinada: {combined_worst_week}")

    sum_individual_dd = summary["dd_maximo"].abs().sum()
    print(f"\n  Suma de los DD individuales, en positivo (si NO se solapan nunca): {round(sum_individual_dd, 2)}")
    print(f"  DD real combinado, en positivo (con solapes reales):               {abs(combined_dd)}")
    if abs(combined_dd) < sum_individual_dd:
        print("  → los sistemas SÍ se compensan algo: el drawdown junto es menor que la suma.")
    else:
        print("  → ⚠ el drawdown junto es igual o peor que la suma: puede que estén cayendo a la vez,")
        print("    justo lo contrario de lo que se busca al combinar sistemas.")

    # ── correlación real entre sistemas ──
    print("\n=== CORRELACIÓN DIARIA ENTRE SISTEMAS (medida, no supuesta) ===")
    corr = all_daily.corr()
    print(corr.round(2).to_string())
    print("\n  Cerca de 0 o negativa = se compensan de verdad. Cerca de 1 = suben y bajan juntos:")
    print("  combinarlos no reduce el riesgo tanto como parece por separado.")

    # ── guardar salidas ──
    args.out.parent.mkdir(parents=True, exist_ok=True)
    summary_path = args.out.with_suffix(".csv")
    corr_path = Path(str(args.out) + "_correlacion.csv")
    plot_path = args.out.with_suffix(".png")

    summary.to_csv(summary_path)
    corr.to_csv(corr_path)

    plt.figure(figsize=(10, 6))
    for label, d in daily.items():
        plt.plot(equity_curve(d).index, equity_curve(d).values, label=label, alpha=0.7)
    plt.plot(combined_eq.index, combined_eq.values, label="COMBINADO", color="black", linewidth=2.5)
    plt.axhline(0, color="gray", linewidth=0.8)
    plt.legend()
    plt.title("Equity por sistema vs. cartera combinada (datos reales del CSV)")
    plt.xlabel("Fecha")
    plt.ylabel("P&L acumulado")
    plt.tight_layout()
    plt.savefig(plot_path, dpi=130)

    print(f"\nGuardado: {summary_path}, {corr_path}, {plot_path}")


if __name__ == "__main__":
    main()
