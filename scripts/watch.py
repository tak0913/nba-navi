"""試合の見張り番(watch.yml から動く)。

GitHubの定期実行は20〜60分遅れたり抜けたりするので、試合がある時間はこれがESPNを数分おきに見て、
試合が終わったらすぐ「更新と通知」(update.yml)を動かして採点させる。

- 起動したとき:今日(日本の日付)にもう終わっている試合があれば、念のため1回更新を動かす(見張りがいない間に終わった分)
- そのあと:poll_seconds ごとに見て、新しく終わった試合があれば更新を動かす
- 試合中の試合がなく、max_wait_minutes 分以内に始まる試合もなくなったら終わる(最長 max_minutes 分)
- 更新の回は concurrency で1つずつ動き、待っている回は1つにまとまるので、続けて終わっても動かしすぎない
- ネタバレ防止:ログには試合の中身(スコア・勝敗)を出さない。件数だけ
"""
import datetime as dt, json, os, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
JST = dt.timezone(dt.timedelta(hours=9))
CFG = json.load(open(os.path.join(HERE, "config.json"))).get("watch", {})
POLL = CFG.get("poll_seconds", 90)
MAX_MIN = CFG.get("max_minutes", 340)
WAIT_MIN = CFG.get("max_wait_minutes", 70)


def get(url):
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if i == 2:
                raise
            time.sleep(1 + 2 * i)


def games(now):
    """今日(日本の日付)と昨日に始まる試合の {id: (状態, 開始時刻)}。状態は pre / in / post"""
    out = {}
    days = {(now - dt.timedelta(days=k)).date() for k in (0, 1)}
    for us in sorted({d - dt.timedelta(days=1) for d in days} | days):
        sb = get(f"{API}/scoreboard?dates={us.strftime('%Y%m%d')}")
        for e in sb.get("events", []):
            start = dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(JST)
            if start.date() not in days:
                continue
            st = e["competitions"][0]["status"]["type"]
            out[e["id"]] = ("post" if st.get("completed") else st.get("state", "pre"), start)
    return out


def dispatch(reason):
    """更新と通知(update.yml)を動かす。通知の時間なら通知も送る(auto=yes)"""
    print(f"更新を動かす:{reason}", flush=True)
    if "--dry" in sys.argv:
        return
    r = subprocess.run(["gh", "workflow", "run", "update.yml", "--ref", "main", "-f", "auto=yes"],
                       capture_output=True, text=True)
    if r.returncode:
        print("dispatch failed:", r.stderr.strip()[:200], flush=True)


def should_stay(gs, now):
    """まだ見張る必要があるか:試合中がある、または WAIT_MIN 分以内に始まる試合がある"""
    for state, start in gs.values():
        if state == "in":
            return True
        if state == "pre" and start - now <= dt.timedelta(minutes=WAIT_MIN):
            return True   # 開始時刻を過ぎてもまだ pre(開始が遅れている)場合もここに入る
    return False


def main():
    t0 = time.time()
    now = dt.datetime.now(JST)
    try:
        gs = games(now)
    except Exception as e:
        print("scoreboard failed:", type(e).__name__)
        return
    done = {gid for gid, (s, _) in gs.items() if s == "post"}
    today_done = [gid for gid in done if gs[gid][1].date() == now.date()]
    print(f"見張り開始:試合 {len(gs)} / 終了 {len(done)}", flush=True)
    if today_done:
        dispatch(f"起動時点で今日の終了 {len(today_done)} 試合")
    while time.time() - t0 < MAX_MIN * 60:
        now = dt.datetime.now(JST)
        if not should_stay(gs, now):
            print("見張る試合がなくなったので終わり", flush=True)
            return
        time.sleep(POLL)
        try:
            gs = games(dt.datetime.now(JST))
        except Exception as e:
            print("scoreboard failed:", type(e).__name__, flush=True)
            continue
        new = {gid for gid, (s, _) in gs.items() if s == "post"} - done
        if new:
            done |= new
            dispatch(f"新しく終わった試合 {len(new)}")
    print("時間切れ(次の見張りに引き継ぐ)", flush=True)


if __name__ == "__main__":
    main()
