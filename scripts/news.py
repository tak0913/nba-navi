#!/usr/bin/env python3
"""移籍ニュース(トレード)を集める。

- 確定:ESPNの「選手の移籍・契約の一覧」(transactions)から、トレード(Acquired / Traded / trade)だけ拾う
- 報道:ESPNのニュース記事のうち、書いた人が Shams Charania で、見出しがトレードの話のものだけ拾う
  (Xは使えないので、シャムズのESPN記事で代わりにする。Xの速報が全部記事になるわけではない)

ネタバレ防止:
- 報道は見出しだけ使う(本文・説明文は使わない)。見出しにスコアや勝ち負けの言葉があれば、その記事は出さない(迷ったら出さない)
- 移籍の一覧には試合結果は書かれない

入力:  stars.json, names_ja.json, config.json, news_state.json(前回までに拾ったもの)
出力:  OUT_DIR/news.json(画面が読む), news_push.json(通知の文面。チャンネルごと), news_alert.txt(持ち主への知らせ)
"""
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "news_state.json")
BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
KEEP_DAYS = 60          # 画面に出す・覚えておく日数
PUSH_HOURS = 48         # これより古いものは通知しない(取りこぼしを後から送らない)


def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1 + 2 * i)


def load_json(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def norm(s):
    return re.sub(r"[^a-z]", "", (s or "").lower())


# ---------- チーム ----------
def teams():
    """{abbr: {"full": "Houston Rockets", "nick": "Rockets", "city": "Houston"}}"""
    d = get(f"{BASE}/teams")
    out = {}
    for t in d["sports"][0]["leagues"][0]["teams"]:
        t = t["team"]
        out[t["abbreviation"]] = {"full": t["displayName"], "nick": t.get("name") or "", "city": t.get("location") or ""}
    return out


def find_team(text, tinfo):
    """「Chicago Bulls」「Charlotte」「the L.A. Clippers」などからチームを探す。見つからなければ None"""
    # 単語で比べる(「Hornets」の中の「nets」でネッツにしないように)
    w = lambda s: " " + " ".join(re.findall(r"[a-z0-9]+", (s or "").lower())) + " "
    n = w(text)
    for ab, t in tinfo.items():
        if t["nick"] and w(t["nick"]) in n:
            return ab
    for ab, t in tinfo.items():
        if t["city"] and n.startswith(w(t["city"])):
            return ab
    return None


def teams_in(text, tinfo):
    """見出しに出てくるチーム(ニックネームで探す。Rockets, Lakers など)"""
    out = []
    for ab, t in tinfo.items():
        if t["nick"] and re.search(r"\b" + re.escape(t["nick"]) + r"\b", text):
            out.append(ab)
    return out


# ---------- 確定したトレード(移籍の一覧) ----------
POS = re.compile(r"\b(?:PG|SG|SF|PF|Gs|Fs|Cs|G|F|C)\s+(?=[A-Z])")


NUM = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


def _pick(m):
    n = NUM.get((m.group(1) or "").lower())
    year = f"{m.group(2)}年の" if m.group(2) else ""
    name = "1巡目指名権" if m.group(3).lower() == "first" else "2巡目指名権"
    swap = "の交換" if m.group(4) else ""
    return year + name + (f"×{n}" if n and n > 1 else "") + swap


def ja_assets(s):
    s = POS.sub("", s.strip().rstrip("."))
    s = re.sub(r"\bcash considerations?\b", "金銭", s)
    s = re.sub(r"\bdraft considerations?\b", "ドラフト指名権など", s)
    s = re.sub(r"\b(?:(a|one|two|three|four|five|six|seven|eight|nine|ten)\s+)?(?:(\d{4})\s+)?(first|second)[- ]round (?:draft )?picks?( swap)?\b",
               _pick, s, flags=re.I)
    s = re.sub(r"\band\b", "・", s).replace(", ", "・").replace(",", "・")
    return re.sub(r"\s*・\s*", "・", s).strip("・ ")


def sentences(desc):
    """文に分ける。「Jr.」「Sr.」「L.A.」の点で切らないように先に点を外す"""
    s = re.sub(r"\b(Jr|Sr|St|Mr)\.", r"\1", desc)
    s = s.replace("L.A.", "LA")
    return [x.strip() for x in re.split(r"\.(?:\s+|$)", s) if x.strip()]


ACQ = re.compile(r"^Acquired (.+?) from (?:the )?(.+?)(?: (?:in exchange )?for (.+))?$")
TRD = re.compile(r"^Traded (.+?) to (?:the )?(.+?)(?: (?:in exchange )?for (.+))?$")
IS_TRADE = re.compile(r"\bAcquired\b|\bTraded\b|\btrade\b")   # 移籍の一覧のうち、トレードの行


def _moves(team, desc, tinfo):
    """トレードの文を「誰が・何を・誰から・見返りに何を」に直す。[(手に入れたチーム, 何を, 出したチーム, 見返り)]
    ESPNの書き方は2つある:
      手に入れた側の行「Acquired G X from Chicago Bulls for G Y.」
      出した側の行    「Traded G X to the Oklahoma City Thunder.」(手に入れた側の行が出ないことがある。11/8 Nique Clifford)
    トレードの文が全部きれいに読めたときだけ返す。1つでも読めなければ None(英語の原文だけ出す)"""
    out = []
    for s in sentences(desc):
        if s.startswith("Acquired"):
            m = ACQ.match(s)
        elif s.startswith("Traded"):
            m = TRD.match(s)
        else:
            continue
        if not m:
            return None
        what, other, give = m.group(1), m.group(2), m.group(3)
        if " in a " in other or " and " in other or (give and " in a " in give):   # 三角トレードなどは原文だけ
            return None
        oab = find_team(other, tinfo)
        if not oab:
            return None
        out.append((team, what, oab, give) if s.startswith("Acquired") else (oab, what, team, give))
    return out or None


def trade_ja(team, desc, tinfo, ja):
    """「Acquired G X from Chicago Bulls for G Y.」→「ホーネッツが X を獲得(ブルズから。見返り:Y)」
    「Traded G X to the Oklahoma City Thunder.」(キングスの行)→「サンダーが X を獲得(キングスから)」
    トレードの文が全部きれいに読めたときだけ日本語にする。1つでも読めなければ None(英語の原文だけ出す)
    戻り値:(日本語, [相手チーム])"""
    mv = _moves(team, desc, tinfo)
    if not mv:
        return None
    parts, others = [], []
    for getter, what, giver, give in mv:
        t = f"{ja_assets(what)} を獲得({ja.get(giver, giver)}から"
        if give:
            t += f"。見返り:{ja_assets(give)}"
        parts.append((getter, t + ")"))
        other = giver if getter == team else getter
        if other not in others:
            others.append(other)
    if all(g == parts[0][0] for g, _ in parts):
        g = parts[0][0]
        return f"{ja.get(g, g)}が " + "/".join(t for _, t in parts), others
    return "/".join(f"{ja.get(g, g)}が {t}" for g, t in parts), others


def trade_sides(team, desc, tinfo):
    """どのチームが何を手に入れたか。{"CHA": ["Rob Dillingham"], "CHI": ["Buddy Hield"]}
    trade_ja と同じく、トレードの文が全部きれいに読めたときだけ。読めなければ None"""
    mv = _moves(team, desc, tinfo)
    if not mv:
        return None
    sides = {}
    add = lambda ab, txt: sides.setdefault(ab, []).extend(x for x in ja_assets(txt).split("・") if x and x not in sides.get(ab, []))
    for getter, what, giver, give in mv:
        add(getter, what)
        if give:
            add(giver, give)
        else:
            sides.setdefault(giver, [])
    return sides


# ---------- 同じトレードの話を1つにまとめる ----------
def _day(d):
    try:
        return dt.date.fromisoformat(d[:10])
    except Exception:
        return None


def _near(a, b, days):
    da, db = _day(a), _day(b)
    return bool(da and db and abs((da - db).days) <= days)


PICK_WORDS = ("指名権", "金銭")


def _players(sides):
    return [x for v in (sides or {}).values() for x in v if not any(w in x for w in PICK_WORDS)]


def deal_ja(sides, teams_order, ja):
    """「ブルズ ⇄ ホーネッツ:ブルズ ← Buddy Hield/ホーネッツ ← Rob Dillingham」(通知・古い画面用)"""
    tm = [t for t in teams_order if t in sides]
    head = (" ⇄ " if len(tm) == 2 else "・").join(ja.get(t, t) for t in tm)
    body = "/".join(f"{ja.get(t, t)} ← {'・'.join(sides[t]) or '(原文に書いてない)'}" for t in tm)
    return f"{head}:{body}"


def merge(items, tinfo, ja):
    """移籍の一覧はチームごとに1行ずつ出る(1つのトレードが2〜3行になる)ので1枚にまとめる。
    ・確定:日付が1日以内で、チームが2つ以上重なるものは同じトレード
    ・報道:同じトレードの5日前〜1日後で、チームが2つ重なるか、動いた選手の名前が見出しにあれば、そのトレードの下に付ける
    ・報道どうし:3日以内でチームが2つ以上重なれば1枚に(新しい見出しが上)
    戻り値:画面に出す項目(新しい順)。members に元の項目の id"""
    trades = sorted([i for i in items if i["kind"] == "trade"], key=lambda x: (x["date"], x["id"]))
    reports = sorted([i for i in items if i["kind"] == "report"], key=lambda x: (x["time"], x["id"]), reverse=True)
    tw = {x for t in tinfo.values() for x in re.findall(r"[A-Za-z]+", (t.get("city") or "") + " " + (t.get("nick") or ""))}
    tw |= {"Acquired", "Traded", "Received", "Waived", "Re", "LA", "L", "A", "Guardians", "Oklahoma", "City"}

    def names(en):   # 原文に出てくる選手名らしきもの(大文字で始まる2語以上。チーム名は除く)
        out = set()
        for m in re.finditer(r"[A-Z][\w'’.-]+(?:\s+[A-Z][\w'’.-]+)+", en):
            ws = [w for w in m.group(0).split() if w.strip(".") not in tw and w not in ("G", "F", "C", "Gs", "Fs", "Cs", "PG", "SG", "SF", "PF")]
            if len(ws) >= 2:
                out.add(norm(" ".join(ws)))
        return out

    def same(a, b):
        if not _near(a["date"], b["date"], 1):
            return False
        share = set(a["teams"]) & set(b["teams"])
        # チームが2つ重なる、または同じ選手が両方の行に出てきてチームも1つ重なる
        return len(share) >= 2 or (share and names(a["en"]) & names(b["en"]))

    groups = []
    for it in trades:
        for g in groups:
            if any(same(m, it) for m in g):
                g.append(it)
                break
        else:
            groups.append([it])
    out = []
    for g in groups:
        teams_order = []
        for m in g:
            teams_order += [t for t in m["teams"] if t not in teams_order]
        sides = {}
        for m in g:
            sd = trade_sides(m["teams"][0], m["en"], tinfo)
            if sd is None:
                sides = None
                break
            for t, v in sd.items():
                sides.setdefault(t, [])
                sides[t] += [x for x in v if x not in sides[t]]
        ens = []
        for m in g:
            if m["en"] not in ens:
                ens.append(m["en"])
        out.append({"id": "d:" + hashlib.md5("|".join(sorted(m["id"] for m in g)).encode()).hexdigest()[:12],
                    "kind": "trade", "date": min(m["date"] for m in g), "time": max(m["time"] for m in g),
                    "teams": teams_order, "sides": sides,
                    "ja": deal_ja(sides, teams_order, ja) if sides else None,
                    "en": " / ".join(ens), "ens": ens, "url": None, "reports": [],
                    "members": [m["id"] for m in g]})
    rgroups = []
    for r in reports:
        deal = None
        for d in out:
            if not (_near(r["date"], d["date"], 5) and r["date"] <= (_day(d["date"]) + dt.timedelta(days=1)).isoformat()):
                continue
            if len(set(r["teams"]) & set(d["teams"])) >= 2 or any(norm(p) and norm(p) in norm(r["en"]) for p in _players(d["sides"])):
                deal = d
                break
        if deal:
            deal["reports"].append({"en": r["en"], "url": r.get("url"), "time": r["time"]})
            deal["members"].append(r["id"])
            continue
        for g in rgroups:
            if any(_near(m["date"], r["date"], 3) and len(set(m["teams"]) & set(r["teams"])) >= 2 for m in g):
                g.append(r)
                break
        else:
            rgroups.append([r])
    for g in rgroups:
        top = g[0]
        teams_order = []
        for m in g:
            teams_order += [t for t in m["teams"] if t not in teams_order]
        out.append({**top, "id": "g:" + hashlib.md5("|".join(sorted(m["id"] for m in g)).encode()).hexdigest()[:12] if len(g) > 1 else top["id"],
                    "teams": teams_order, "more": [{"en": m["en"], "url": m.get("url"), "time": m["time"]} for m in g[1:]],
                    "members": [m["id"] for m in g]})
    return sorted(out, key=lambda x: (x["date"], x["time"], x["id"]), reverse=True)


def fetch_trades(tinfo, ja, season=None):
    url = f"{BASE}/transactions?limit=500" + (f"&season={season}" if season else "")
    d = get(url)
    out = []
    for t in d.get("transactions", []):
        desc = (t.get("description") or "").strip()
        if not IS_TRADE.search(desc):
            continue
        ab = (t.get("team") or {}).get("abbreviation")
        if not ab:
            continue
        date = (t.get("date") or "")[:10]
        j = trade_ja(ab, desc, tinfo, ja)
        tm = [ab]
        if j:
            tm += [x for x in j[1] if x not in tm]
        else:   # 日本語にできなくても、相手チームは見出しから拾っておく(推しの絞り込み・通知のため)
            for s in sentences(desc):
                m = re.search(r" from (?:the )?([A-Z][\w.]*(?: [A-Z][\w.]*){0,2})", s)
                fab = find_team(m.group(1), tinfo) if m else None
                if fab and fab not in tm:
                    tm.append(fab)
        iid = "t:" + hashlib.md5(f"{date}|{ab}|{desc}".encode()).hexdigest()[:12]
        out.append({"id": iid, "kind": "trade", "date": date, "time": date + "T00:00:00Z", "teams": tm,
                    "ja": j[0] if j else None, "en": desc, "url": None})
    return out


# ---------- シャムズの報道(ESPNの記事) ----------
TRADE_WORDS = re.compile(r"\btrad(?:e|ed|es|ing)\b|\bacquir|\bswap\b|\bdeal\b|\bbuyout\b", re.I)
# 見出しに結果が混ざっていそうなら出さない(迷ったら出さない)
SPOILER = re.compile(
    r"\d{2,3}\s*[-–]\s*\d{2,3}|\b(?:win|wins|won|winning|loss|losses|lose|loses|lost|beat|beats|defeat\w*|rout\w*|rall(?:y|ies|ied)|"
    r"victor\w*|edge[sd]?|top(?:s|ped)|outlast\w*|streak|overtime|OT|game-winner|buzzer|blowout|comeback|points|"
    r"triple-double|career-high|leaves|exits|ejected|injur\w*)\b", re.I)


def is_shams(a):
    return "charania" in norm(a.get("byline"))


def fetch_reports(tinfo):
    d = get(f"{BASE}/news?limit=50")
    out = []
    for a in d.get("articles", []):
        h = (a.get("headline") or "").strip()
        if not is_shams(a) or not TRADE_WORDS.search(h) or SPOILER.search(h):
            continue
        t = a.get("published") or a.get("lastModified") or ""
        url = ((a.get("links") or {}).get("web") or {}).get("href")
        out.append({"id": f"r:{a.get('id')}", "kind": "report", "date": t[:10], "time": t, "teams": teams_in(h, tinfo),
                    "ja": None, "en": h, "url": url, "src": "Shams Charania(ESPN)"})
    return out


# ---------- エース表の選手が動いたか ----------
def star_moves(items, stars):
    """確定したトレードで、エース表の選手がよそのチームに獲得されていたら知らせる(エース表を手で直すため)"""
    where = {norm(p["name"]): (ab, p["name"]) for ab, lst in stars.items() if not ab.startswith("_") for p in lst}
    out = []
    for it in items:
        if it["kind"] != "trade":
            continue
        # 手に入れた側の行(Acquired … from)は teams[0] が新しいチーム。出した側の行(Traded … to)は teams[1]
        for pat, idx in ((r"Acquired (.+?) from ", 0), (r"Traded (.+?) to ", 1)):
            got = norm(" ".join(m.group(1) for s in sentences(it["en"]) for m in [re.match(pat, s)] if m))
            if not got or len(it["teams"]) <= idx:
                continue
            for key, (ab, name) in where.items():
                if key and key in got and it["teams"][idx] != ab:
                    out.append((it, name, ab, it["teams"][idx]))
    return out


def stars_in(it, stars):
    names = [p["name"] for ab, lst in stars.items() if not ab.startswith("_") for p in lst]
    s = norm(it["en"])
    return [n for n in names if norm(n) and norm(n) in s]


# ---------- 通知の文面 ----------
def line(it, ja):
    if it["kind"] == "trade":
        return "【確定】" + (it["ja"] or it["en"])
    return "【報道・シャムズ】" + it["en"]


def build_push(new, ch_keys, owner_fav, stars, ja):
    """チャンネルごとの文面。
    ・そのチャンネルの推しチームが絡むもの
    ・エース表の選手が動くトレード(確定)はみんなに
    持ち主(_owner)は持ち主の推しで同じように作る"""
    texts = {}
    for key in ["_owner"] + list(ch_keys):
        fav = owner_fav if key == "_owner" else (None if key == "none" else key.upper())
        lines = []
        for it in new:
            mine = fav and fav in it["teams"]
            big = it["kind"] == "trade" and stars_in(it, stars)
            if mine or big:
                lines.append(("【推し】" if mine else "") + line(it, ja))
        if lines:
            texts[key] = "移籍ニュース\n" + "\n".join(lines[:8]) + ("\n…ほか" if len(lines) > 8 else "")
    return texts


def main():
    now = dt.datetime.now(dt.timezone.utc)
    state = load_json(STATE, {})
    known = {it["id"]: it for it in state.get("items", [])}
    seeded = state.get("seeded", False)
    stars = load_json(os.path.join(HERE, "stars.json"), {})
    ja = load_json(os.path.join(HERE, "names_ja.json"), {}).get("teams", {})
    cfg = load_json(os.path.join(HERE, "config.json"), {})

    tinfo = teams()
    found, ok = [], 0
    for f in (lambda: fetch_trades(tinfo, ja), lambda: fetch_reports(tinfo)):
        try:
            found += f()
            ok += 1
        except Exception as e:
            print("取れなかった:", type(e).__name__)
    cut = (now - dt.timedelta(days=KEEP_DAYS)).date().isoformat()
    fresh = [it for it in found if it["date"] >= cut]
    new = [it for it in fresh if it["id"] not in known]
    for it in fresh:
        known[it["id"]] = it
    items = sorted([it for it in known.values() if it["date"] >= cut], key=lambda x: (x["time"], x["id"]), reverse=True)

    # 通知:初回(seeded=false)は今あるものを覚えるだけで送らない
    since = now - dt.timedelta(hours=PUSH_HOURS)
    def is_recent(it):   # 確定のトレードは日付しかないので日付で、報道は時刻で比べる
        if it["kind"] == "trade":
            return it["date"] >= since.date().isoformat()
        try:
            return dt.datetime.fromisoformat(it["time"].replace("Z", "+00:00")) >= since
        except Exception:
            return False
    # まとめた単位で送る:元の項目がすべて今回はじめて見たもので、最近のものだけ
    # (トレードの2つ目の行が後から来ても、同じトレードを2回送らない)
    deals = merge(items, tinfo, ja)
    new_ids = {it["id"] for it in new}
    byid = {it["id"]: it for it in items}
    recent = []
    for d in deals:
        mem = [byid[i] for i in d["members"] if i in byid]
        tr = [m for m in mem if m["kind"] == "trade"]
        base = tr or mem   # トレードが決まったら、前に報道を送っていても「確定」として送る
        if seeded and base and all(m["id"] in new_ids for m in base) and any(is_recent(m) for m in base):
            recent.append(d)
    ch_keys = ["none"] + [k.lower() for k in sorted(tinfo)]
    owner_fav = (cfg.get("favorites") or [None])[0]
    push = build_push(recent, ch_keys, owner_fav, stars, ja)
    json.dump(push, open(os.path.join(HERE, "news_push.json"), "w"), ensure_ascii=False)

    # 持ち主へ:エース表の選手がよそに動いた(同じ知らせは1回だけ)
    alerted = set(state.get("star_alerted", []))
    msgs = []
    for it, name, old, newab in star_moves(items, stars):
        k = f"{it['id']}|{name}"
        if k in alerted:
            continue
        alerted.add(k)
        if seeded:
            msgs.append(f"・{name}:エース表では{old}、{it['date']}に{newab}が獲得 → stars.json を直す?")
    alert = os.path.join(HERE, "news_alert.txt")
    if msgs:
        open(alert, "w").write("【エース表の選手が移籍したかも】\n" + "\n".join(msgs))
    elif os.path.exists(alert):
        os.remove(alert)

    if ok:   # 両方取れなかったときは記録を変えない
        json.dump({"seeded": True, "items": items, "star_alerted": sorted(alerted)},
                  open(STATE, "w"), ensure_ascii=False, indent=0)
    out_dir = os.environ.get("OUT_DIR", HERE)
    json.dump({"generated_at": now.isoformat(timespec="minutes"), "ok": ok == 2, "items": deals,
               "team_ja": ja},
              open(os.path.join(out_dir, "news.json"), "w"), ensure_ascii=False)
    # ログには件数だけ
    print(f"news: {len(items)} items ({sum(1 for i in items if i['kind'] == 'report')} reports), new {len(new)}, push {len(push)} channels, sources ok {ok}/2")


if __name__ == "__main__":
    main()
