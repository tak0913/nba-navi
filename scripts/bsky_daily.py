#!/usr/bin/env python3
"""公式Blueskyに1日1回「今日の見るべき試合」を投稿する

collect.py が作った bsky_daily.json を読み、その日の試合が全部終わったら(遅くても17時以降なら)1回だけ投稿。
投稿した日付は bsky_state.json に残す(自動でコミット)。スコア・勝敗は一切書かない。
"""
import json, os, datetime as dt
import vote as V

HERE = os.path.dirname(os.path.abspath(__file__))
JST = dt.timezone(dt.timedelta(hours=9))
STATE = os.path.join(HERE, "bsky_state.json")


def main():
    cfg = V.CFG.get("bsky_daily", {})
    if not cfg.get("enabled"):
        return
    d = V.load(os.path.join(HERE, "bsky_daily.json"), None)
    st = V.load(STATE, {})
    now = dt.datetime.now(JST)
    if not d or d["total"] == 0 or st.get("last_daily") == d["date"] or d["date"] != now.date().isoformat():
        return
    if not (d["done"] == d["total"] and now.hour >= cfg["earliest_hour"]) and now.hour < cfg["latest_hour"]:
        return
    day = dt.date.fromisoformat(d["date"])
    head = f"【{day.month}/{day.day} 今日の見るべき試合】🏀"
    body = d["lines"][:cfg["max_lines"]] or ["今日はA以上の試合はなし。ゆっくり休もう"]
    foot = "スコア・勝敗は書かないのでネタバレなし\n" + V.CFG.get("app_url", "")
    text = "\n".join([head] + body + ["", foot])
    while len(text) > 300 and len(body) > 1:
        body = body[:-1]
        text = "\n".join([head] + body + ["…ほかはアプリで", foot])
    try:
        sess = V.bsky_login()
    except Exception as e:
        print("bluesky login failed:", type(e).__name__)
        return
    if not sess:
        return
    V.bsky_say(sess, text)
    st["last_daily"] = d["date"]
    V.save(STATE, st)
    print("bsky daily posted:", d["date"])


if __name__ == "__main__":
    main()
