#!/usr/bin/env python3
"""クラフトレシピを軽量化して site/recipes.json に書き出す（ゲームの大型アップデート後に再実行）。

元データ: dimkk/aion2-market-stack の exports/economy/recipes.json（Apache License 2.0）
  https://github.com/dimkk/aion2-market-stack

  git clone --depth 1 https://github.com/dimkk/aion2-market-stack.git /tmp/ams
  python tools/build_recipes.py /tmp/ams/exports/economy/recipes.json site/recipes.json

出力形式:
  {"build": ゲームのビルド番号, "src": 出典, "names": {id: 英名},
   "r": [[レシピID, 系統, 分類, 陣営(L/D), 熟練度, 方式, コンボ確率(0-1), コンボ時の製品ID,
          製品ID, 製品数, [[素材ID, 個数], ...], 種類(C=通常/S=継承)], ...]}
"""
import json
import sys
from pathlib import Path

SRC_URL = "https://github.com/dimkk/aion2-market-stack"


def main(src: str, out: str) -> int:
    d = json.loads(Path(src).read_text())
    recipes = d["recipes"]
    by_internal = {r["internalName"]: r for r in recipes}
    names, rows = {}, []
    for r in recipes:
        if not r["materials"]:
            # コンボ専用レシピ（元レシピのコンボでしか出ない）。直接は作れないので除外し、
            # 元レシピ側の「コンボ時の製品」として扱う
            continue
        out_item = r["output"]
        names[str(out_item["itemId"])] = out_item.get("name") or r.get("name")
        mats = []
        for m in r["materials"]:
            names[str(m["itemId"])] = m.get("name")
            mats.append([m["itemId"], m["quantity"]])
        combo = by_internal.get(r.get("comboRecipeInternalName") or "")
        combo_out = combo["output"]["itemId"] if combo else None
        if combo:
            names[str(combo_out)] = combo["output"].get("name") or combo.get("name")
        rows.append([
            r["recipeId"], r["category"], r["subcategory"],
            "L" if r["race"] == "Light" else "D" if r["race"] == "Dark" else "A",
            r.get("masteryLevel") or 0, r.get("learnType"),
            round((r.get("comboProbability") or 0) / 10000, 4), combo_out,
            out_item["itemId"], out_item.get("quantity") or 1, mats,
            "S" if r.get("craftType") == "Succession" else "C",
        ])
    doc = {
        "build": d.get("appliesToBuild") or d.get("sourceBuild"),
        "src": SRC_URL + " (Apache-2.0)",
        "names": names,
        "r": rows,
    }
    Path(out).write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
    print(f"{len(rows)} recipes, {len(names)} items -> {out} ({Path(out).stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
