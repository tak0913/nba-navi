#!/usr/bin/env python3
"""Claudeが判定(tools/JUDGE.md)をするための材料を、小さく出す。

先に scripts で  OUT_DIR=../docs python3 collect.py --rebuild --no-buzz  を動かしておく(archive.json と material.json ができる)。
  python3 tools/judge_material.py        判例・直された記録・ルールにする候補・まだ判定していない直近3日の試合
注意:ここに出る数字は試合の結果そのもの(ネタバレ)。報告には書かない。
"""
import json, os, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
S = os.path.join(HERE, "..", "scripts")


def load(name, default):
    try:
        return json.load(open(os.path.join(S, name)))
    except Exception:
        return default


cfg = load("config.json", {})
owner = load("judgments.json", {}).get("owner", [])
cj = load("claude_judge.json", {})
mine = cj.get("claude", {})
arch = {r["id"]: r for r in load("archive.json", [])}
mat = {str(g["id"]): g for g in load("material.json", [])}
RK = {"-": 0, "C": 1, "B": 2, "A": 3, "S": 4, "SS": 5}

# 同じ試合は持ち主の最後の1件
last = {}
for o in sorted(owner, key=lambda o: o.get("at") or ""):
    last[str(o["gid"])] = o
cases = list(last.values())

print(f"== 判例(持ち主の判定){len(cases)}件 ==")
if not cases:
    print("まだない。判例がないうちは判定しない(tools/JUDGE.md)")
for o in cases:
    sn = o.get("snap") or {}
    calc = sn.get("calc") or "?"
    print(f"[{o['id']}] {o.get('game') or sn.get('game')} 計算{calc}→持ち主{o['to']} 何がすごかった?:{'・'.join(o.get('tags') or []) or 'なし'} ひとこと:{o.get('text') or 'なし'}")
    if sn:
        print(f"   様子: スター {', '.join(sn.get('stars') or []) or 'なし'} / 展開 {', '.join(sn.get('flow') or []) or 'なし'}"
              f" / 記録 {', '.join(sn.get('record') or []) or 'なし'} / 話題の選手 {', '.join(sn.get('spot') or []) or 'なし'}")
    else:
        print("   様子: まだ付いていない(次の更新で付く)")

# 直された記録:Claudeが判定したあとに、持ち主が同じ試合を判定した
fixed = []
for gid, c in mine.items():
    o = last.get(str(gid))
    if o and (o.get("at") or "") >= (c.get("at") or ""):
        fixed.append((gid, c, o))
print(f"\n== 直された記録 {len(fixed)}件(Claudeが判定した{sum(1 for c in mine.values() if c.get('to'))}件のうち) ==")
for gid, c, o in fixed:
    print(f"[{gid}] Claude {c.get('to') or '上げない'}(似ている判例 {','.join(c.get('like') or []) or 'なし'})→ 持ち主 {o['to']}。持ち主のひとこと:{o.get('text') or 'なし'}")

# ルールにする候補:同じ「何がすごかった?」で上げた判例が rule_after 件以上、まだ提案していないもの
need = (cfg.get("judge") or {}).get("rule_after", 3)
done = {t for r in cj.get("rules", []) for t in (r.get("tags") or [])}
cnt = {}
for o in cases:
    calc = (o.get("snap") or {}).get("calc")
    if calc in RK and RK[o["to"]] > RK[calc]:
        for t in o.get("tags") or []:
            cnt.setdefault(t, []).append(o["id"])
cand = {t: ids for t, ids in cnt.items() if len(ids) >= need and t not in done}
print(f"\n== ルールにする候補({need}件以上たまった種類){len(cand)} ==")
for t, ids in cand.items():
    print(f"{t}: {', '.join(ids)}")

# まだ判定していない直近3日の試合(持ち主が判定した試合は除く)
today = max((r["date"] for r in arch.values()), default="")
lim = (dt.date.fromisoformat(today) - dt.timedelta(days=3)).isoformat() if today else ""
todo = [r for r in arch.values() if r["date"] >= lim and r["id"] not in mine and r["id"] not in last and r.get("bd")]
print(f"\n== まだ判定していない試合 {len(todo)}(直近3日) ==")
for r in sorted(todo, key=lambda r: r["date"]):
    bd = r["bd"]
    tier = r.get("j_from") or r.get("tier") or "?(推しで隠し中)"
    st = [f"{x['who']}({x['team']}) {'+'.join(x['why'])} {x['pts']}点" for x in bd["stars"]["items"] if x.get("k") == "star"]
    sp = [f"{w['who']} いつもの{w['ratio']}倍" for w in (r.get("spot") or {}).get("who", [])]
    print(f"[{r['id']}] {r['date']} {r['away']}@{r['home']} 今の計算{tier}")
    print(f"   スター {', '.join(st) or 'なし'} / 展開 {', '.join(x['txt'] for x in bd['flow']['items']) or 'なし'}"
          f" / 記録 {', '.join(x.get('who', '') for x in bd['record']['items']) or 'なし'} / 話題の選手 {', '.join(sp) or 'なし'}")
    g = mat.get(r["id"])
    if g:
        for ab, ps in g["players"].items():
            print(f"   {ab}: " + "; ".join(f"{p['name']} {p['PTS']}点 {p['REB']}R {p['AST']}A {p['STL']}S {p['BLK']}B FG{p['FG']} {p['MIN']}分" for p in ps[:3]))
