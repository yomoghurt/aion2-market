#!/usr/bin/env python3
"""動作確認用の模擬サーバー。aion2.exchange と NC公式の item API と同じ形のJSONを返す。
価格は決定的な疑似乱数で作るランダムウォーク（上昇・下落・横ばい・急騰の品を混ぜる）。

  python tools/mock_exchange.py 8765
  python collector/collect.py --base http://127.0.0.1:8765 --nc http://127.0.0.1:8765 --out site --delay 0
"""
import json
import math
import random
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

N_ITEMS = 450
SERVERS = [
    {"server_id": 15101, "name": "Tiamat", "region": "AS", "region_name": "Asia", "faction": "Elyos", "has_data": True},
    {"server_id": 15201, "name": "Tiamat", "region": "AS", "region_name": "Asia", "faction": "Asmodian", "has_data": True},
    {"server_id": 11201, "name": "Siel", "region": "NAW", "region_name": "North America West", "faction": "Elyos", "has_data": True},
]
CATS = [("Enchant stones", "Materials"), ("Manastones", "Materials"), ("Potions", "Consumables"),
        ("Greatswords", "Weapons"), ("Chest", "Armor"), ("Crafting ore", "Materials")]
JA = ["強化石", "マナストーン", "回復ポーション", "大剣", "胸当て", "鉱石"]
GRADE = ["(コモン)", "(レア)", "(レジェンド)", "(ユニーク)"]


START_H = int(time.time() // 3600) - 24 * 20
_cache = {}


def _series(key: int, sid: int, upto: int):
    """アイテムごとの性格（ドリフト・ボラ・急騰イベント）を種から決めた、START_H からの時系列。"""
    c = _cache.get((key, sid))
    if c and len(c) > upto - START_H:
        return c
    r = random.Random(key * 7 + sid)
    base = 10 ** r.uniform(2.5, 6.5)
    drift = r.choice([0.0, 0.0, 0.0, 0.004, -0.004, 0.01, -0.01])
    vol = r.uniform(0.005, 0.04)
    shock_at = r.randrange(0, 24 * 18) if r.random() < 0.15 else None
    lst = r.randint(3, 80)
    rr = random.Random(key * 31 + sid * 3)
    lp, out = 0.0, []
    for i in range(upto - START_H + 1):
        lp += drift + rr.gauss(0, vol)
        if shock_at is not None and i == shock_at:
            lp += rr.choice([0.4, -0.35])
        lp = max(min(lp, 1.5), -1.5)
        listings = max(1, int(lst * math.exp(-lp * 1.2) + rr.randint(-2, 2)))
        price = base * math.exp(lp)
        if rr.random() < 0.004:
            price *= 8  # たまに混じる外れ出品
        out.append((round(price), listings))
    _cache[(key, sid)] = out
    return out


def walk(key: int, sid: int, hour: int):
    return _series(key, sid, max(hour, int(time.time() // 3600)))[hour - START_H]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        now_h = int(time.time() // 3600)
        if u.path == "/api/servers":
            return self.send({"default": 11201, "servers": SERVERS})
        if u.path == "/api/items":
            sid, lim, off = int(q["server"]), int(q.get("limit", 50)), int(q.get("offset", 0))
            if lim > 200:
                return self.send({"detail": "limit"}, 422)
            rows = []
            for i in range(off, min(off + lim, N_ITEMS)):
                key = 100000 + i
                p, l = walk(key, sid, now_h)
                c = CATS[i % len(CATS)]
                rows.append({
                    "item_key": key, "name": f"Item {i} ({c[0]})",
                    "category": {"id": i % len(CATS), "name": c[0], "section_id": c[1].lower(), "section_name": c[1]},
                    "price": float(p), "listings": l,
                    "captured_at": datetime.now(timezone.utc).replace(minute=0, second=0).isoformat().replace("+00:00", "Z"),
                    "change_pct": 0.0, "units_sold": random.Random(key).randint(0, 3000),
                    "sold_avg_price": float(p) * 0.95, "sold_min_price": float(p) * 0.7, "sold_max_price": float(p) * 1.4,
                })
            return self.send({"total": N_ITEMS, "limit": lim, "offset": off, "items": rows})
        if u.path.startswith("/api/items/") and u.path.endswith("/prices"):
            key, sid = int(u.path.split("/")[3]), int(q["server"])
            since = datetime.fromisoformat(q["from"].replace("Z", "+00:00"))
            pts = []
            h = int(since.timestamp() // 3600)
            while h < now_h:
                for m in (0, 15, 30, 45):
                    p, l = walk(key, sid, h)
                    pts.append({"ts": datetime.fromtimestamp(h * 3600 + m * 60, timezone.utc).isoformat().replace("+00:00", "Z"),
                                "low": float(p) * (1 + 0.01 * (m % 2)), "listings": l})
                h += 1
            return self.send({"item_key": key, "points": pts})
        if u.path.endswith("/api/gameconst/item"):
            i = int(q["id"]) - 100000
            if not 0 <= i < N_ITEMS:
                return self.send({"id": 0, "name": ""})
            return self.send({"id": int(q["id"]), "name": f"{JA[i % len(JA)]}{GRADE[i % 4]} #{i}", "icon": ""})
        self.send({"detail": "not found"}, 404)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8765), H).serve_forever()
