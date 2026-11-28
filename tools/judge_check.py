#!/usr/bin/env python3
"""Claudeの判定(scripts/claude_judge.json)が決まりを守っているか確かめる。OKなら「OK」とだけ出す。
  python3 tools/judge_check.py
"""
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
S = os.path.join(HERE, "..", "scripts")
RK = {"-": 0, "C": 1, "B": 2, "A": 3, "S": 4, "SS": 5}
cfg = json.load(open(os.path.join(S, "config.json")))
steps = cfg["judge"]["claude_max_steps"]
cj = json.load(open(os.path.join(S, "claude_judge.json")))
owner = json.load(open(os.path.join(S, "judgments.json"))).get("owner", [])
case_ids = {o["id"] for o in owner}
try:
    arch = {r["id"]: r for r in json.load(open(os.path.join(S, "archive.json")))}
except Exception:
    arch = {}
bad = []
SCORE = re.compile(r"\d{2,3}\s*[-–対]\s*\d{2,3}")
RESULT = re.compile(r"勝った|負けた|勝利|敗れ|敗戦|勝ち越し|白星|黒星")
for gid, c in (cj.get("claude") or {}).items():
    if not str(gid).isdigit():
        bad.append(f"{gid}: 試合idが数字でない")
        continue
    if not isinstance(c, dict) or not c.get("at"):
        bad.append(f"{gid}: at(日付)がない")
        continue
    to = c.get("to")
    if to is None:
        continue   # 上げないと決めた記録
    if to not in RK:
        bad.append(f"{gid}: ランク {to!r} がおかしい")
        continue
    like = c.get("like") or []
    if not like:
        bad.append(f"{gid}: 上げるなら似ている判例(like)が要る")
    for x in like:
        if x not in case_ids:
            bad.append(f"{gid}: 判例 {x} がない")
    why = str(c.get("why") or "")
    if not why or len(why) > 150:
        bad.append(f"{gid}: 理由は1〜150字({len(why)}字)")
    if SCORE.search(why) or RESULT.search(why):
        bad.append(f"{gid}: 理由に最終スコアか勝ち負けが入っている")
    r = arch.get(str(gid))
    if r:
        calc = r.get("j_from") or r.get("tier")
        if calc in RK and RK[to] <= RK[calc]:
            bad.append(f"{gid}: 今の計算({calc})より上げていない(上げないなら to は null)")
        if calc in RK and RK[to] - RK[calc] > steps:
            bad.append(f"{gid}: {steps}段より多く上げている({calc}→{to})")
for i, r in enumerate(cj.get("rules") or []):
    if not r.get("tags") or not r.get("text") or r.get("status") not in ("proposed", "adopted", "rejected"):
        bad.append(f"rules[{i}]: tags・text・status(proposed/adopted/rejected)が要る")
if bad:
    print("\n".join(bad))
    sys.exit(1)
print("OK")
