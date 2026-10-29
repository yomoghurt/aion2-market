#!/usr/bin/env python3
"""AION2 取引所ウォッチャー：データ収集スクリプト

aion2.exchange の公開API（CORSなし）から Asia リージョンのサーバーの全アイテム価格を取得し、
1時間単位の履歴に追記して、値動きの分析結果（上がりそう/下がりそう）と一緒に静的JSONとして書き出す。
GitHub Actions から毎時実行し、出力を GitHub Pages で配信する想定。標準ライブラリのみ。

出力（--out 以下）:
  data/index.json               サーバー一覧と更新時刻
  data/names.json               アイテムID -> [日本語名, アイコンURL]（NC公式サイトから取得）
  data/s/<server>/summary.json  ダッシュボード用の一覧（現在値・変化率・スコア・理由・ミニチャート）
  data/s/<server>/hist.json     全履歴（次回実行時のマージ用）
  data/s/<server>/i/<item>.json アイテム詳細チャート用の履歴

前回の履歴は --prev（公開中サイトのURL）から読み戻すので、リポジトリにデータをコミットしない。
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

EXCHANGE = "https://aion2.exchange"
NC = "https://aion2.plaync.com"
UA = "aion2-market-watch/1.0 (personal dashboard; hourly)"
DELAY = 0.25          # リクエスト間隔（秒）。相手は個人運営なので控えめに
PAGE = 200            # /api/items の limit 上限
KEEP_DAYS = 30        # 保持する履歴
BACKFILL_DAYS = 14    # 履歴が薄いアイテムを過去分で埋める日数
BACKFILL_MAX = 400    # 1回の実行で過去分を取りに行く最大件数（残りは次回）
NAMES_MAX = 1000      # 1回の実行で日本語名を取りに行く最大件数
DATA_VERSION = 1

# ── 通信 ───────────────────────────────────────────────────────────────────

_last = [0.0]


def get_json(url: str, params: dict | None = None, retries: int = 4, polite: bool = True):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    err: Exception | None = None
    for attempt in range(retries):
        if polite:
            wait = DELAY - (time.monotonic() - _last[0])
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = e
            busy = e.code in (429, 502, 503, 504)
            ra = e.headers.get("Retry-After") if e.headers else None
            time.sleep(min(int(ra) if ra and ra.isdigit() else (5 * 2 ** attempt if busy else 2 ** attempt), 60))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, http.client.HTTPException, OSError) as e:
            err = e
            time.sleep(min(2 ** attempt, 10))
    raise RuntimeError(f"{url}: {err}")


def hour_of(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return int(t.timestamp() // 3600)


# ── 履歴（1時間グリッド） ──────────────────────────────────────────────────
# hist = {"h0": 最初の時(エポック時), "items": {key: {"p": [...], "l": [...]}}}
# p[i], l[i] は h0+i 時の最安出品価格と出品数。欠損は None。


class History:
    def __init__(self, doc: dict | None):
        doc = doc or {}
        self.h0: int | None = doc.get("h0")
        self.items: dict[str, dict] = doc.get("items") or {}

    def _ensure(self, key: str, hour: int) -> dict:
        if self.h0 is None:
            self.h0 = hour
        if hour < self.h0:  # 過去側に伸ばす
            pad = self.h0 - hour
            for s in self.items.values():
                s["p"] = [None] * pad + s["p"]
                s["l"] = [None] * pad + s["l"]
            self.h0 = hour
        s = self.items.setdefault(key, {"p": [], "l": []})
        need = hour - self.h0 + 1
        if len(s["p"]) < need:
            s["p"] += [None] * (need - len(s["p"]))
            s["l"] += [None] * (need - len(s["l"]))
        return s

    def put(self, key: str, hour: int, price, listings, keep_lower: bool = False):
        if price is None or price <= 0:
            return
        s = self._ensure(key, hour)
        i = hour - self.h0
        if keep_lower and s["p"][i] is not None:
            if price < s["p"][i]:
                s["p"][i] = price
        else:
            s["p"][i] = price
        if listings is not None:
            s["l"][i] = listings

    def points(self, key: str) -> int:
        s = self.items.get(key)
        return sum(1 for v in s["p"] if v is not None) if s else 0

    def trim(self, now_hour: int, keep_days: int = KEEP_DAYS):
        if self.h0 is None:
            return
        cut = now_hour - keep_days * 24
        end = now_hour + 1
        for k, s in list(self.items.items()):
            n = end - self.h0
            s["p"] = (s["p"] + [None] * max(0, n - len(s["p"])))[:n]
            s["l"] = (s["l"] + [None] * max(0, n - len(s["l"])))[:n]
        if cut > self.h0:
            d = cut - self.h0
            for s in self.items.values():
                s["p"], s["l"] = s["p"][d:], s["l"][d:]
            self.h0 = cut
        for k in [k for k, s in self.items.items() if all(v is None for v in s["p"])]:
            del self.items[k]

    def series(self, key: str) -> tuple[list, list]:
        s = self.items.get(key) or {"p": [], "l": []}
        return s["p"], s["l"]

    def doc(self) -> dict:
        return {"v": DATA_VERSION, "h0": self.h0, "items": self.items}


# ── 分析 ───────────────────────────────────────────────────────────────────


def _valid(xs, start=0, end=None):
    end = len(xs) if end is None else end
    return [(i, v) for i, v in enumerate(xs[start:end], start) if v is not None and v > 0]


def clean(p: list, win: int = 6, hi: float = 2.5, lo: float = 0.4) -> list:
    """外れ出品（相場の何倍もの1件だけの出品など）を除く。前後win時間の中央値から大きく外れた点を欠損扱いにする。"""
    out = list(p)
    for i, v in enumerate(p):
        if v is None:
            continue
        nb = [x for x in p[max(0, i - win): i + win + 1] if x is not None and x > 0]
        if len(nb) >= 4:
            m = statistics.median(nb)
            if v > m * hi or v < m * lo:
                out[i] = None
    return out


def _near(p: list, idx: int, tol: int = 3):
    """idx 付近（±tol時間）の価格。近い順に探す。"""
    for d in range(tol + 1):
        for j in (idx - d, idx + d):
            if 0 <= j < len(p) and p[j] is not None and p[j] > 0:
                return p[j]
    return None


def _slope_per_day(pts: list[tuple[int, float]]) -> float | None:
    """ln(価格) の線形回帰から1日あたりの変化率。"""
    if len(pts) < 8:
        return None
    xs = [i for i, _ in pts]
    ys = [math.log(v) for _, v in pts]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return math.exp(b * 24) - 1


def _daily_vol(pts: list[tuple[int, float]]) -> float | None:
    rets = []
    for (i0, v0), (i1, v1) in zip(pts, pts[1:]):
        if 0 < i1 - i0 <= 3:
            rets.append(math.log(v1 / v0) / math.sqrt(i1 - i0))
    if len(rets) < 6:
        return None
    return statistics.pstdev(rets) * math.sqrt(24)


def features(p: list, l: list, t: int) -> dict | None:
    """時刻インデックス t までのデータだけを使って特徴量を作る（過去検証でも同じ関数を使う）。"""
    if t < 0 or t >= len(p):
        return None
    w7 = _valid(p, max(0, t - 167), t + 1)
    if len(w7) < 12:
        return None
    recent = [v for _, v in _valid(p, max(0, t - 2), t + 1)]
    if not recent:
        return None
    now = statistics.median(recent)  # 直近3時間の中央値：1件の外れ出品に引っ張られないように
    med7 = statistics.median([v for _, v in w7])
    w72 = _valid(p, max(0, t - 71), t + 1)
    slope = _slope_per_day(w72)
    vol = _daily_vol(w72) or _daily_vol(w7)
    p24, p7d = _near(p, t - 24), _near(p, t - 168, 6)
    lw = [v for v in l[max(0, t - 167): t + 1] if v is not None]
    lnow = next((l[j] for j in range(t, max(-1, t - 3), -1) if j < len(l) and l[j] is not None), None)
    lmed = statistics.median(lw) if lw else None
    lt = (lnow / lmed - 1) if (lnow is not None and lmed) else None
    return {
        "now": now, "med7": med7, "dev": now / med7 - 1,
        "ch24": (now / p24 - 1) if p24 else None,
        "ch7": (now / p7d - 1) if p7d else None,
        "slope": slope, "vol": vol, "lnow": lnow, "lt": lt, "n": len(w7),
    }


def score(f: dict, sold: float | None = None) -> tuple[float, list]:
    """+ は上がりそう、- は下がりそう。各成分は -1..1 に丸めて重み付け。"""
    parts = []
    if f["slope"] is not None:
        parts.append(("trend", 0.40, math.tanh(f["slope"] / 0.10)))
    if f["lt"] is not None:
        parts.append(("supply", 0.35, math.tanh(-f["lt"] / 0.40)))
    parts.append(("value", 0.25, math.tanh(-f["dev"] / 0.20)))
    tot_w = sum(w for _, w, _ in parts)
    raw = sum(w * v for _, w, v in parts) / tot_w if tot_w else 0.0
    conf = min(1.0, f["n"] / 48)
    if f["lnow"] is not None:
        conf *= min(1.0, (f["lnow"] + 1) / 5)  # 出品1〜3件の品は最安値が1件で決まるので信頼度を下げる
    if sold is not None:
        conf *= 0.5 + 0.5 * min(1.0, math.log10(1 + sold) / 2)  # 売れていない品は信頼度を下げる
    return raw * conf, [(name, w * v / tot_w) for name, w, v in parts]


def pct(x: float) -> str:
    return f"{x * 100:+.0f}%"


def reasons(f: dict, parts: list) -> tuple[list, list]:
    w = {name: abs(v) for name, v in parts}
    tags, why = [], []
    s, lt, dev = f["slope"], f["lt"], f["dev"]
    if s is not None and s >= 0.03:
        tags.append("上昇トレンド"); why.append((w.get("trend", 0), f"直近3日で1日あたり{pct(s)}のペースで上昇"))
    if s is not None and s <= -0.03:
        tags.append("下落トレンド"); why.append((w.get("trend", 0), f"直近3日で1日あたり{pct(s)}のペースで下落"))
    if lt is not None and lt <= -0.25:
        tags.append("出品減少"); why.append((w.get("supply", 0), f"出品数が7日の中央値比{pct(lt)}：品薄で上がりやすい"))
    if lt is not None and lt >= 0.30:
        tags.append("出品増加"); why.append((w.get("supply", 0), f"出品数が7日の中央値比{pct(lt)}：だぶつき気味"))
    if dev <= -0.15:
        tags.append("割安"); why.append((w.get("value", 0), f"価格が7日の中央値比{pct(dev)}：反発の余地"))
    if dev >= 0.20:
        tags.append("割高"); why.append((w.get("value", 0), f"価格が7日の中央値比{pct(dev)}：売り時の可能性"))
    if f["vol"] is not None and f["vol"] >= 0.20:
        tags.append("値動き大")
    why.sort(key=lambda x: -x[0])
    return tags, [t for _, t in why]


THRESH = 0.15  # |スコア| がこれ以上で「上がりそう/下がりそう」に載せる


def backtest(hist: History, sold: dict) -> dict:
    """過去データで「その時点のスコア」と「24時間後の実際の値動き」を突き合わせた的中率。"""
    hit = n = 0
    rets_up, rets_dn = [], []
    for key, s in hist.items.items():
        p, l = clean(s["p"]), s["l"]
        T = len(p)
        for t in range(48, T - 24, 12):
            f = features(p, l, t)
            if not f:
                continue
            sc, _ = score(f, sold.get(key))
            if abs(sc) < THRESH:
                continue
            fut = _near(p, t + 24, 2)
            if not fut:
                continue
            r = fut / f["now"] - 1
            if abs(r) < 0.01:  # ほぼ動かなかった回は数えない
                continue
            n += 1
            ok = (r > 0) == (sc > 0)
            hit += ok
            (rets_up if sc > 0 else rets_dn).append(r)
    return {
        "n": n,
        "hit_rate": round(hit / n, 3) if n else None,
        "avg_up": round(statistics.fmean(rets_up), 4) if rets_up else None,
        "avg_down": round(statistics.fmean(rets_dn), 4) if rets_dn else None,
    }


def spark(p: list, end: int, hours: int = 168, step: int = 4) -> list:
    out = []
    for t in range(max(0, end - hours + 1), end + 1, step):
        vals = [v for v in p[t: t + step] if v is not None]
        out.append(round(min(vals)) if vals else None)
    return out


# ── 収集 ───────────────────────────────────────────────────────────────────


def fetch_items(base: str, sid: int) -> list[dict]:
    rows, off = [], 0
    while True:
        j = get_json(f"{base}/api/items", {"server": sid, "limit": PAGE, "offset": off, "window": "24h"}) or {}
        items = j.get("items") or []
        rows += items
        off += PAGE
        if len(items) < PAGE or (j.get("total") and len(rows) >= j["total"]):
            return rows


def backfill(base: str, sid: int, key: str, hist: History, days: int):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    j = get_json(f"{base}/api/items/{key}/prices", {"server": sid, "bucket": "raw", "from": since}) or {}
    for pt in j.get("points") or []:
        h = hour_of(pt.get("ts"))
        price = pt.get("low", pt.get("price"))
        if h is not None and price:
            hist.put(key, h, float(price), pt.get("listings"), keep_lower=True)


def _is_japanese(s: str) -> bool:
    return any("぀" <= ch <= "ヿ" for ch in s or "")  # ひらがな・カタカナを含むか


def _name_url():
    """日本語名を返すNC公式のアイテムAPIを、候補から1件試して見つける。見つからなければ None。
    2026/10時点、グローバル版向けの /ja-jp/api/gameconst/item は404、ロケール無しは韓国語のみを返す。"""
    probe = "516530001"  # 突破石（ユニーク）
    for url in (f"{NC}/ja-jp/api/gameconst/item", f"{NC}/api/gameconst/item"):
        try:
            j = get_json(url, {"lang": "ja-JP", "region": "as", "id": probe, "enchantLevel": 0}, retries=1)
        except Exception:
            continue
        if j and _is_japanese(j.get("name")):
            return url
    return None


def fetch_names(keys: list[str], names: dict, limit: int) -> int:
    todo = [k for k in keys if k not in names][:limit]
    if not todo:
        return 0
    url = _name_url()
    if not url:
        print("日本語名：NC公式のアイテムAPIが日本語を返さないため今回は取得しない", file=sys.stderr)
        return 0
    got = 0
    for k in todo:
        try:
            j = get_json(url, {"lang": "ja-JP", "region": "as", "id": k, "enchantLevel": 0}, retries=1)
        except Exception as e:  # 名前は飾り。取れなくても英名で表示する
            print(f"name {k}: {e}", file=sys.stderr)
            continue
        if j and j.get("id") and _is_japanese(j.get("name")):
            names[k] = [j["name"], j.get("icon") or ""]
            got += 1
    return got


def load_prev(prev: str | None, path: str):
    if not prev:
        return None
    try:
        return get_json(prev.rstrip("/") + "/" + path, {"run": int(time.time())}, retries=2, polite=False)
    except Exception as e:
        print(f"prev {path}: {e}", file=sys.stderr)
        return None


def build_server(base: str, server: dict, out: Path, prev: str | None, names: dict, budget: dict) -> dict:
    sid = server["server_id"]
    sdir = out / "data" / "s" / str(sid)
    (sdir / "i").mkdir(parents=True, exist_ok=True)
    hist = History(load_prev(prev, f"data/s/{sid}/hist.json"))

    rows = fetch_items(base, sid)
    meta, sold = {}, {}
    now_hour = int(time.time() // 3600)
    captured = None
    for r in rows:
        k = str(r.get("item_key"))
        h = hour_of(r.get("captured_at")) or now_hour
        captured = max(captured or "", r.get("captured_at") or "")
        hist.put(k, h, r.get("price"), r.get("listings"))
        cat = r.get("category") or {}
        meta[k] = {
            "en": r.get("name"), "cat": cat.get("name"), "sec": cat.get("section_name"),
            "price": r.get("price"), "listings": r.get("listings"),
            "units_sold": r.get("units_sold"), "sold_avg": r.get("sold_avg_price"),
        }
        sold[k] = r.get("units_sold")

    # 履歴が薄いアイテムを過去分で埋める（初回はほぼ全件。上限を超えた分は次回に回す）
    thin = sorted((k for k in meta if hist.points(k) < 24), key=lambda k: -(sold.get(k) or 0))
    for k in thin:
        if budget["backfill"] <= 0:
            break
        budget["backfill"] -= 1
        try:
            backfill(base, sid, k, hist, BACKFILL_DAYS)
        except Exception as e:
            print(f"backfill {sid}/{k}: {e}", file=sys.stderr)

    hist.trim(now_hour)
    end = now_hour - (hist.h0 or now_hour)

    items = []
    for k, m in meta.items():
        raw, l = hist.series(k)
        p = clean(raw)
        f = features(p, l, min(end, len(p) - 1)) if p else None
        row = {
            "k": int(k), "en": m["en"], "cat": m["cat"], "sec": m["sec"],
            "p": m["price"], "l": m["listings"], "us": m["units_sold"], "sa": m["sold_avg"],
        }
        if f:
            sc, parts = score(f, sold.get(k))
            tags, why = reasons(f, parts)
            row.update({
                "ch24": _r(f["ch24"]), "ch7": _r(f["ch7"]), "dev": _r(f["dev"]), "med7": round(f["med7"]),
                "slope": _r(f["slope"]), "vol": _r(f["vol"]), "lt": _r(f["lt"]),
                "sc": round(sc, 3), "tags": tags, "why": why, "sp": spark(p, len(p) - 1),
            })
        items.append(row)
        (sdir / "i" / f"{k}.json").write_text(json.dumps(
            {"h0": hist.h0, "p": p, "l": l}, separators=(",", ":")))

    bt = backtest(hist, sold)
    summary = {
        "generated_at": _iso(), "captured_at": captured, "server": server,
        "thresh": THRESH, "backtest": bt, "items": items,
    }
    (sdir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    (sdir / "hist.json").write_text(json.dumps(hist.doc(), separators=(",", ":")))
    print(f"{server.get('name')} ({sid}): {len(items)} items, history from hour {hist.h0}, backtest {bt}")
    return {**server, "captured_at": captured, "generated_at": summary["generated_at"], "items": len(items)}


def _r(x):
    return None if x is None else round(x, 4)


def _iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    global DELAY
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    ap.add_argument("--base", default=EXCHANGE)
    ap.add_argument("--nc", default=NC)
    ap.add_argument("--prev", help="公開中サイトのURL（前回の履歴を読み戻す）")
    ap.add_argument("--regions", default="AS", help="対象リージョンコード（カンマ区切り）")
    ap.add_argument("--servers", help="サーバーIDを直接指定（カンマ区切り）")
    ap.add_argument("--extra-servers", default="", help="リージョンに加えて収集するサーバーID（カンマ区切り）")
    ap.add_argument("--delay", type=float, default=DELAY)
    a = ap.parse_args()
    DELAY = a.delay
    globals()["NC"] = a.nc.rstrip("/")
    base = a.base.rstrip("/")
    out = Path(a.out)
    (out / "data").mkdir(parents=True, exist_ok=True)

    srv = (get_json(f"{base}/api/servers") or {}).get("servers") or []
    if a.servers:
        want = {int(x) for x in a.servers.split(",")}
        servers = [s for s in srv if s.get("server_id") in want]
    else:
        regions = {r.strip().upper() for r in a.regions.split(",")}
        servers = [s for s in srv if (s.get("region") or "").upper() in regions]
    if a.extra_servers:  # リージョン指定に加えて個別に追加するサーバー（例: EUのネザカン 11302）
        extra = {int(x) for x in a.extra_servers.split(",") if x.strip()}
        have = {s.get("server_id") for s in servers}
        # 先頭に置く：過去分の取り込み枠（BACKFILL_MAX）を、後から追加したサーバーに優先して回す
        servers = [s for s in srv if s.get("server_id") in extra and s.get("server_id") not in have] + servers
    servers = [{k: s.get(k) for k in ("server_id", "name", "region", "region_name", "faction")} for s in servers]
    if not servers:
        print(f"対象サーバーなし。取得できたリージョン: {sorted({s.get('region') for s in srv})}", file=sys.stderr)
        return 1

    names = load_prev(a.prev, "data/names.json") or {}
    # 韓国比較用に訳した日本語名（site/kr.json）を下地にする。公式から取れた名前があればそちらを優先
    try:
        for it in json.loads((out / "kr.json").read_text(encoding="utf-8")).get("items") or []:
            names.setdefault(str(it["key"]), [it["ja"], ""])
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    budget = {"backfill": BACKFILL_MAX}
    done, errors = [], []
    for s in servers:
        try:
            done.append(build_server(base, s, out, a.prev, names, budget))
        except Exception as e:
            print(f"server {s['server_id']}: {e}", file=sys.stderr)
            errors.append(f"{s.get('name')}: {e}")
            # 失敗したサーバーは前回の公開データを引き継ぐ
            for f in ("summary.json", "hist.json"):
                doc = load_prev(a.prev, f"data/s/{s['server_id']}/{f}")
                if doc:
                    d = out / "data" / "s" / str(s["server_id"])
                    d.mkdir(parents=True, exist_ok=True)
                    (d / f).write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
                    if f == "summary.json":
                        done.append({**s, "stale": True, "generated_at": doc.get("generated_at"),
                                     "captured_at": doc.get("captured_at"), "items": len(doc.get("items") or [])})

    keys = sorted({str(i["k"]) for d in done if not d.get("stale")
                   for i in json.loads((out / "data" / "s" / str(d["server_id"]) / "summary.json").read_text())["items"]})
    # クラフトの素材・製品の名前も取る（相場に出ている品を先に。残りは次回以降）
    rp = out / "recipes.json"
    if rp.exists():
        try:
            rkeys = json.loads(rp.read_text()).get("names") or {}
            have = set(keys)
            keys += sorted(k for k in rkeys if k not in have)
        except (OSError, json.JSONDecodeError) as e:
            print(f"recipes.json: {e}", file=sys.stderr)
    got = fetch_names(keys, names, NAMES_MAX)
    print(f"日本語名: {got}件追加（計{len(names)}件）")
    (out / "data" / "names.json").write_text(json.dumps(names, ensure_ascii=False, separators=(",", ":")))
    (out / "data" / "index.json").write_text(json.dumps({
        "generated_at": _iso(), "source": base, "data_version": DATA_VERSION,
        "servers": done, "errors": errors,
    }, ensure_ascii=False, separators=(",", ":")))
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
