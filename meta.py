"""
Meta-etiquetado (López de Prado): el indicador decide la DIRECCIÓN; un segundo modelo, entrenado con el
historial, decide QUÉ señales tomar. Aquí, con las garantías que lo hacen honesto:

  · entrena solo con el primer 70% del tiempo y se evalúa en el 30% final que no ha visto
  · PURGA: descarta del entrenamiento las operaciones que seguían abiertas al empezar la prueba
  · el umbral se elige en entrenamiento, no en prueba
  · prueba de azar: compara el filtro con 5.000 selecciones ALEATORIAS del mismo tamaño en el periodo de prueba.
    Si una selección aleatoria lo hace igual de bien a menudo, el modelo no aporta nada

  python meta.py --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT,AVAXUSDT,DOTUSDT --tf 15m --days 365
  python meta.py ... --guardar        → escribe meta_model.json (el bot lo usa con META_FILTER=aviso|bloquea)
"""
import argparse
import random
from types import SimpleNamespace

import config as C
from backtest import TIMELINES, load_symbol, metrics
from strategy import META_FEATURES, MetaModel, apply_breadth, select_trades


def cfg_with(**kw):
    base = {k: getattr(C, k) for k in dir(C) if k.isupper()}
    base.update(kw)
    return SimpleNamespace(**base)


def auc(probs, labels):
    pos = [p for p, y in zip(probs, labels) if y]
    neg = [p for p, y in zip(probs, labels) if not y]
    if not pos or not neg:
        return float("nan")
    wins = sum((p > q) + 0.5 * (p == q) for p in pos for q in neg)
    return wins / (len(pos) * len(neg))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,LINKUSDT,AVAXUSDT,DOTUSDT")
    ap.add_argument("--tf", default=C.TIMEFRAME)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--warmup", type=int, default=400)
    ap.add_argument("--strict", default="Agresivo", help="Agresivo da más señales para aprender")
    ap.add_argument("--incluir-trampas", action="store_true", help="aprende también sobre las operaciones trampa")
    ap.add_argument("--guardar", action="store_true")
    args = ap.parse_args()
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    cfg = cfg_with(TREND_FILTER="aviso", CONTEXT_FILTER="aviso", BTC_FILTER="aviso", BREADTH_FILTER="aviso",
                   META_FILTER="off", MIN_RR=0.0, FAIL_TRADES="on" if args.incluir_trampas else "off")
    allc = [c for s in syms for c in load_symbol(s, args.tf, args.days, args.warmup, args.strict, cfg)]
    apply_breadth(allc, TIMELINES)
    tr = sorted(select_trades(allc, cfg), key=lambda x: x["open_t"])
    print(f"{len(tr)} operaciones · {len(syms)} símbolos · {args.tf} · {args.days} días · exigencia {args.strict}")
    if len(tr) < 80:
        print("⚠ Menos de 80 operaciones: el modelo aprendería ruido. Añade símbolos o días, o usa un TF menor.")
        if len(tr) < 30:
            return
    cutoff = tr[0]["open_t"] + (tr[-1]["open_t"] - tr[0]["open_t"]) * 0.7
    train = [x for x in tr if x["close_t"] <= cutoff]          # purga: nada que solape con la prueba
    test = [x for x in tr if x["open_t"] >= cutoff]
    purged = len([x for x in tr if x["open_t"] < cutoff < x["close_t"]])
    print(f"entrenamiento {len(train)} · prueba {len(test)} · purgadas {purged} (abiertas al cruzar el corte)")
    m = MetaModel().fit(train, [1 if x["r"] > 0 else 0 for x in train])

    # umbral elegido en ENTRENAMIENTO: el que maximiza la media R conservando ≥40% de las operaciones
    ptr = sorted(m.prob(x) for x in train)
    best_thr, best_avg = 0.0, metrics([x["r"] for x in train])["avg"]
    for q in (0.2, 0.3, 0.4, 0.5, 0.6):
        thr = ptr[int(len(ptr) * q)]
        kept = [x["r"] for x in train if m.prob(x) >= thr]
        if len(kept) >= 0.4 * len(train) and metrics(kept)["avg"] > best_avg:
            best_thr, best_avg = thr, metrics(kept)["avg"]
    m.thr = best_thr

    probs = [m.prob(x) for x in test]
    labels = [1 if x["r"] > 0 else 0 for x in test]
    keep = [x["r"] for x, p in zip(test, probs) if p >= m.thr]
    allr = [x["r"] for x in test]
    base, filt = metrics(allr), metrics(keep)
    print("\n— PRUEBA (datos no vistos) —")
    print(f"AUC {auc(probs, labels):.3f}  (0.50 = no distingue ganadoras de perdedoras; 0.55+ empieza a ser algo)")
    print(f"todas las señales   {base['n']:>4} ops  media {base['avg']:+.3f}R  total {base['tot']:+.2f}R  PF {base['pf']:.2f}")
    print(f"filtradas p≥{m.thr:.2f}   {filt['n']:>4} ops  media {filt['avg']:+.3f}R  total {filt['tot']:+.2f}R  PF {filt['pf']:.2f}")
    if keep and len(keep) < len(allr):
        rnd = random.Random(7)
        better = sum(1 for _ in range(5000)
                     if sum(rnd.sample(allr, len(keep))) / len(keep) >= filt["avg"])
        pval = better / 5000
        print(f"prueba de azar: {pval:.1%} de las selecciones aleatorias del mismo tamaño lo hacen igual o mejor")
        verdict = ("el filtro aporta (p<5%)" if pval < 0.05 else "indicio débil (5-15%)" if pval < 0.15
                   else "NO aporta: es indistinguible de elegir al azar")
        print(f"→ {verdict}")
    print("\n— qué ha aprendido (coeficientes estandarizados; + = sube la probabilidad de ganar) —")
    for k, w in sorted(m.w.items(), key=lambda kv: -abs(kv[1])):
        print(f"  {k:<10} {w:+.3f}")
    if args.guardar:
        m.save(C.META_MODEL, {"symbols": syms, "tf": args.tf, "days": args.days, "strict": args.strict,
                              "n_train": len(train), "test_avg_all": base["avg"], "test_avg_kept": filt["avg"],
                              "feats": list(META_FEATURES)})
        print(f"\nGuardado en {C.META_MODEL}. Súbelo al repo y usa META_FILTER=aviso unas semanas antes de 'bloquea'.")


if __name__ == "__main__":
    main()
