#!/usr/bin/env python3
"""月末の決選投票(民衆の声で格上げを決める)

毎時の更新のたびに呼ばれ、日付を見て必要なときだけ動く(何度呼んでも同じ結果になる)。
  - 毎月 open_day 日以降:先月の SS・S・A の試合と「物語」を候補にして、Bluesky に投票スレッドを立てる
  - 毎月 close_day 日以降:各候補への「いいね+リポスト」を数えて、上位を1段格上げ(upgrades.json に書く)
投票が少なすぎる(min_total 未満)ときは、余韻(ニュース・動画の盛り上がり)で代わりに決める。

Bluesky のアカウントは GitHub の Secret「BSKY_HANDLE」「BSKY_APP_PASSWORD」に入れる。
未設定のあいだは投稿せず、候補だけ作って余韻で決める。
ネタバレ防止のため、投稿にも標準出力にもスコア・勝敗は出さない。

手動で試す:  python3 vote.py --now 2026-12-02T12:00+09:00 [--dry-run]
"""
import json, os, sys, datetime as dt, urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
JST = dt.timezone(dt.timedelta(hours=9))
CFG = json.load(open(os.path.join(HERE, "config.json")))
V = CFG["vote"]
VOTE_DIR = os.path.join(HERE, "votes")
UPGRADES = os.path.join(HERE, "upgrades.json")
ARCHIVE = os.path.join(HERE, "archive.json")
NEXT = {"A": "S", "S": "SS"}
KIND_JA = {"rec70": "歴史的記録", "rec60": "60点超え", "pts50": "50点超え", "pts40": "40点超え", "td": "トリプルダブル"}
KIND_ORDER = ["rec70", "rec60", "pts50", "pts40", "td"]


