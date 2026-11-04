#!/usr/bin/env python3
"""採点のテスト。採点をいじったときに、別のところが知らないうちに壊れていないかを確かめる。

- 本物の3試合(fixtures/)を採点して、前回確かめた点数(expected.json)と同じになるか
- ランク付け(docs/tiers.js)・大爆発/覚醒・スタメン欠場・一言の理由・見方の目安を、作った例で確かめる

ルールや重みをわざと変えたときは、点数が変わるのが正しいので、確かめたうえで
    python3 tests/run_tests.py --update
で expected.json を作り直す。失敗したら自動更新はそこで止まり、アプリは前の状態のまま残る。
"""
import gzip, json, os, sys, copy, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import collect as C  # noqa: E402

EXPECTED = os.path.join(HERE, "expected.json")
FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}: 期待 {want!r} / 実際 {got!r}")


def snapshot():
    """本物の試合の採点結果(チームの今季成績など、日によって変わるものは使わない)"""
    out = {}
    for fn in sorted(os.listdir(os.path.join(HERE, "fixtures"))):
        with gzip.open(os.path.join(HERE, "fixtures", fn), "rt") as f:
            d = json.load(f)
        e = d["event"]
        rec = C.score_record(e, C.jst(e["date"]), d["summary"], {}, {})
        bd = rec["bd"]
        out[fn.split(".")[0]] = {
            "content": rec["content"], "flow": rec["flow"],
            "flow_items": sorted(x["txt"] for x in bd["flow"]["items"]),
            "stars": bd["stars"]["pts"], "card": bd["card"]["pts"], "record": bd["record"]["pts"],
            "star_players": sorted(x["who"] for x in bd["stars"]["items"] if x.get("k") == "star"),
        }
    return out


def test_snapshot(update):
    got = snapshot()
    if update or not os.path.exists(EXPECTED):
        json.dump(got, open(EXPECTED, "w"), ensure_ascii=False, indent=1)
        print("expected.json を作り直した")
        return
    want = json.load(open(EXPECTED))
    for k in want:
        for f in want[k]:
            check(f"{k}.{f}", got.get(k, {}).get(f), want[k][f])


def rec(i, date, score, season=2027, typ="regular", **kw):
    r = {"id": str(1000 + i), "date": date, "season": season, "type": typ, "content": score, "legend": False,
         "buzz": {"d1": None, "d7": None, "d30": None}, "home": "AAA", "away": "BBB"}
    r.update(kw)
    return r


def test_tiers():
    # 同じ週の40試合。点数の高い試合ほど上のランクになるか、C未満はランク外か
    # (週や月で比べる範囲が変わるので、ランクの順番が点数どおりになるのは同じ週の中だけ)
    today = dt.date(2027, 2, 28)
    rs = [rec(i, f"2027-02-{1 + i % 7:02d}", 10 + i * 2.5) for i in range(40)]
    C.assign_tiers(rs, {}, today)
    by = {r["content"]: r["tier"] for r in rs}
    check("tiers.top", by[107.5], "SS")
    check("tiers.low", by[20], "-")
    check("tiers.c", by[30], "C")
    order = {"-": 0, "C": 1, "B": 2, "A": 3, "S": 4, "SS": 5}
    srt = sorted(rs, key=lambda r: r["content"])
    check("tiers.monotonic", all(order[a["tier"]] <= order[b["tier"]] for a, b in zip(srt, srt[1:])), True)
    # 歴史的記録は点数に関係なくSS候補
    rs2 = [rec(i, "2027-02-10", 20) for i in range(5)]
    rs2[0]["legend"] = True
    C.assign_tiers(rs2, {}, today)
    check("tiers.legend", rs2[0]["tier"], "SS")


def star_item(pid, raw, mult=0.2, base=25):
    return {"k": "star", "id": pid, "who": "Test Player", "team": "AAA", "why": ["30点・高効率"], "base": base,
            "mult": mult, "pts": round(base * mult, 1), "raw": raw}


