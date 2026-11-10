#!/usr/bin/env python3
"""Claudeが書いた言葉(scripts/lines.json)の形と決まりを確かめる。問題があれば一覧を出して終了コード1。
- 形:k / guide / why / at がある。長さは guide 60字まで・why 150字まで
- 見方の一言(guide):選手名・スコア・勝ち負けを書かない(1段目=スコアと勝敗を出さない段に出るので)
- 理由(why):最終スコアと「勝った・負けた」を書かない
- k が今の材料(material.json)の見方の種類と同じ(違うと画面では決まり文句に戻る)
"""
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
S = os.path.join(HERE, "..", "scripts")
lines = json.load(open(os.path.join(S, "lines.json")))
mat = {str(g["id"]): g for g in json.load(open(os.path.join(S, "material.json")))} if os.path.exists(os.path.join(S, "material.json")) else {}
KEYS = {"full_comeback", "full_or_q4", "full", "q4", "half", "hl_star", "hl", "skip"}
WIN = re.compile(r"勝っ|負け|勝利|敗れ|白星|黒星|制した|下した")
bad = []
for gid, ln in lines.items():
    for f in ("k", "guide", "why", "at"):
        if not ln.get(f):
            bad.append(f"{gid}: {f} がない")
    g, w = ln.get("guide", ""), ln.get("why", "")
    if ln.get("k") not in KEYS:
        bad.append(f"{gid}: k が知らない種類")
    if len(g) > 60 or len(w) > 150:
        bad.append(f"{gid}: 長すぎ(guide {len(g)} / why {len(w)})")
    if re.search(r"[A-Za-z]{3,}", g) or re.search(r"\d+点|\d+\s*[-ー対]\s*\d+", g) or WIN.search(g):
        bad.append(f"{gid}: 見方の一言に選手名・点数・勝ち負けが入っている")
    if WIN.search(w):
        bad.append(f"{gid}: 理由に勝ち負けが書いてある")
    m = mat.get(gid)
    if m:
        if ln.get("k") != m["gk"]:
            bad.append(f"{gid}: k={ln.get('k')} だが今の見方の種類は {m['gk']}")
        fin = sorted(m["final"].values())
        if re.search(rf"{fin[1]}\s*[-ー対]\s*{fin[0]}|{fin[0]}\s*[-ー対]\s*{fin[1]}", w):
            bad.append(f"{gid}: 理由に最終スコアが書いてある")
gs = [ln.get("guide", "") for ln in lines.values()]
dup = {x for x in gs if gs.count(x) > 2}
for x in dup:
    bad.append(f"同じ見方の一言が3回以上: {x}")
print("\n".join(bad) if bad else f"OK: {len(lines)}試合ぶん")
sys.exit(1 if bad else 0)