def load(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(obj, open(path, "w"), ensure_ascii=False, indent=1)


def md(d):
    d = dt.date.fromisoformat(d)
    return f"{d.month}/{d.day}"


def last_name(n):
    return n.split(" ")[-1] if n.split(" ")[-1] not in ("Jr.", "III", "II") else n.split(" ")[-2]


def buzz_avg(r):
    vals = [v for v in (r.get("buzz") or {}).values() if v is not None]
    return sum(vals) / len(vals) if vals else 0.5


# ---------- 候補づくり ----------
def build_candidates(month, records):
    games = [r for r in records if r["date"][:7] == month and r.get("tier") in ("SS", "S", "A")]
    by_id = {r["id"]: r for r in games}
    cands = []
    # 物語の自動検出は、基準が決まるまで止めておく(config の vote.auto_stories)
    if not V.get("auto_stories", False):
        games_only = True
    else:
        games_only = False
    # 物語①:同じ選手が今月なんども大活躍(2試合以上)
    players = {}
    for r in games:
        for name, abbr, kind in r.get("hl") or []:
            players.setdefault((name, abbr), []).append((r["id"], kind))
    for (name, abbr), evs in ([] if games_only else players.items()):
        ids = sorted({i for i, _ in evs}, key=lambda i: by_id[i]["date"])
        if len(ids) < 2:
            continue
        top = min((k for _, k in evs), key=KIND_ORDER.index)
        n = sum(1 for _, k in evs if k == top)
        label = f"{last_name(name)}({abbr})の{KIND_JA[top]}×{n}" if n >= 2 else f"{last_name(name)}({abbr})の大活躍×{len(ids)}"
        cands.append({"key": f"p:{name}", "kind": "story", "name": label, "games": ids})
    # 物語②:同じカードが今月2回以上ランク入り
    pairs = {}
    for r in games:
        pairs.setdefault(tuple(sorted((r["home"], r["away"]))), []).append(r["id"])
    for (a, b), ids in ([] if games_only else pairs.items()):
        if len(ids) >= 2:
            ids.sort(key=lambda i: by_id[i]["date"])
            cands.append({"key": f"m:{a}-{b}", "kind": "story", "name": f"{a}×{b} 因縁の{len(ids)}戦", "games": ids})
    # 1試合ずつ(S・A。SSは余韻で殿堂入りを決めるので投票しない)
    for r in sorted(games, key=lambda r: r["date"]):
        if r["tier"] in ("A", "S"):
            cands.append({"key": f"g:{r['id']}", "kind": "game", "name": f"{md(r['date'])} {r['away']} @ {r['home']}",
                          "games": [r["id"]]})
    cands = cands[:V["max_candidates"]]
    for c in cands:
        c["detail"] = [{"id": i, "date": by_id[i]["date"], "away": by_id[i]["away"], "home": by_id[i]["home"],
                        "tier": by_id[i]["tier"]} for i in c["games"]]
    return cands


def cand_text(i, c):
    if c["kind"] == "story":
        lines = [f"候補{i} 物語:{c['name']}"] + [f"・{md(d['date'])} {d['away']} @ {d['home']}({d['tier']})" for d in c["detail"]]
    else:
        d = c["detail"][0]
        lines = [f"候補{i} {c['name']}(いま{d['tier']})"]
    txt = "\n".join(lines)
    return txt[:280]


# ---------- Bluesky ----------
BSKY = "https://bsky.social/xrpc/"
PUBLIC = "https://public.api.bsky.app/xrpc/"


def _post(url, body, token=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def bsky_login():
    handle, pw = os.environ.get("BSKY_HANDLE"), os.environ.get("BSKY_APP_PASSWORD")
    if not handle or not pw:
        return None
    s = _post(BSKY + "com.atproto.server.createSession", {"identifier": handle, "password": pw})
    return s["accessJwt"], s["did"]


def bsky_say(sess, text, root=None, parent=None):
    token, did = sess
    rec = {"$type": "app.bsky.feed.post", "text": text, "langs": ["ja"],
           "createdAt": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")}
    if root:
        rec["reply"] = {"root": root, "parent": parent or root}
    r = _post(BSKY + "com.atproto.repo.createRecord", {"repo": did, "collection": "app.bsky.feed.post", "record": rec}, token)
    return {"uri": r["uri"], "cid": r["cid"]}


def bsky_counts(uris):
    out = {}
    for i in range(0, len(uris), 25):
        q = "&".join("uris=" + urllib.parse.quote(u) for u in uris[i:i + 25])
        with urllib.request.urlopen(PUBLIC + "app.bsky.feed.getPosts?" + q, timeout=30) as r:
            for p in json.load(r).get("posts", []):
                out[p["uri"]] = p.get("likeCount", 0) + p.get("repostCount", 0) + p.get("quoteCount", 0)
    return out


# ---------- 開く・締める ----------
def open_vote(month, now, records, dry):
    path = os.path.join(VOTE_DIR, f"{month}.json")
    st = load(path, None)
    if st is None:
        cands = build_candidates(month, records)
        close_at = now.replace(day=V["close_day"], hour=12, minute=0, second=0, microsecond=0)
        st = {"month": month, "opened_at": now.isoformat(timespec="minutes"),
              "close_at": close_at.isoformat(timespec="minutes"), "posted": False, "closed": False, "candidates": cands}
        save(path, st)
    # まだ投稿してなければ投稿(アカウントをあとから設定しても、締切の2日前までなら間に合う)
    cands, close_at = st["candidates"], dt.datetime.fromisoformat(st["close_at"])
    if st["posted"] or st["closed"] or now > close_at - dt.timedelta(days=2):
        return False
    try:
        sess = None if dry else bsky_login()
    except Exception as e:
        print("bluesky login failed:", type(e).__name__)
        sess = None
    if sess and cands:
        y, m = month.split("-")
        head = (f"【NBA観戦ナビ {int(m)}月の決選投票】\n"
                f"格上げしてほしい試合・物語に「いいね」で投票してね(リポストも1票)。\n"
                f"締切 {close_at.month}/{close_at.day} 12:00(日本時間)\n"
                f"スコアや勝敗は書かないのでネタバレなし🏀\n{CFG.get('app_url', '')}")
        root = bsky_say(sess, head[:300])
        st["root"] = root
        parent = root
        for i, c in enumerate(cands, 1):
            c["post"] = bsky_say(sess, cand_text(i, c), root, parent)
            parent = c["post"]
        st["posted"] = True
    save(path, st)
    print(f"vote opened: {month} candidates={len(cands)} posted={st['posted']}")
    return True


def close_vote(month, now, records, dry):
    path = os.path.join(VOTE_DIR, f"{month}.json")
    st = load(path, None)
    if not st or st.get("closed") or now < dt.datetime.fromisoformat(st["close_at"]):
        return False
    by_id = {r["id"]: r for r in records}
    cands = st["candidates"]
    votes = {}
    if st.get("posted") and not dry:
        try:
            cnt = bsky_counts([c["post"]["uri"] for c in cands if c.get("post")])
            votes = {c["key"]: cnt.get(c["post"]["uri"], 0) for c in cands if c.get("post")}
        except Exception as e:
            print("count failed:", type(e).__name__)
    total = sum(votes.values())
    by_people = total >= V["min_total"]
    for c in cands:
        c["votes"] = votes.get(c["key"], 0)
        c["buzz"] = round(sum(buzz_avg(by_id[i]) for i in c["games"] if i in by_id) / max(1, len(c["games"])), 3)
    if by_people:
        ranked = sorted([c for c in cands if c["votes"] > 0], key=lambda c: -c["votes"])
    else:  # 投票が少ないときは余韻(民衆の声)で決める
        ranked = sorted([c for c in cands if c["buzz"] >= V["fallback_buzz_min"]], key=lambda c: -c["buzz"])
    winners = ranked[:V["winners"]]
    ups = load(UPGRADES, {})
    for c in winners:
        why = c["name"] if c["kind"] == "story" else "民衆の声" if by_people else "余韻"
        for d in c["detail"]:
            if d["tier"] in NEXT and d["id"] not in ups:
                ups[d["id"]] = {"from": d["tier"], "to": NEXT[d["tier"]], "why": why, "month": month}
    save(UPGRADES, ups)
    st.update({"closed": True, "closed_at": now.isoformat(timespec="minutes"), "total_votes": total,
               "decided_by": "votes" if by_people else "buzz", "winners": [c["key"] for c in winners]})
    save(path, st)
    print(f"vote closed: {month} total={total} decided_by={st['decided_by']} winners={len(winners)}")
    return True


def main():
    now = dt.datetime.now(JST)
    if "--now" in sys.argv:
        now = dt.datetime.fromisoformat(sys.argv[sys.argv.index("--now") + 1]).astimezone(JST)
    dry = "--dry-run" in sys.argv
    if not V.get("enabled", True):
        return
    records = load(ARCHIVE, [])
    first = now.date().replace(day=1)
    prev_month = (first - dt.timedelta(days=1)).strftime("%Y-%m")
    if now.day >= V["open_day"] and records:
        open_vote(prev_month, now, records, dry)
    if now.day >= V["close_day"]:
        close_vote(prev_month, now, records, dry)


if __name__ == "__main__":
    main()