def game(i, date, pid, pts, item=None):
    r = rec(i, date, 30)
    r["content_raw"] = 30
    r["lineup"] = {"AAA": {"st": {}, "pl": [pid], "out": [], "pts": {pid: ["Test Player", pts]}}}
    r["bd"] = {"stars": {"pts": item["pts"] if item else 0, "cap": 50, "items": [item] if item else []},
               "flow": {"pts": 0, "items": []}}
    return r


def test_surprise():
    # 平均10点の選手(格=その他×0.2)が30点 → 1回目はエース級(×0.4)、2試合連続でAS級(×0.7)
    rs = [game(i, f"2027-01-0{i + 1}", "p1", 10) for i in range(4)]
    rs.append(game(4, "2027-01-05", "p1", 30, star_item("p1", 30)))
    rs.append(game(5, "2027-01-06", "p1", 31, star_item("p1", 31)))
    rs.append(game(6, "2027-01-07", "p1", 12))
    rs.append(game(7, "2027-01-08", "p1", 40, star_item("p1", 40)))   # 連続が切れたあと、また大爆発 → 1回目扱い
    C.apply_surprise(rs)
    m = [r["bd"]["stars"]["items"][0]["mult"] if r["bd"]["stars"]["items"] else None for r in rs]
    check("surprise.first", m[4], C.CFG["tier_mult"]["surprise1"])
    check("surprise.streak", m[5], C.CFG["tier_mult"]["surprise"])
    check("surprise.after_break", m[7], C.CFG["tier_mult"]["surprise1"])
    check("surprise.content", rs[4]["content"], round(30 + 25 * (C.CFG["tier_mult"]["surprise1"] - 0.2), 1))
    # 2回計算しても同じ(キャッシュした記録から作り直しても点数が増えない)
    before = [r["content"] for r in rs]
    C.apply_surprise(rs)
    check("surprise.idempotent", [r["content"] for r in rs], before)


def test_absence():
    # いつもスタメンの2人が欠けた試合は −6(展開30以上なら半分)
    def g(i, starters, flow=0):
        r = rec(i, f"2027-01-{10 + i:02d}", 40, flow=flow)
        r["content_base"] = 40
        r["home"], r["away"] = "AAA", "BBB"
        r["lineup"] = {"AAA": {"st": {p: p for p in starters}, "pl": list(starters), "out": []},
                       "BBB": {"st": {}, "pl": [], "out": []}}
        return r
    rs = [g(i, ["x1", "x2", "x3", "x4", "x5"]) for i in range(4)]
    rs.append(g(4, ["x3", "x4", "x5", "y1", "y2"]))
    rs.append(g(5, ["x3", "x4", "x5", "y1", "y2"], flow=35))
    C.apply_absence(rs)
    check("absence.two_out", rs[4]["absence"], 2 * C.P["starter_out"])
    check("absence.soften", rs[5]["absence"] <= 0 and rs[5]["absence"] > 2 * C.P["starter_out"] - 1, True)


def test_text():
    out = {"flow": {"items": [{"txt": "クラッチ(終盤の接戦)", "pts": 20}, {"txt": "最後の攻撃までもつれた", "pts": 15}]},
           "stars": {"pts": 35, "items": [dict(star_item("p", 41, 1.0, 35), who="Luka Doncic", why=["40点以上"])]},
           "card": {"pts": 10, "items": []}, "record": {"pts": 0}, "absence": {"pts": 0}, "buzz": {"pts": 0},
           "moment": {"pts": 0}}
    check("why.close+star", C.why_line(out), "最後までもつれた接戦+ドンチッチの40点超え")
    check("guide.top_late", C.watch_guide(out, "A").startswith("本編を最初から"), True)
    low = copy.deepcopy(out)
    low["flow"]["items"] = []
    low["stars"] = {"pts": 0, "items": []}
    check("why.none", C.why_line(low), "見せ場が少なかった")


def main():
    update = "--update" in sys.argv
    for t in (lambda: test_snapshot(update), test_tiers, test_surprise, test_absence, test_text):
        try:
            t()
        except Exception as e:
            FAILS.append(f"{getattr(t, '__name__', 'snapshot')}: エラー {type(e).__name__}: {e}")
    if FAILS:
        print("テスト失敗:")
        for f in FAILS:
            print(" -", f)
        sys.exit(1)
    print("テストOK")


if __name__ == "__main__":
    main()
