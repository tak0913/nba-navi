"""余韻(民衆の声)の測定

試合の1日後・7日後・30日後に、その試合がどれだけ語られているかを数字にする。
- ニュース: Google ニュースで、その時期にその対戦を取り上げた「媒体の数」(同じ媒体の連投は1つと数える)
  - 地元以外の媒体の数も別に数える(地元は勝っても負けても必ず書くため)
- ハイライト動画: YouTube で、その試合のハイライト動画の再生数の合計(1日後・7日後のみ)
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
def bluesky_engagement(team_a, team_b, start, end, max_pages=3):
    """Blueskyで、期間内に両チーム名を含む投稿の 数・いいね・リポスト を合計する(登録不要の公開API)"""
    base = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
    q = f'{team_a["nick"]} {team_b["nick"]}'
    since = f"{start.isoformat()}T00:00:00Z"
    until = f"{end.isoformat()}T00:00:00Z"
    total, posts, cursor = 0, 0, None
    for _ in range(max_pages):
        url = f"{base}?q={urllib.parse.quote(q)}&since={since}&until={until}&limit=100&sort=top"
        if cursor:
            url += "&cursor=" + urllib.parse.quote(cursor)
        d = json.loads(_get(url))
        for p in d.get("posts", []):
            posts += 1
            total += 1 + p.get("likeCount", 0) + 2 * p.get("repostCount", 0) + p.get("quoteCount", 0)
        cursor = d.get("cursor")
        if not cursor:
            break
    return total


def youtube_clip_views(team_a, team_b, game_date):
    """1プレーだけの切り抜き動画(フルハイライト以外)の再生数合計。名場面があった試合ほど伸びる"""
    q = f'{team_a["nick"]} {team_b["nick"]} {game_date.strftime("%B")} {game_date.day} {game_date.year}'
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
                vids.append((title, _views(v.get("viewCountText", {}).get("simpleText", "")), pub))
            for y in x.values():
                walk(y)
        elif isinstance(x, list):
            for y in x:
                walk(y)
    walk(json.loads(m.group(1)))
    total = 0
    skip = ("full game", "full highlights", "reaction", "live", "stream", "recap", "postgame", "press conference")
    for title, v, pub in vids[:15]:
        if "year" in pub or any(s in title for s in skip):
            continue
        if team_a["nick"].lower() in title or team_b["nick"].lower() in title:
            total += v
    return total
