#!/usr/bin/env python3
"""移籍ニュース(トレード)を集める。

- 確定:ESPNの「選手の移籍・契約の一覧」(transactions)から、トレード(Acquired / trade)だけ拾う
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


def trade_ja(team, desc, tinfo, ja):
    """「Acquired G X from Chicago Bulls for G Y.」→「ホーネッツが X を獲得(ブルズから。見返り:Y)」
    「Acquired …」の文が全部きれいに読めたときだけ日本語にする。1つでも読めなければ None(英語の原文だけ出す)
    戻り値:(日本語, [相手チーム])"""
    acq = [s for s in sentences(desc) if s.startswith("Acquired")]
    if not acq:
        return None
    parts, others = [], []
    for s in acq:
        m = ACQ.match(s)
        if not m:
            return None
        what, frm, give = m.group(1), m.group(2), m.group(3)
        if " in a " in frm or " and " in frm or (give and " in a " in give):   # 三角トレードなどは原文だけ
            return None
        fab = find_team(frm, tinfo)
        if not fab:
            return None
        t = f"{ja_assets(what)} を獲得({ja.get(fab, fab)}から"
        if give:
            t += f"。見返り:{ja_assets(give)}"
        parts.append(t + ")")
        others.append(fab)
    return f"{ja.get(team, team)}が " + "/".join(parts), others


def fetch_trades(tinfo, ja, season=None):
    url = f"{BASE}/transactions?limit=500" + (f"&season={season}" if season else "")
    d = get(url)
    out = []
    for t in d.get("transactions", []):
        desc = (t.get("description") or "").strip()
        if not re.search(r"\bAcquired\b|\btrade\b", desc):
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
        got = norm(" ".join(m.group(1) for s in sentences(it["en"]) for m in [re.match(r"Acquired (.+?) from ", s)] if m))
        if not got:
            continue
        for key, (ab, name) in where.items():
            if key and key in got and it["teams"][0] != ab:
                out.append((it, name, ab, it["teams"][0]))
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
    recent = [it for it in new if seeded and is_recent(it)]
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
    json.dump({"generated_at": now.isoformat(timespec="minutes"), "ok": ok == 2, "items": items},
              open(os.path.join(out_dir, "news.json"), "w"), ensure_ascii=False)
    # ログには件数だけ
    print(f"news: {len(items)} items ({sum(1 for i in items if i['kind'] == 'report')} reports), new {len(new)}, push {len(push)} channels, sources ok {ok}/2")


if __name__ == "__main__":
    main()
