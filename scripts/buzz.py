"""余韻(民衆の声)の測定

試合の1日後・7日後・30日後に、その試合がどれだけ語られているかを数字にする。
- ニュース: Google ニュースで、その時期にその対戦を取り上げた「媒体の数」(同じ媒体の連投は1つと数える)
  - 地元以外の媒体の数も別に数える(地元は勝っても負けても必ず書くため)
- ハイライト動画: YouTube で、その試合のハイライト動画の再生数の合計(1日後・7日後のみ)
- 切り抜き動画・Bluesky: チーム名に加えて主力選手の名前でも探す(1プレーのバズは選手名だけで語られがち)
- 名場面: 一番伸びた切り抜き1本の再生数を同じ夜で比べ、上位ならボーナス(collect.py の measure_moments)
人気チームの偏りを抑えるため、生の数ではなく「同じ夜の試合の中で何番目か」に直し、
複数の指標の真ん中の値を使う。結果の中身(スコア等)は一切保存しない。
"""
import re, json, html, urllib.request, urllib.parse, datetime as dt, statistics

UA = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US"}


def _get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "ignore")


def news_sources(full_a, full_b, start, end):
    """start〜end(日付)に両チーム名を含む記事を出した媒体の一覧"""
    q = f'"{full_a}" "{full_b}" after:{start.isoformat()} before:{end.isoformat()}'
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(q) + "&hl=en-US&gl=US&ceid=US:en"
    x = _get(url)
    return [html.unescape(s) for s in re.findall(r"<source[^>]*>([^<]*)</source>", x)]


def news_signal(team_a, team_b, game_date, day):
    """day日後あたり(前後1日)の媒体数と、地元以外の媒体数"""
    s = game_date + dt.timedelta(days=max(0, day - 1))
    e = game_date + dt.timedelta(days=day + 2)
    srcs = set(news_sources(team_a["full"], team_b["full"], s, e))
    local_words = [w.lower() for t in (team_a, team_b) for w in t["local"]]
    national = {x for x in srcs if not any(w in x.lower() for w in local_words)}
    return len(srcs), len(national)


def _views(txt):
    m = re.match(r"([\d,\.]+)\s*([KM]?)", txt.replace("views", "").strip())
    if not m:
        return 0
    n = float(m.group(1).replace(",", ""))
    return int(n * {"K": 1e3, "M": 1e6}.get(m.group(2), 1))


def youtube_views(team_a, team_b, game_date):
    """その試合のハイライト動画(上位の検索結果で両チーム名を含むもの)の再生数合計"""
    q = f'{team_a["nick"]} vs {team_b["nick"]} full game highlights {game_date.strftime("%B")} {game_date.day}, {game_date.year}'
    h = _get("https://www.youtube.com/results?search_query=" + urllib.parse.quote(q))
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", h)
    if not m:
        return None
    data = json.loads(m.group(1))
    vids = []

    def walk(x):
        if isinstance(x, dict):
            if "videoRenderer" in x:
                v = x["videoRenderer"]
                title = "".join(r.get("text", "") for r in v.get("title", {}).get("runs", [])).lower()
                pub = v.get("publishedTimeText", {}).get("simpleText", "")
                vids.append((title, _views(v.get("viewCountText", {}).get("simpleText", "")), pub))
            for y in x.values():
                walk(y)
        elif isinstance(x, list):
            for y in x:
                walk(y)
    walk(data)
    total = 0
    for title, v, pub in vids[:10]:
        if team_a["nick"].lower() in title and team_b["nick"].lower() in title and "year" not in pub:
            total += v
    return total


def percentile_within(values):
    """同じ夜の中での順位を 0〜1 に(1が一番騒がれた)"""
    keys = [k for k, v in values.items() if v is not None]
    if len(keys) <= 1:
        return {k: 0.5 for k in keys}
    srt = sorted(keys, key=lambda k: values[k])
    return {k: i / (len(srt) - 1) for i, k in enumerate(srt)}


def combine(pcts):
    vals = [v for v in pcts if v is not None]
    return round(statistics.median(vals), 3) if vals else None


