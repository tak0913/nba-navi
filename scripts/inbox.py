#!/usr/bin/env python3
"""みんなからの投稿(因縁の候補・ランクのズレ・その他の意見)を集める

アプリの「投稿」タブから ntfy の投稿用チャンネルに送られた内容を、毎時の更新で拾って
suggestions.json に貯める(ntfy は約12時間しか保存しないので、毎時拾えば取りこぼさない)。
新しい投稿があったら、持ち主の通知チャンネル(Secret の NTFY_TOPIC)に件数だけ知らせる。
投稿は「候補」として貯めるだけ。因縁リストに入れるかは持ち主が決める。
"""
import json, os, re, time, urllib.request, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json")))
PATH = os.path.join(HERE, "suggestions.json")
RATINGS = os.path.join(HERE, "ratings.json")   # 見た人の「面白かった?」{試合id: {端末の印: good/ok/bad}}
KINDS = {"rivalry", "rank", "trivia", "other"}
RATE = {"good", "ok", "bad"}
HEAT = {"hot", "tradition"}


def load():
    try:
        return json.load(open(PATH))
    except Exception:
        return {"last_poll": 0, "items": []}


def clean(s, n):
    s = re.sub(r"[\x00-\x1f\x7f]", " ", str(s or "")).strip()
    return s[:n]


def parse(msg, teams):
    """ntfy の1件を投稿データに直す。形がおかしいものは捨てる"""
    try:
        d = json.loads(msg.get("message", ""))
    except Exception:
        return None
    if not isinstance(d, dict) or d.get("kind") not in KINDS:
        return None
    it = {"id": clean(msg.get("id"), 20), "at": dt.datetime.fromtimestamp(msg.get("time", 0), dt.timezone.utc)
          .astimezone(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="minutes"),
          "kind": d["kind"], "text": clean(d.get("text"), 400), "by": clean(d.get("by"), 20), "status": "new"}
    if d["kind"] == "rivalry":
        a, b = clean(d.get("a"), 5).upper(), clean(d.get("b"), 5).upper()
        if a not in teams or b not in teams or a == b:
            return None
        it["pair"] = "-".join(sorted((a, b)))
        it["heat"] = d.get("heat") if d.get("heat") in HEAT else "hot"
    elif d["kind"] == "trivia":
        it["game"] = clean(d.get("game"), 40)
        if not it["text"]:
            return None
    elif d["kind"] == "rank":
        it["game"] = clean(d.get("game"), 40)
        if not it["text"]:
            return None
    elif not it["text"]:
        return None
    return it


def main():
    topic = CFG.get("suggest_topic")
    if not topic:
        return
    st = load()
    since = st.get("last_poll") or int(time.time()) - 12 * 3600
    url = f"https://ntfy.sh/{topic}/json?poll=1&since={since}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "nba-navi"}), timeout=30) as r:
            lines = r.read().decode("utf-8", "ignore").splitlines()
    except Exception as e:
        print("inbox poll failed:", type(e).__name__)
        return
    teams = set(json.load(open(os.path.join(HERE, "stars.json"))).keys()) - {"_note"}
    seen = {i["id"] for i in st["items"]}
    new, rated = [], 0
    try:
        ratings = json.load(open(RATINGS))
    except Exception:
        ratings = {}
    for ln in lines:
        try:
            m = json.loads(ln)
        except Exception:
            continue
        if m.get("event") != "message" or m.get("id") in seen:
            continue
        try:
            d = json.loads(m.get("message", ""))
        except Exception:
            d = None
        if isinstance(d, dict) and d.get("kind") == "rate":
            # 同じ端末が同じ試合を押し直したら、最後の1回だけ数える
            g, dev, v = clean(d.get("game"), 20), clean(d.get("dev"), 16), d.get("v")
            if g.isdigit() and dev and v in RATE:
                ratings.setdefault(g, {})[dev] = v
                rated += 1
            st["last_poll"] = max(st.get("last_poll", 0), int(m.get("time", 0)))
            continue
        it = parse(m, teams)
        if it:
            new.append(it)
            seen.add(it["id"])
        st["last_poll"] = max(st.get("last_poll", 0), int(m.get("time", 0)))
    st["items"] = (st["items"] + new)[-500:]
    json.dump(st, open(PATH, "w"), ensure_ascii=False, indent=1)
    if rated:
        json.dump(ratings, open(RATINGS, "w"), separators=(",", ":"))
    print(f"inbox: {len(new)} new, {rated} ratings")
    owner = os.environ.get("NTFY_TOPIC")
    if new and owner:
        kinds = {"rivalry": "因縁", "rank": "ランクのズレ", "trivia": "小話", "other": "その他"}
        body = "みんなからの投稿が届いたよ\n" + "\n".join(
            f"{kinds[k]}:{sum(1 for i in new if i['kind'] == k)}件" for k in kinds if any(i["kind"] == k for i in new))
        try:
            urllib.request.urlopen(urllib.request.Request("https://ntfy.sh/" + owner, data=body.encode(),
                                                          headers={"Title": "NBA Navi 投稿", "Tags": "mailbox"}), timeout=20)
        except Exception:
            pass


if __name__ == "__main__":
    main()
