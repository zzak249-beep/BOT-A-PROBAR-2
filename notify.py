"""Telegram + diario de operaciones reales (CSV en el volumen)."""
import csv
import logging
import os
import time

import requests

log = logging.getLogger("notify")


class Telegram:
    def __init__(self, token, chat):
        self.token, self.chat = token, chat
        self.ok = bool(token and chat)
        if not self.ok:
            log.warning("Telegram sin configurar: los avisos solo van al log")

    def send(self, text):
        log.info("TG | %s", text.replace("\n", " | "))
        if not self.ok:
            return
        # mensajes largos (universo de cientos de símbolos) se trocean por líneas en vez de cortarse
        chunks, cur = [], ""
        for line in text.split("\n"):
            if len(cur) + len(line) + 1 > 3900 and cur:
                chunks.append(cur)
                cur = ""
            cur = f"{cur}\n{line}" if cur else line[:3900]
        chunks.append(cur)
        for ch in chunks:
            self._send_one(ch)

    def _send_one(self, text):
        for attempt in range(3):
            try:
                r = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                  json={"chat_id": self.chat, "text": text[:4000], "parse_mode": "HTML",
                                        "disable_web_page_preview": True}, timeout=10)
                if r.status_code == 200:
                    return
                if r.status_code == 400:  # HTML inválido → reintenta en texto plano
                    requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                  json={"chat_id": self.chat, "text": text[:4000]}, timeout=10)
                    return
            except requests.RequestException as e:
                log.warning("Telegram: %s", e)
            time.sleep(2 * (attempt + 1))


    def poll(self):
        """Comandos del chat autorizado (solo TELEGRAM_CHAT_ID). Devuelve lista de textos."""
        if not self.ok:
            return []
        try:
            r = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates",
                             params={"offset": getattr(self, "_off", 0), "timeout": 0}, timeout=8)
            res = r.json().get("result", [])
        except (requests.RequestException, ValueError):
            return []
        out = []
        for u in res:
            self._off = u["update_id"] + 1
            m = u.get("message") or u.get("channel_post") or {}
            if str(m.get("chat", {}).get("id")) == str(self.chat) and m.get("text", "").startswith("/"):
                out.append(m["text"].strip())
        return out


class Journal:
    FIELDS = ["open_time", "close_time", "symbol", "tf", "side", "kind", "entry_expected", "entry_real", "slippage_pct",
              "sl", "tp1", "tp2", "rr_plan", "qty", "exit_reason", "r_net", "minutes", "conf", "val",
              "against_trend", "ctx_align", "ctx_label", "btc_align", "funding", "range_atr", "b_bars", "flow", "flow_exc", "breadth", "meta_p", "struct_r", "struct_why", "mode"]

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "journal.csv")
        if os.path.exists(self.path):
            with open(self.path) as f:
                head = f.readline().strip().split(",")
            if head != self.FIELDS:  # diario de una versión anterior: se conserva aparte
                os.replace(self.path, self.path.replace(".csv", f"_v1_{int(time.time())}.csv"))
        if not os.path.exists(self.path):
            with open(self.path, "w", newline="") as f:
                csv.writer(f).writerow(self.FIELDS)

    def write(self, row):
        try:
            with open(self.path, "a", newline="") as f:
                csv.DictWriter(f, fieldnames=self.FIELDS, extrasaction="ignore").writerow(row)
        except OSError as e:
            log.error("journal: %s", e)


class EventLog:
    """events.csv: una fila por evento Wyckoff con lo que en perpetuos delata el posicionamiento (funding, prima, OI).
    El bot solo registra; events_report.py baja las velas posteriores y mide qué rasgos anticipan el movimiento."""
    FIELDS = ["ts_ms", "time_utc", "symbol", "tf", "cls", "event", "dir", "price", "atr", "phase", "conf", "val",
              "funding_pct", "premium_pct", "oi_now", "oi_chg_1h", "oi_chg_6h", "oi_chg_24h", "oi_src", "rh", "rl"]

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "events.csv")
        if os.path.exists(self.path):
            with open(self.path) as f:
                head = f.readline().strip().split(",")
            if head != self.FIELDS:
                os.replace(self.path, self.path.replace(".csv", f"_old_{int(time.time())}.csv"))
        if not os.path.exists(self.path):
            with open(self.path, "w", newline="") as f:
                csv.writer(f).writerow(self.FIELDS)

    def write(self, row):
        try:
            with open(self.path, "a", newline="") as f:
                csv.DictWriter(f, fieldnames=self.FIELDS, extrasaction="ignore").writerow(row)
        except OSError as e:
            log.error("events: %s", e)
