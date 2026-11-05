# 答え合わせ:昨季の記録(scripts/archive_prev.json)を「今/案A/案B」で採点し直し、ファン投票の名勝負と比べる
# fans_2025_26.txt = wikihoops.com Top Games 2025-26 の上位50(日付はアメリカ東部|アウェイ|ホーム|票)
# 使い方:cd scripts && python3 collect.py --backfill-prev(約15分)→ python3 ../tools/compare_fans.py
import json, subprocess, datetime as dt, sys
import os
HERE = os.path.dirname(os.path.abspath(__file__))
S = os.path.join(HERE, "..", "scripts")   # archive_prev.json は collect.py --backfill-prev で作る
recs = [r for r in json.load(open(f"{S}/archive_prev.json")) if r["type"] == "regular" and r.get("cup") != "final"]
cfg = json.load(open(f"{S}/config.json"))
P = cfg["points"]

def flow_raw(r): return sum(i["pts"] for i in r["bd"]["flow"]["items"])
def has_buzzer(r): return any(i["txt"].startswith("ブザー") for i in r["bd"]["flow"]["items"])

def variant(r, v):
    raw, old = flow_raw(r), r["flow"]
    if v == "now": new = old
    elif v == "A": new = (min(P["flow_cap"], raw - P["buzzer"]) + P["buzzer"]) if has_buzzer(r) else old
    elif v == "B": new = P["flow_cap"] + (raw - P["flow_cap"]) / 2 if raw > P["flow_cap"] else old
    return r["content"] + (new - old)

def tiers(scores):
    rows = [{"id": r["id"], "date": r["date"], "season": r["season"], "type": "regular", "score": scores[r["id"]],
             "legend": r.get("legend", False), "hall_ok": False} for r in recs]
    job = {"jobs": [{"records": rows, "opts": {"T": cfg["tiers"], "R": cfg["rules"], "prev": {}, "today": "2026-11-06", "upgrades": {}}}]}
    out = subprocess.run(["node", f"{S}/tiers_cli.js"], input=json.dumps(job), capture_output=True, text=True, check=True).stdout
    return {x["id"]: x for x in json.loads(out)[0]}

# 民衆の名勝負(wikihoops・ファン投票の上位50。日付はアメリカ東部)
fans = []
for line in open(os.path.join(HERE, "fans_2025_26.txt")):
    d, a, b, votes = line.strip().split("|")
    fans.append((d, a, b, int(votes)))
def find(d, a, b):
    days = {d, (dt.date.fromisoformat(d) + dt.timedelta(days=1)).isoformat()}
    for r in recs:
        if {r["home"], r["away"]} == {a, b} and r["date"] in days:
            return r
fan_ids = {}
for d, a, b, v in fans:
    r = find(d, a, b)
    if r: fan_ids[r["id"]] = v

res = {}
for v in ("now", "A", "B"):
    sc = {r["id"]: variant(r, v) for r in recs}
    tr = tiers(sc)
    order = sorted(recs, key=lambda r: (-sc[r["id"]], r["id"]))
    rank = {r["id"]: i + 1 for i, r in enumerate(order)}
    top50 = {r["id"] for r in order[:50]}
    dist = {}
    for i in fan_ids: dist[tr[i]["tier"]] = dist.get(tr[i]["tier"], 0) + 1
    med = sorted(rank[i] for i in fan_ids)[len(fan_ids) // 2]
    res[v] = dict(sc=sc, tr=tr, rank=rank, hit=len(top50 & set(fan_ids)), dist=dist, med=med,
                  nS=sum(1 for x in tr.values() if x["tier"] in ("S", "SS")))
print(f"昨季レギュラーシーズン {len(recs)}試合 / ファン投票上位50のうち照合できた {len(fan_ids)}試合")
for v in ("now", "A", "B"):
    x = res[v]
    print(v, "ファン上位50と自分の上位50の重なり:", x["hit"], "/ ファンの名勝負の順位の真ん中:", x["med"], "位",
          "/ ファンの名勝負のランク:", dict(sorted(x["dist"].items())), "/ S以上の数:", x["nS"])
# 動いた試合
for v in ("A", "B"):
    ch = [r for r in recs if res[v]["tr"][r["id"]]["tier"] != res["now"]["tr"][r["id"]]["tier"]]
    print(f"\n案{v}でランクが変わった試合: {len(ch)}")
    for r in sorted(ch, key=lambda r: r["date"]):
        i = r["id"]
        print(f"  {r['date']} {r['away']}@{r['home']}  {res['now']['tr'][i]['tier']}→{res[v]['tr'][i]['tier']}"
              f"  {res['now']['sc'][i]:.1f}→{res[v]['sc'][i]:.1f}  ファン票:{fan_ids.get(i, '-')}")
# 昨季のブザービーター試合の数と上限
bz = [r for r in recs if has_buzzer(r)]
print("\nブザービーターの試合:", len(bz), " うち展開が上限を超えていた:", sum(1 for r in bz if flow_raw(r) > P["flow_cap"]))
print("展開の素点が上限を超えた試合:", sum(1 for r in recs if flow_raw(r) > P["flow_cap"]))

print("\n--- ファンが選んだのに今の計算で低い試合(B以下) ---")
byid = {r["id"]: r for r in recs}
low = [i for i in fan_ids if res["now"]["tr"][i]["tier"] in ("B", "C", "-")]
for i in sorted(low, key=lambda i: -fan_ids[i]):
    r = byid[i]; bd = r["bd"]
    fl = "+".join(x["txt"][:6] for x in bd["flow"]["items"]) or "なし"
    st = ",".join(f"{x['who'].split()[-1]}{x.get('raw','')}" for x in bd["stars"]["items"] if x.get("k") == "star") or "なし"
    print(f"票{fan_ids[i]:>3} {r['away']}@{r['home']} {res['now']['tr'][i]['tier']} {r['content']:.0f}点 展開{r['flow']}[{fl}] 星{bd['stars']['pts']}[{st}] 格{bd['card']['pts']} 欠場{r.get('absence',0)}")
print("\n--- 自分は上位50なのにファンの50に入っていない試合の特徴 ---")
order = sorted(recs, key=lambda r: (-res['now']['sc'][r['id']], r['id']))[:50]
import collections
cnt = collections.Counter()
for r in order:
    if r["id"] in fan_ids: continue
    for x in r["bd"]["flow"]["items"]: cnt[x["txt"]] += 1
print(cnt.most_common())