# ---------- SNS(v2) ----------
def bluesky_engagement(team_a, team_b, start, end, max_pages=3, names_a=(), names_b=()):
    """Blueskyで、期間内に両チーム名を含む投稿の 数・いいね・リポスト を合計する(登録不要の公開API)。
    「KDえぐい」みたいに選手名だけの投稿も拾うため、主力選手名+相手チーム名でも探す(同じ投稿は1回だけ数える)"""
    base = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
    since = f"{start.isoformat()}T00:00:00Z"
    until = f"{end.isoformat()}T00:00:00Z"
    queries = [(f'{team_a["nick"]} {team_b["nick"]}', max_pages)]
    for mine, opp in ((names_a, team_b), (names_b, team_a)):
        for n in list(mine)[:2]:
            queries.append((f'"{n}" {opp["nick"]}', 1))
    total, seen = 0, set()
    for q, pages in queries:
        cursor = None
        for _ in range(pages):
            url = f"{base}?q={urllib.parse.quote(q)}&since={since}&until={until}&limit=100&sort=top"
            if cursor:
                url += "&cursor=" + urllib.parse.quote(cursor)
            d = json.loads(_get(url))
            for p in d.get("posts", []):
                if p.get("uri") in seen:
                    continue
                seen.add(p.get("uri"))
                total += 1 + p.get("likeCount", 0) + 2 * p.get("repostCount", 0) + p.get("quoteCount", 0)
            cursor = d.get("cursor")
            if not cursor:
                break
    return total


def _yt_search(q):
    """YouTube検索の結果を (動画ID, タイトル小文字, 再生数, 公開時期の文字) のリストで返す"""
    h = _get("https://www.youtube.com/results?search_query=" + urllib.parse.quote(q))
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", h)
    if not m:
        return None
    vids = []

    def walk(x):
        if isinstance(x, dict):
            if "videoRenderer" in x:
                v = x["videoRenderer"]
                title = "".join(r.get("text", "") for r in v.get("title", {}).get("runs", [])).lower()
                pub = v.get("publishedTimeText", {}).get("simpleText", "")
                vids.append((v.get("videoId"), title, _views(v.get("viewCountText", {}).get("simpleText", "")), pub))
            for y in x.values():
                walk(y)
        elif isinstance(x, list):
            for y in x:
                walk(y)
    walk(json.loads(m.group(1)))
    return vids


def _age_days(pub):
    """「3 days ago」などをおおよその日数に。わからなければ None"""
    # 「3 days ago」と短い「3d ago」「1mo ago」の両方に対応
    m = re.search(r"(\d+)\s*(seconds?|minutes?|hours?|days?|weeks?|months?|years?|mo|s|m|h|d|w|y)\b", pub.lower())
    if not m:
        return None
    n, u = int(m.group(1)), m.group(2)
    u = {"s": "second", "m": "minute", "h": "hour", "d": "day", "w": "week", "mo": "month", "y": "year"}.get(u, u.rstrip("s"))
    return {"second": 0, "minute": 0, "hour": 0, "day": n, "week": 7 * n, "month": 30 * n, "year": 365 * n}[u]


def _last(name):
    """選手名の名字(小文字)。Jr. などは飛ばす"""
    parts = [w for w in name.lower().replace(".", "").split() if w not in ("jr", "sr", "ii", "iii", "iv")]
    return parts[-1] if parts else ""


def youtube_clip_views(team_a, team_b, game_date, names_a=(), names_b=()):
    """1プレーだけの切り抜き動画(フルハイライト以外)の (再生数の合計, 一番伸びた1本の再生数)。
    名場面があった試合ほど伸びる。切り抜きは「KDがギャフォードにポスタライズ」みたいに
    選手名だけのタイトルが多いので、チーム名に加えて主力選手の名前でも探す・数える。
    別の日の動画を数えないよう、公開時期が試合の日とだいたい合うものだけ使う"""
    us = game_date - dt.timedelta(days=1)   # 記録の日付は日本の日付。動画のタイトルはアメリカの日付なので1日戻す
    when = f'{us.strftime("%B")} {us.day} {us.year}'
    queries = [f'{team_a["nick"]} {team_b["nick"]} {when}']
    for mine, opp in ((names_a, team_b), (names_b, team_a)):
        for n in list(mine)[:2]:
            queries.append(f'{n} {opp["nick"]} {when}')
    since = (dt.date.today() - us).days
    tol = max(2, round(since * 0.35))
    words = {team_a["nick"].lower(), team_b["nick"].lower()} | {_last(n) for n in (*names_a, *names_b) if len(_last(n)) >= 4}
    skip = ("full game", "highlights", "reaction", "live", "stream", "recap", "postgame", "press conference")
    seen, total, top, got = set(), 0, 0, False
    for q in queries:
        vids = _yt_search(q)
        if vids is None:
            continue
        got = True
        for vid, title, v, pub in vids[:15]:
            if vid in seen or any(s in title for s in skip):
                continue
            age = _age_days(pub)
            if age is None or abs(age - since) > tol:
                continue
            if any(w in title for w in words):
                seen.add(vid)
                total += v
                top = max(top, v)
    return (total, top) if got else None
