#!/usr/bin/env python3
"""Claudeが「見方の一言」と「理由」を書くための材料を、読みやすく小さく出す。

先に scripts で  OUT_DIR=../docs python3 collect.py --rebuild --no-buzz  を動かしておく(scripts/material.json ができる)。
  python3 tools/lines_material.py          まだ言葉を書いていない試合だけ
  python3 tools/lines_material.py --all    直近3日の全部
  python3 tools/lines_material.py --recent 最近書いた言葉(同じ言い回しを続けないために読む)
注意:ここに出る数字は試合の結果そのもの(ネタバレ)。書いた言葉はボタンを押した人にしか出ない。
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
S = os.path.join(HERE, "..", "scripts")
lines = json.load(open(os.path.join(S, "lines.json"))) if os.path.exists(os.path.join(S, "lines.json")) else {}

if "--recent" in sys.argv:
    for gid, ln in sorted(lines.items(), key=lambda kv: kv[1].get("at", ""))[-25:]:
        print(f"{ln.get('at','')} [{ln.get('k','')}] 一言: {ln.get('guide','')} / 理由: {ln.get('why','')}")
    sys.exit()

mat = json.load(open(os.path.join(S, "material.json")))
todo = [g for g in mat if "--all" in sys.argv or str(g["id"]) not in lines]
print(f"書く試合: {len(todo)} / 直近3日の終わった試合: {len(mat)}")
for g in sorted(todo, key=lambda g: g["date"]):
    j = lambda xs: "、".join(str(x) for x in xs) if xs else "なし"
    print(f"\n[{g['id']}] {g['date']} {g['away']}@{g['home']} ランク{g['tier']} {g['total']}点 見方の種類={g['gk']}(決まり文句:{g['guide_default']})")
    print(f" 決まり文句の理由: {g['why_default']}")
    print(f" 展開: {j(g['flow'])} / スター: {j([f'''{x['who']}({x['team']}){x['pts']}点{x['why']}''' for x in g['stars']])}")
    print(f" 格: {j(g['card'])} / チーム: {j(g['team'])} / 記録: {j(g['record'])} / いつものスタメンの欠場: {g['absent'] or 'なし'}")
    print(" Qごとの得点 " + " / ".join(f"{ab} {'-'.join(map(str, v))}={g['final'][ab]}" for ab, v in g["q_scores"].items()))
    for ab, ps in g["players"].items():
        print(f" {ab}: " + "; ".join(f"{p['name']} {p['PTS']}点(Q:{','.join(map(str, p['q_pts']))}) {p['REB']}R {p['AST']}A {p['STL']}S {p['BLK']}B FG{p['FG']} 3P{p['3PT']} FT{p['FT']} {p['MIN']}分" for p in ps[:4]))
