#!/usr/bin/env python3
"""NBA観戦アプリ: データ収集 + 採点 + 記録の蓄積

使い方:
  python3 collect.py                      通常の更新(直近3日を取り直し、記録に追加、ランクを付け直す)
  python3 collect.py --now 2026-11-02T12:00+09:00
  python3 collect.py --backfill 2026-10-20 2026-11-01   日本の日付の範囲をさかのぼって記録に追加
  python3 collect.py --backfill-prev      昨季(レギュラー+プレーオフ)を参考データとして記録

入力:   stars.json, config.json, archive.json(なければ新規), archive_prev.json(昨季の参考データ)
出力:   data.json(アプリ画面が読む), notify.txt(通知の文面), archive.json

ネタバレ防止のため、このスクリプトは標準出力に試合の中身を一切出さない。
"""
import json, os, sys, re, time, unicodedata, urllib.request, datetime as dt
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
JST = dt.timezone(dt.timedelta(hours=9))

CFG = json.load(open(os.path.join(HERE, "config.json")))
STARS = json.load(open(os.path.join(HERE, "stars.json")))
P, R, T = CFG["points"], CFG["rules"], CFG["tiers"]
TIER_ORDER = ["other", "ace", "allstar", "mvp"]
ARCHIVE = os.path.join(HERE, "archive.json")
ARCHIVE_PREV = os.path.join(HERE, "archive_prev.json")
PREV_SCORES = os.path.join(HERE, "prev_scores.json")   # 昨季の点数だけ(小さい版)
BUZZ_STATE = os.path.join(HERE, "buzz_state.json")      # 余韻の測定結果(これだけは測った時点でしか取れないので保存)

INJ_JA = {"Out": "欠場", "Doubtful": "出場厳しい", "Questionable": "出場微妙",
          "Probable": "出場濃厚", "Day-To-Day": "様子見", "Suspension": "出場停止"}

DETAIL_JA = [("Not Specified", "理由未発表"), ("Rest", "休養"), ("Illness", "体調不良"), ("Sprain", "捻挫"),
    ("Surgery", "手術"), ("Strain", "肉離れ"), ("Soreness", "痛み"), ("Fracture", "骨折"), ("Contusion", "打撲"),
    ("Inflammation", "炎症"), ("Bruise", "打撲"), ("Dislocated", "脱臼"), ("Spasms", "けいれん"),
    ("Pinched Nerve", "神経の圧迫"), ("Plantar Fasciitis", "足底筋膜炎"), ("Heel", "かかと"), ("Ankle", "足首"),
    ("Knee", "ひざ"), ("Back", "腰"), ("Hamstring", "太もも裏"), ("Foot", "足"), ("Hip", "股関節"),
    ("Calf", "ふくらはぎ"), ("Wrist", "手首"), ("Shoulder", "肩"), ("Hand", "手"), ("Concussion", "脳しんとう"),
    ("Groin", "足の付け根"), ("Achilles", "アキレス腱"), ("Thumb", "親指"), ("Finger", "指"), ("Elbow", "ひじ"),
    ("Quadriceps", "太もも前"), ("Toe", "つま先"), ("Personal", "個人的な理由"), ("Neck", "首"), ("Rib", "肋骨"),
    ("Abdominal", "腹部"), ("Oblique", "脇腹"), ("Shin", "すね"), ("Eye", "目"), ("Face", "顔"), ("Nose", "鼻"),
    ("Leg", "脚"), ("Chest", "胸"), ("Head", "頭"), ("Lower", "下部"), ("Upper", "上部"), ("Tendinitis", "腱炎"),
    ("Torn", "断裂"), ("Tear", "断裂"), ("Plantar Fascia", "足底"), ("Management", "管理"), ("Injury", "負傷"),
    ("Suspension", "出場停止"), ("Other", ""), ("Undisclosed", "理由未発表"), ("Not Injury Related", "けが以外"),
    ("Mcl", "内側側副靭帯"), ("Acl", "前十字靭帯"), ("Meniscus", "半月板"), ("Tendon", "腱"), ("Ligament", "靭帯"),
    ("Sore", "痛み"), ("Tightness", "張り"), ("Surgery", "手術"), ("Rupture", "断裂"), ("Stress Reaction", "疲労性の損傷"),
    ("Arm", "腕"), ("Forearm", "前腕"), ("Pelvis", "骨盤"), ("Lumbar", "腰椎"), ("Spine", "背骨"), ("Jaw", "あご"),
    ("Mouth", "口"), ("Ear", "耳"), ("Patella", "膝蓋骨"), ("Coach's Decision", "監督判断")]

SEASON_TYPE = {1: "pre", 2: "regular", 3: "playoff", 5: "playin"}
CUP_JA = {"group": "グループリーグ", "qf": "準々決勝", "sf": "準決勝", "final": "決勝"}


def cup_stage(comp):
    """NBAカップの試合なら段階(group/qf/sf/final)。ESPNの notes の見出し(例:"NBA Cup - Group Play")から読む"""
    for n in comp.get("notes") or []:
        h = (n.get("headline") or "").lower()
        if "cup" not in h:
            continue
        if "semi" in h:
            return "sf"
        if "quarter" in h:
            return "qf"
        if "championship" in h or "final" in h:
            return "final"
        return "group"
    return None


def game_type(e):
    """レギュラー/プレーオフなど。NBAカップ決勝はレギュラーシーズンの試合に数えないので、
    ESPNが別の種類で出してきてもランクから抜け落ちないよう、レギュラーと同じ表で比べる"""
    t = SEASON_TYPE.get(e.get("season", {}).get("type"))
    if t is None and cup_stage(e["competitions"][0]):
        return "regular"
    return t


# ---------- 小さな道具 ----------
def detail_ja(s):
    for en, ja in DETAIL_JA:
        s = re.sub(rf"\b{en}\b", ja, s, flags=re.I)
    s = s.replace("理由未発表", "") if len(s) > len("理由未発表") else s
    return s.replace(" ", "")


SIDE_JA = {"Left": "左", "Right": "右", "Bilateral": "両"}
WD_JA = "月火水木金土日"


def injury_reason(det):
    """例:左足首捻挫 / 休養 / 出場停止"""
    side = SIDE_JA.get(det.get("side", ""), "")
    body = detail_ja(det.get("type", "") or "")
    kind = detail_ja(det.get("detail", "") or "")
    if body in ("出場停止", "Suspension"):
        return "出場停止"
    if body and kind == "理由未発表":
        kind = ""
    s = (side + body + kind).replace("Suspension", "出場停止")
    return s or "理由未発表"


def return_label(ret, base):
    """ESPNの復帰見込み日から「あと何日」を作る"""
    if not ret:
        return "復帰時期は未発表"
    try:
        d = dt.date.fromisoformat(ret[:10])
    except ValueError:
        return "復帰時期は未発表"
    n = (d - base).days
    md = f"{d.month}/{d.day}({WD_JA[d.weekday()]})"
    if n <= 0:
        return "近日中に復帰の見込み"
    if n >= 60:
        return f"長期離脱(復帰見込み {md}・約{round(n / 30)}ヶ月)"
    if n >= 14:
        return f"復帰見込み {md}・約{round(n / 7)}週間"
    return f"復帰見込み {md}・あと{n}日"


def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1 + 2 * i)   # ESPNが一瞬つまずいただけのことが多いので、少し待ってからやり直す(1秒→3秒)


def norm(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s.lower())


def clock_sec(txt):
    txt = (txt or "0").strip()
    if ":" in txt:
        m, s = txt.split(":")
        return int(m) * 60 + float(s)
    try:
        return float(txt)
    except ValueError:
        return 0.0


def made_att(s):
    try:
        m, a = s.split("-")
        return int(m), int(a)
    except Exception:
        return 0, 0


def num(s):
    try:
        return float(s)
    except Exception:
        return 0.0


def jst(iso):
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(JST)


# ---------- チームの今季成績(強み・レーティング) ----------
STRENGTH_DEFS = [
    ("avgOffensiveRebounds", "オフェンスリバウンド", True),
    ("avgDefensiveRebounds", "ディフェンスリバウンド", True),
    ("avgThreePointFieldGoalsMade", "3ポイントの数", True),
    ("threePointFieldGoalPct", "3ポイントの成功率", True),
    ("avgAssists", "パス回し(アシスト)", True),
    ("avgSteals", "スティール", True),
    ("avgBlocks", "ブロック", True),
    ("avgTurnovers", "ミスの少なさ", False),
    ("offRtg", "攻撃力(オフェンスレーティング)", True),
]


def team_season(team_id):
    d = get(f"{API}/teams/{team_id}/statistics")
    st = {}
    for c in d["results"]["stats"]["categories"]:
        for s in c["stats"]:
            st[s["name"]] = s.get("value", 0) or 0
    poss = st.get("avgFieldGoalsAttempted", 0) + 0.44 * st.get("avgFreeThrowsAttempted", 0) \
        - st.get("avgOffensiveRebounds", 0) + st.get("avgTurnovers", 0)
    st["offRtg"] = 100 * st.get("avgPoints", 0) / poss if poss else 0
    return st


def strengths(season):
    out = {}
    n = R["strength_top_n"]
    for key, label, high in STRENGTH_DEFS:
        ranked = sorted(season, key=lambda t: season[t].get(key, 0), reverse=high)
        for i, t in enumerate(ranked[:n]):
            out.setdefault(t, []).append(f"{label}(リーグ{i+1}位・今季)")
    return out


def load_league():
    teams = get(f"{API}/teams")["sports"][0]["leagues"][0]["teams"]
    tmap = {t["team"]["id"]: t["team"]["abbreviation"] for t in teams}
    with ThreadPoolExecutor(8) as ex:
        seas = dict(zip(tmap.values(), ex.map(team_season, tmap.keys())))
    return seas, strengths(seas)


# ---------- 試合の取得 ----------
# ---------- 取ったデータの保存(終わった試合の中身は変わらないので、1回取ったら使い回す) ----------
CACHE_DIR = os.path.join(HERE, "cache")
STALE = {"roster": 0, "summary": 0, "game": 0}   # 取れずに前回の分で埋めた件数(ログ用)
FRESH_DAYS = 2   # この日数より新しい試合は毎回取り直す(スタッツの訂正が入ることがあるため)


def _today_jst():
    return (NOW or dt.datetime.now(JST)).date()


def _cache_path(kind, key, ext="json.gz"):
    d = os.path.join(CACHE_DIR, kind)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{key}.{ext}")


def _cache_load(path):
    import gzip
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def _cache_save(path, obj):
    import gzip
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def trim_summary(sm):
    """採点に使う部分だけ残す(保存を小さくするため)"""
    keep_play = lambda pl: {
        "period": {"number": pl.get("period", {}).get("number")},
        "clock": {"displayValue": pl.get("clock", {}).get("displayValue")},
        "homeScore": pl.get("homeScore"), "awayScore": pl.get("awayScore"),
        "scoringPlay": pl.get("scoringPlay"), "scoreValue": pl.get("scoreValue"),
        "team": {"id": pl.get("team", {}).get("id")}, "type": {"text": pl.get("type", {}).get("text")},
        "text": pl.get("text"), "wallclock": pl.get("wallclock"),
        "participants": [{"athlete": {"id": x.get("athlete", {}).get("id")}} for x in pl.get("participants", [])],
    }
    return {"boxscore": sm.get("boxscore", {}), "injuries": sm.get("injuries", []),
            "plays": [keep_play(pl) for pl in sm.get("plays", [])]}


def get_summary(eid, completed, fresh):
    """試合の中身。終わった試合は保存して使い回す。取り直しに失敗したら保存してある分を使う"""
    path = _cache_path("summary", eid)
    if completed and not fresh and os.path.exists(path):
        return _cache_load(path)
    pre_path = _cache_path("pre", eid)   # まだ終わっていない試合の欠場情報(取れなかったときの予備)
    try:
        sm = get(f"{API}/summary?event={eid}")
    except Exception:
        if os.path.exists(path):
            return _cache_load(path)
        if not completed and os.path.exists(pre_path):
            STALE["summary"] += 1
            sm = _cache_load(pre_path)
            sm["_stale"] = True
            return sm
        raise
    if completed:
        _cache_save(path, trim_summary(sm))
    else:
        _cache_save(pre_path, {"injuries": sm.get("injuries", [])})
    return sm


def events_for_jst_date(d):
    """日本の日付 d に始まる試合(アメリカの日付だと前日と当日にまたがる)。古い日の分は保存して使い回す"""
    path = _cache_path("events", d.isoformat())
    old = (_today_jst() - d).days > FRESH_DAYS
    if old and os.path.exists(path):
        return [(e, jst(e["date"])) for e in _cache_load(path)]
    evs = {}
    try:
        for us in (d - dt.timedelta(days=1), d):
            sb = get(f"{API}/scoreboard?dates={us.strftime('%Y%m%d')}")
            for e in sb.get("events", []):
                start = jst(e["date"])
                if start.date() == d:
                    evs[e["id"]] = (e, start)
    except Exception:
        if os.path.exists(path):
            return [(e, jst(e["date"])) for e in _cache_load(path)]
        raise
    out = sorted(evs.values(), key=lambda x: x[1])
    if old and all(e["competitions"][0]["status"]["type"].get("completed") for e, _ in out):
        _cache_save(path, [e for e, _ in out])
    return out


def star_list(abbr):
    return STARS.get(abbr, [])


def tier_of(abbr, name):
    for p in star_list(abbr):
        if norm(p["name"]) == norm(name):
            return p["tier"]
    return "other"


def is_star(tier):
    return tier in ("mvp", "allstar")


# ---------- 採点(試合の中身) ----------
FLOW_JA = {"clutch": "クラッチ(終盤の接戦)", "overtime": "延長", "comeback20": "20点差以上の逆転",
           "buzzer": "ブザービーター", "last_possession": "最後の攻撃までもつれた", "clutch_stop": "試合を決める守備",
           "late_surge": "終盤の猛追", "late_upset": "終盤の大逆転"}


def score_flow(plays, home_id):
    """① 展開"""
    flags = {"clutch": False, "overtime": False, "comeback20": False, "buzzer": False,
             "last_possession": False, "clutch_stop": False, "late_surge": False, "late_upset": False}
    if not plays:
        return 0, flags
    lo = hi = 0
    prev_h = prev_a = 0
    prev_per, prev_sec = 0, 720.0
    last_period = 0
    close_secs = 0.0          # 終盤(第4Q残り5分〜延長)に5点差以内だった時間の合計
    peak = {"h": 0, "a": 0}   # 終盤(第4Q残り6分〜)に開いた最大リード
    surged = {"h": False, "a": False}   # そのリードを5点差以内まで詰められた
    for pl in plays:
        per = pl.get("period", {}).get("number", 0)
        last_period = max(last_period, per)
        sec = clock_sec(pl.get("clock", {}).get("displayValue"))
        h, a = int(pl.get("homeScore", prev_h)), int(pl.get("awayScore", prev_a))
        diff = h - a
        lo, hi = min(lo, diff), max(hi, diff)
        # クラッチ:前のプレーからこのプレーまでの時間、5点差以内だったら足す
        if per == prev_per and (per >= 5 or per == 4):
            start_s = prev_sec if per >= 5 else min(prev_sec, R["clutch_seconds"])
            if sec < start_s and abs(prev_h - prev_a) <= R["clutch_margin"]:
                close_secs += start_s - sec
        # 終盤の猛追:残り6分以降に15点差以上ついたのを、5点差以内まで詰めた
        if (per == 4 and sec <= R["late_window_seconds"]) or per >= 5:
            if diff > 0: peak["h"] = max(peak["h"], diff)
            if diff < 0: peak["a"] = max(peak["a"], -diff)
            for side, lead in (("h", diff), ("a", -diff)):
                if peak[side] >= R["late_surge_from"] and lead <= R["clutch_margin"]:
                    surged[side] = True
        # 最後の1プレーまでもつれた:第4Q・延長の残り24秒以内に3点差以内
        final_secs = per >= 4 and sec <= R["last_possession_seconds"]
        if final_secs and abs(prev_h - prev_a) <= R["last_possession_margin"]:
            flags["last_possession"] = True
        # 試合を決める守備:残り24秒以内、決まればワンポゼッション(and-one含む4点差以内)になる場面でのブロック・スティール
        if final_secs and abs(prev_h - prev_a) <= R["clutch_stop_margin"]:
            txt = (pl.get("text") or "").lower()
            if " blocks " in txt or " steals " in txt or "steal" in pl.get("type", {}).get("text", "").lower():
                flags["clutch_stop"] = True
                flags.setdefault("_key_players", []).extend(
                    x.get("athlete", {}).get("id") for x in pl.get("participants", []))
        if pl.get("scoringPlay") and per >= 4 and sec <= R["buzzer_seconds"] \
                and "free throw" not in pl.get("type", {}).get("text", "").lower():
            scorer_home = pl.get("team", {}).get("id") == home_id
            before = (prev_h - prev_a) if scorer_home else (prev_a - prev_h)
            after = diff if scorer_home else -diff
            if before < 0 <= after or (before == 0 and after > 0):
                flags["buzzer"] = True
        prev_h, prev_a = h, a
        prev_per, prev_sec = per, sec
    final = prev_h - prev_a
    flags["overtime"] = last_period >= 5
    flags["clutch"] = close_secs >= R["clutch_min_seconds"]
    if (final > 0 and lo <= -R["comeback_margin"]) or (final < 0 and hi >= R["comeback_margin"]):
        flags["comeback20"] = True
    # 終盤に大差をつけた側が負けた → 大逆転。負けなかったら猛追(どちらか一方だけ)
    if (peak["h"] >= R["late_surge_from"] and final < 0) or (peak["a"] >= R["late_surge_from"] and final > 0):
        flags["late_upset"] = True
    elif surged["h"] or surged["a"]:
        flags["late_surge"] = True
    pts = sum(P[k] for k, v in flags.items() if v and not k.startswith("_"))
    return min(P["flow_cap"], pts), flags


def garbage_points(plays):
    """勝負が決まった後(第4Q残り5分以降で20点差以上)に取った点を選手ごとに数える。
    ただし、そのあと10点差以内まで詰まった(勝負が決まってなかった)なら数えない"""
    g = {}
    seq, prev_h, prev_a = [], 0, 0
    for pl in plays:
        h, a = int(pl.get("homeScore", prev_h)), int(pl.get("awayScore", prev_a))
        seq.append((pl, prev_h, prev_a, abs(h - a)))
        prev_h, prev_a = h, a
    later_min = [0] * len(seq)
    m = 999
    for i in range(len(seq) - 1, -1, -1):
        later_min[i] = m
        m = min(m, seq[i][3])
    for i, (pl, ph, pa, _) in enumerate(seq):
        per = pl.get("period", {}).get("number", 0)
        sec = clock_sec(pl.get("clock", {}).get("displayValue"))
        if pl.get("scoringPlay") and per == 4 and sec <= R["garbage_seconds"] \
                and abs(ph - pa) >= R["garbage_margin"] and later_min[i] > R["garbage_recover_margin"]:
            parts = pl.get("participants") or []
            if parts:
                pid = parts[0].get("athlete", {}).get("id")
                g[pid] = g.get(pid, 0) + int(pl.get("scoreValue", 0))
    return g


def box_players(summary):
    out = {}
    for team in summary.get("boxscore", {}).get("players", []):
        abbr = team["team"]["abbreviation"]
        if not team.get("statistics"):
            continue
        st = team["statistics"][0]
        keys = st["names"]
        rows = []
        for a in st["athletes"]:
            vals = dict(zip(keys, a.get("stats") or []))
            rows.append({"id": a["athlete"]["id"], "name": a["athlete"]["displayName"],
                         "starter": a.get("starter", False), "dnp": a.get("didNotPlay", False) or not a.get("stats"),
                         "reason": a.get("reason", ""), "stats": vals})
        out[abbr] = rows
    return out


def has_aura(abbr, name):
    return any(norm(p["name"]) == norm(name) and p.get("aura") for p in star_list(abbr))


def promoted_mult(abbr, name, out_names):
    """大活躍した選手の倍率。エースが欠場なら次の選手を1段上げる。華のある選手もさらに1段上げる(最大MVP級)"""
    lst = star_list(abbr)
    tier = tier_of(abbr, name)
    up = 0
    if len(lst) >= 2 and norm(lst[0]["name"]) in out_names and norm(lst[1]["name"]) == norm(name):
        up += 1
    if has_aura(abbr, name):
        up += 1
    i = TIER_ORDER.index(tier)
    return CFG["tier_mult"][TIER_ORDER[min(i + up, len(TIER_ORDER) - 1)]]


def score_stars(box, plays, out_by_team=None, detail=None):
    """② スターの活躍(上限あり)と ⑤ 歴史的記録(上限なし・格を掛けない)を返す。detail に内訳を足す"""
    garbage = garbage_points(plays)
    total, record, legend, hl = 0.0, 0, False, []
    for abbr, rows in box.items():
        out_names = {norm(r["name"]) for r in rows if r["dnp"]} | (out_by_team or {}).get(abbr, set())
        for r in rows:
            if r["dnp"]:
                continue
            s = r["stats"]
            raw = num(s.get("PTS"))
            pts = raw - garbage.get(r["id"], 0)
            fgm, fga = made_att(s.get("FG", "0-0"))
            ftm, fta = made_att(s.get("FT", "0-0"))
            denom = 2 * (fga + 0.44 * fta)
            ts = raw / denom if denom else 0
            ev, why = [], []
            if pts >= 50: ev.append(P["pts50"]); why.append("50点以上")
            elif pts >= 40: ev.append(P["pts40"]); why.append("40点以上")
            elif pts >= 30 and ts >= 0.70: ev.append(P["pts30_ts70"]); why.append("30点・高効率")
            elif pts >= 25 and ts >= 0.75: ev.append(P["pts25_ts75"]); why.append("25点・超高効率")
            if num(s.get("BLK")) >= 5 or num(s.get("STL")) >= 4: ev.append(P["defense"]); why.append("守備")
            cats = [num(s.get(k)) for k in ("PTS", "REB", "AST", "STL", "BLK")]
            n10 = sum(1 for c in cats if c >= 10)
            if n10 >= 3: ev.append(P["triple_double"]); why.append("トリプルダブル")
            # ⑤ 歴史的記録:誰がやっても歴史に残るので格は掛けない
            rec = 0
            if pts >= 70: rec = P["record70"]
            elif pts >= 60: rec = P["record60"]
            if n10 >= 4: rec = max(rec, P["record70"])                       # クアドルプルダブル
            if sum(1 for c in cats if c >= 20) >= 3: rec = max(rec, P["record60"])  # 20-20-20
            # 物語づくり用のハイライト(月末の決選投票で「同じ選手が何度も」をまとめる)
            kind = ("rec70" if rec >= P["record70"] else "rec60" if rec else
                    "pts50" if pts >= 50 else "pts40" if pts >= 40 else "td" if n10 >= 3 else None)
            if kind and tier_of(abbr, r["name"]) != "other":
                hl.append([r["name"], abbr, kind])
            if rec:
                record += rec
                legend = legend or rec >= P["record70"]
                if detail is not None:
                    detail.append({"k": "record", "who": r["name"], "pts": rec})
            if not ev:
                continue
            base = max(ev) + P["extra_event"] * (len(ev) - 1)
            m = promoted_mult(abbr, r["name"], out_names)
            total += base * m
            if detail is not None:
                detail.append({"k": "star", "id": r["id"], "who": r["name"], "team": abbr, "why": why, "base": base,
                               "mult": m, "pts": round(base * m, 1), "raw": int(raw)})
    return min(P["star_cap"], round(total, 1)), record, legend, hl


def card_quality(teams, out_by_team, winning, detail=None, ctx=()):
    """③ カードの格。ctx = 試合の重み(カップの勝ち抜け戦・順位争い)。上限とは別に足す(最大 context_cap)"""
    pts = 0
    add = (lambda txt, v: detail.append({"txt": txt, "pts": v})) if detail is not None else (lambda txt, v: None)
    if all(winning.get(t["abbr"]) for t in teams):
        pts += P["both_winning"]; add("両チーム勝率5割以上", P["both_winning"])
    def has_star(abbr):
        return any(is_star(p["tier"]) and norm(p["name"]) not in out_by_team.get(abbr, set())
                   for p in star_list(abbr))
    if all(has_star(t["abbr"]) for t in teams):
        pts += P["both_stars"]; add("両チームのスター出場", P["both_stars"])
    for t in teams:
        lst = star_list(t["abbr"])
        outs = out_by_team.get(t["abbr"], set())
        if len(lst) >= 2 and norm(lst[0]["name"]) in outs and norm(lst[1]["name"]) in outs:
            pts += P["both_aces_out"]; add(f"{t['abbr']}:{lst[0]['name']}と{lst[1]['name']}が欠場", P["both_aces_out"])
        elif lst and norm(lst[0]["name"]) in outs:
            v = P["star_ace_out"] if is_star(lst[0]["tier"]) else P["ace_out"]
            pts += v; add(f"{t['abbr']}:エースの{lst[0]['name']}が欠場", v)
    base = max(0, min(P["card_cap"], pts))
    extra = 0
    for x in ctx:
        extra += x["pts"]
        if detail is not None:
            detail.append({"txt": x["txt"], "pts": x["pts"], "ctx": True})
    return base + min(P.get("context_cap", 10), extra)


def game_offrtg(team_stats):
    g = {s["name"]: s.get("displayValue", "0") for s in team_stats}
    fgm, fga = made_att(g.get("fieldGoalsMade-fieldGoalsAttempted", "0-0"))
    tpm, _ = made_att(g.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted", "0-0"))
    ftm, fta = made_att(g.get("freeThrowsMade-freeThrowsAttempted", "0-0"))
    pts = 2 * fgm + tpm + ftm
    poss = fga + 0.44 * fta - num(g.get("offensiveRebounds")) + num(g.get("totalTurnovers", g.get("turnovers")))
    return 100 * pts / poss if poss else 0


def team_anomaly(summary, season, detail=None):
    """④ 今日だけ異常(攻撃 or 守備がいつもより大きく良かった)"""
    pts = 0
    tms = summary.get("boxscore", {}).get("teams", [])
    if len(tms) != 2:
        return 0
    rt = {t["team"]["abbreviation"]: game_offrtg(t.get("statistics", [])) for t in tms}
    abbrs = list(rt)
    for a, b in ((abbrs[0], abbrs[1]), (abbrs[1], abbrs[0])):
        sa, sb = season.get(a, {}).get("offRtg", 0), season.get(b, {}).get("offRtg", 0)
        if sa and rt[a] >= sa + R["anomaly_rating_diff"]:
            pts += P["team_anomaly"]
            if detail is not None: detail.append({"txt": f"{a}の攻撃がいつもより大きく良かった", "pts": P["team_anomaly"]})
        if sb and rt[b] <= sb - R["anomaly_rating_diff"]:
            pts += P["team_anomaly"]
            if detail is not None: detail.append({"txt": f"{a}の守備がいつもより大きく良かった", "pts": P["team_anomaly"]})
    return pts


def expect_stars(card, clash):
    v = card + clash
    return 3 if v >= 20 else 2 if v >= 10 else 1


def parse_teams(comp, completed):
    teams, home_id = [], None
    for c in sorted(comp["competitors"], key=lambda c: c["homeAway"] != "home"):
        t = c["team"]
        rec = next((r["summary"] for r in c.get("records", []) if r.get("type") == "total"), "0-0")
        try:
            w, l = (int(x) for x in rec.split("-")[:2])
        except Exception:
            w, l = 0, 0
        if completed:  # 今日の結果を引いて「試合前の成績」にする
            if c.get("winner"): w -= 1
            else: l -= 1
        teams.append({"abbr": t["abbreviation"], "id": t["id"], "name": t.get("displayName", ""),
                      "short": t.get("shortDisplayName", t.get("name", "")), "homeAway": c["homeAway"],
                      "_w": w, "_l": l, "_score": c.get("score")})
        if c["homeAway"] == "home":
            home_id = t["id"]
    winning = {t["abbr"]: (t["_w"] + t["_l"] > 0 and t["_w"] >= t["_l"]) for t in teams}
    return teams, home_id, winning


def score_record(e, start, summary, season, strong, label=None):
    """終わった試合1つの採点結果(記録用)。結果そのもの(スコア)は持たない"""
    comp = e["competitions"][0]
    teams, home_id, winning = parse_teams(comp, True)
    plays = summary.get("plays", [])
    box = box_players(summary)
    # 欠場:ボックススコアの「出場なし」+ ケガ人リストの「欠場・出場停止」(ベンチ外の選手はボックスに載らないため)
    out_post = {ab: {norm(r["name"]) for r in rows if r["dnp"]} for ab, rows in box.items()}
    for blk in summary.get("injuries", []):
        ab = blk.get("team", {}).get("abbreviation")
        for i in blk.get("injuries", []):
            if i.get("status") in ("Out", "Suspension"):
                out_post.setdefault(ab, set()).add(norm(i.get("athlete", {}).get("displayName", "")))
    # 休養などでケガ人リストにも載らず、ボックススコアにもいないエース表の選手も欠場として数える
    for ab, rows in box.items():
        present = {norm(r["name"]) for r in rows}
        out_post.setdefault(ab, set()).update(norm(p["name"]) for p in star_list(ab) if norm(p["name"]) not in present)
    flow, flags = score_flow(plays, home_id)
    sdet, cdet, tdet = [], [], []
    stars, record, legend, hl = score_stars(box, plays, out_post, sdet)
    # 試合を決めたプレーにスター(MVP級・AS級)が関わっていたら、スターの見せ場として加点
    key = set(flags.get("_key_players", []))
    moment_star = any(r["id"] in key and is_star(tier_of(ab, r["name"])) for ab, rows in box.items() for r in rows)
    if moment_star:
        stars = min(P["star_cap"], stars + P["star_moment"])
    card = card_quality(teams, out_post, winning, cdet, context_items(comp, teams, start.date()))
    clash = P["strength_clash"] if all(strong.get(t["abbr"]) for t in teams) else 0
    if clash:
        tdet.append({"txt": "強み同士の対決", "pts": clash})
    team = min(P["team_cap"], clash + team_anomaly(summary, season, tdet))
    # 結果を開いた人に見せる計算の内訳(ネタバレそのものなので、普段のデータには入れず別ファイルにする)
    bd = {
        "flow": {"pts": flow, "cap": P["flow_cap"],
                 "items": [{"txt": FLOW_JA[k], "pts": P[k]} for k, v in flags.items() if v and k in FLOW_JA]},
        "stars": {"pts": stars, "cap": P["star_cap"],
                  "items": [x for x in sdet if x["k"] == "star"]
                           + ([{"k": "moment", "pts": P["star_moment"]}] if moment_star else [])},
        "card": {"pts": card, "cap": P["card_cap"], "items": cdet},
        "team": {"pts": team, "cap": P["team_cap"], "items": tdet},
        "record": {"pts": record, "items": [x for x in sdet if x["k"] == "record"]},
    }
    st = e.get("season", {})
    rec = {
        "id": e["id"], "date": start.date().isoformat(),
        "season": st.get("year"), "type": game_type(e) or "other", "cup": cup_stage(comp),
        "home": teams[0]["abbr"], "away": teams[1]["abbr"],
        "content": round(flow + stars + card + team + record, 1),
        "content_raw": round(flow + stars + card + team + record, 1), "flow": flow, "legend": legend, "hl": hl, "bd": bd,
        # 出場メンバー(いつものスタメンの欠場を数えるため。画面には渡さない)
        "lineup": {ab: {"st": {r["id"]: r["name"] for r in rows if r["starter"] and not r["dnp"]},
                        "pl": [r["id"] for r in rows if not r["dnp"]],
                        "out": sorted(out_post.get(ab, set())),
                        "pts": {r["id"]: [r["name"], int(num(r["stats"].get("PTS")))] for r in rows if not r["dnp"]}}
                   for ab, rows in box.items()},
        "end_utc": plays[-1].get("wallclock") if plays else None,   # 中だけで使う(画面には渡さない:試合の長さ=延長のヒントになる)
        "start_utc": e.get("date"),
        "buzz": {"d1": None, "d7": None, "d30": None},
    }
    if label:
        rec["label"] = label
    return rec


# ---------- 余韻 ----------
SIGNALS = ("news", "national", "yt", "clips", "bsky")   # ニュース/地元以外/フルハイライト/切り抜き/Bluesky
STATS = {k: 0 for k in SIGNALS}                          # 取れた件数(ログ用。中身は出さない)
TRIES = {k: 0 for k in SIGNALS}                          # 測ろうとした件数(見張り用)
NONZERO = {k: 0 for k in SIGNALS}                        # 0より大きい値が取れた件数(取れても全部0なら壊れている疑い)
MOMENT_STATS = {"tries": 0, "ok": 0}
def latest_buzz(r):
    """測れている分(1日後・7日後・30日後)の平均"""
    vals = [v for v in (r.get("buzz") or {}).values() if v is not None]
    return sum(vals) / len(vals) if vals else None


MOMENT_VER = 4   # 名場面の測り方の版。変えたら上げる(古い版の結果は捨てて測り直す)


def _moment_ok(m):
    return isinstance(m, list) and len(m) == 3 and m[2] == MOMENT_VER


def moment_bonus(r):
    """名場面ボーナス(moment = [点, 倍率, 版])"""
    m = r.get("moment") or []
    if not _moment_ok(m):    # 古い測り方の結果は使わない
        return 0
    return m[0] or 0


def fs(r):
    """最終点 = 試合の中身 + 余韻(同じ夜で真ん中なら±0、一番騒がれたら+重み、一番静かなら−重み) + 名場面ボーナス"""
    b = latest_buzz(r)
    base = r["content"] + moment_bonus(r)
    if b is None:
        return round(base, 1)
    w = CFG["buzz"]["weight_up"] if b >= 0.5 else CFG["buzz"]["weight_down"]
    return round(base + w * (b - 0.5) * 2, 1)


def _lum(hx):
    try:
        r, g, b = (int(hx[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except Exception:
        return 0
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def team_color(t):
    """チームカラー。暗い画面で見えるように、メインの色が暗すぎたらサブの色を使う"""
    main, alt = (t.get("color") or "888888"), (t.get("alternateColor") or "888888")
    pick = main if _lum(main) >= 0.06 else (alt if _lum(alt) > _lum(main) else main)
    if _lum(pick) < 0.04 or pick.lower() in ("000000", "ffffff"):
        pick = alt if alt.lower() not in ("000000", "ffffff") else "9aa3b2"
    return "#" + pick


def team_info():
    teams = get(f"{API}/teams")["sports"][0]["leagues"][0]["teams"]
    return {t["team"]["abbreviation"]: {"full": t["team"]["displayName"], "nick": t["team"]["name"],
                                        "local": [t["team"]["location"], t["team"]["name"]],
                                        "color": team_color(t["team"])} for t in teams}


def measure_buzz(records, today, limit):
    """1日後・7日後・30日後の余韻を、同じ夜の試合と比べて測る(古い順に limit 試合まで)"""
    import time
    import buzz as B
    info = team_info()
    nights = {}
    for r in records:
        nights.setdefault((r["date"], r["type"]), []).append(r)
    done = 0
    for (date, typ), games in sorted(nights.items()):
        gd = dt.date.fromisoformat(date)
        for day in (1, 7, 30):
            key = f"d{day}"
            if (today - gd).days < day or all((g.get("buzz") or {}).get(key) is not None for g in games):
                continue
            if done >= limit:
                return
            raw = {}
            for g in games:
                try:
                    a, b = info[g["away"]], info[g["home"]]
                    raw[g["id"]] = {}
                    def safe(f, *args):
                        try:
                            return f(*args)
                        except Exception:
                            return None
                    n = safe(B.news_signal, a, b, gd, day)
                    raw[g["id"]]["news"], raw[g["id"]]["national"] = n if n else (None, None)
                    raw[g["id"]]["yt"] = safe(B.youtube_views, a, b, gd) if day <= 7 else None
                    na, nb = star_names(g["away"]), star_names(g["home"])
                    ro = roster_names(g["id"]) if day <= 7 else {}
                    clip = safe(B.youtube_clip_views, a, b, gd, na, nb, g["away"], g["home"],
                                ro.get(g["away"], ()), ro.get(g["home"], ())) if day <= 7 else None
                    raw[g["id"]]["clips"] = clip[0] if clip else None
                    s = gd + dt.timedelta(days=max(0, day - 1))
                    raw[g["id"]]["bsky"] = safe(B.bluesky_engagement, a, b, s, gd + dt.timedelta(days=day + 2), 3, na, nb)
                except Exception:
                    raw[g["id"]] = {k: None for k in SIGNALS}
                time.sleep(0.4)
                done += 1
            for k in SIGNALS:
                STATS[k] += sum(1 for v in raw.values() if v.get(k) is not None)
                NONZERO[k] += sum(1 for v in raw.values() if v.get(k))
                if k not in ("yt", "clips") or day <= 7:
                    TRIES[k] += len(raw)
            pcts = {k: B.percentile_within({gid: v.get(k) for gid, v in raw.items()}) for k in SIGNALS}
            for g in games:
                g.setdefault("buzz_raw", {})[key] = raw[g["id"]]
                g.setdefault("buzz", {"d1": None, "d7": None, "d30": None})
                g["buzz"][key] = B.combine([pcts[k].get(g["id"]) for k in SIGNALS])


_ROSTERS = {}


def roster_names(gid):
    """その試合に出た選手の名前(チームごと)。切り抜きのタイトルが両チームの試合のものか確かめるのに使う"""
    if gid not in _ROSTERS:
        try:
            box = box_players(get(f"{API}/summary?event={gid}"))
            _ROSTERS[gid] = {ab: [r["name"] for r in rows if not r["dnp"]] for ab, rows in box.items()}
        except Exception:
            _ROSTERS[gid] = {}
    return _ROSTERS[gid]


def star_names(abbr):
    """余韻を探すときに使う主力選手の名前(エース表の順)"""
    return [p["name"] for p in star_list(abbr)]


CLIP_HIST = os.path.join(HERE, "clip_hist.json")   # 試合ごとの切り抜き動画の再生数の合計(チームのいつもの伸びを知るため)


def team_clip_base(hist, abbr, before, exclude):
    """そのチームの、これより前の試合での切り抜き再生数(合計)の真ん中の値。足りなければ None"""
    import statistics
    vals = [h["t"] for gid, h in hist.items()
            if gid != exclude and h["d"] < before and abbr in (h["h"], h["a"]) and h.get("t")]
    return statistics.median(vals) if len(vals) >= CFG["moment"]["min_samples"] else None


def merge_clips(prev, res, date, home, away):
    """切り抜きの測定結果を前回までと合わせる。YouTubeの再生数は減らないので、
    動画ごとに「今まで見た一番多い再生数」を覚えておき、今回の検索に出てこなかった動画も数えたままにする
    (検索のたびに出てくる動画の顔ぶれが変わって合計がブレるのを防ぐ)"""
    vids = dict((prev or {}).get("v") or {})
    for vid, (v, names) in (res[3] if len(res) > 3 else {}).items():
        if vid not in vids or v > vids[vid][0]:
            vids[vid] = [v, names]
    per = {}
    for v, names in vids.values():
        for n in names:
            per[n] = per.get(n, 0) + v
    old_p = (prev or {}).get("p") or {}
    pmap = {**{n: x[0] for n, x in old_p.items()}, **{n: x[0] for n, x in (res[2] if len(res) > 2 else {}).items()}}
    p_out = {n: [pmap.get(n, "?"), max(per.get(n, 0), (old_p.get(n) or [None, 0])[1])] for n in set(per) | set(old_p)}
    total = max(sum(v for v, _ in vids.values()), (prev or {}).get("t") or 0)   # 前の測り方の合計より減らさない
    out = {"d": date, "h": home, "a": away, "t": total, "p": p_out, "v": vids}
    for k in ("seed",):
        if prev and k in prev:
            out[k] = prev[k]
    return out


def moment_scores(records):
    """名場面ボーナスを今の切り抜き再生数で付け直す(毎回。測り直すたびに数字が育つので)。
    倍率 = その試合の切り抜き再生数の合計 ÷ 両チームの「いつもの値」(それより前の試合の真ん中の値)の平均。
    同じ夜のほかの試合とは比べない(すごい試合が重なった夜は全部に付き、地味な夜は誰にも付かない)。
    再生数が min_views 未満は0点(もともと見られていないチームの「3倍」は名場面と言えないので)"""
    import statistics
    M = CFG["moment"]
    hist = load_json(CLIP_HIST, {})
    night_mid = {}
    for gid, h in hist.items():
        night_mid.setdefault(h["d"], []).append(h.get("t") or 0)
    night_mid = {d: statistics.median(v) for d, v in night_mid.items() if v}
    for r in records:
        h = hist.get(r["id"])
        if not h or not h.get("t"):
            continue
        bases = [team_clip_base(hist, ab, h["d"], r["id"]) or night_mid.get(h["d"]) for ab in (h["h"], h["a"])]
        if not all(bases):
            continue
        ratio = h["t"] / (sum(bases) / 2)
        bonus = 0
        if h["t"] >= M["min_views"]:
            bonus = M["bonus_big"] if ratio >= M["ratio_big"] else M["bonus"] if ratio >= M["ratio"] else 0
        r["moment"] = [bonus, round(ratio, 2), MOMENT_VER]


def measure_moments(records, today):
    """名場面:その試合の切り抜き動画(ショート・日本語の動画も含む)の再生数を、試合の1〜7日後に毎日1回測る。
    バズったプレーは色んな人が上げ直すので、1本だけでなく合計で見る。点数は moment_scores で付ける"""
    import time
    import buzz as B
    M = CFG["moment"]
    hist = load_json(CLIP_HIST, {})
    info = team_info()
    for r in sorted(records, key=lambda r: r["date"]):
        gd = dt.date.fromisoformat(r["date"])
        age = (today - gd).days
        if age < 1 or age > M.get("measure_days", 7):
            continue
        prev = hist.get(r["id"])
        if prev and prev.get("m") == today.isoformat():   # 今日はもう測った
            continue
        try:
            res = B.youtube_clip_views(info[r["away"]], info[r["home"]], gd,
                                       star_names(r["away"]), star_names(r["home"]), r["away"], r["home"],
                                       roster_names(r["id"]).get(r["away"], ()), roster_names(r["id"]).get(r["home"], ()))
        except Exception:
            res = None
        MOMENT_STATS["tries"] += 1
        MOMENT_STATS["ok"] += 1 if res else 0
        if res:
            hist[r["id"]] = merge_clips(prev, res, r["date"], r["home"], r["away"])
            hist[r["id"]]["m"] = today.isoformat()
        time.sleep(0.4)
    json.dump(hist, open(CLIP_HIST, "w"), separators=(",", ":"))


# ---------- ランク(比べる範囲で決める) ----------
def week_start(d):
    return d - dt.timedelta(days=d.weekday())


def assign_tiers_many(jobs, prev_records, today):
    """ランク付け。計算はアプリと同じ docs/tiers.js を node で動かす(2か所に書いてずれないように)。
    jobs = [試合記録のリスト, ...]。各記録に tier・ss_confirmed・season_rank・month_rank・up_from・story を書き込む"""
    import subprocess
    prev = prev_records if isinstance(prev_records, dict) else {}
    if isinstance(prev_records, list):
        for r in prev_records:
            prev.setdefault(r["type"], []).append(fs(r))
    opts = {"T": T, "R": R, "prev": prev, "today": today.isoformat(), "upgrades": load_json(UPGRADES, {})}
    payload = {"jobs": [{"records": [{"id": r["id"], "date": r["date"], "season": r["season"], "type": r["type"],
                                       "score": fs(r), "legend": bool(r.get("legend")), "hall_ok": hall_ok(r)}
                                      for r in recs], "opts": opts} for recs in jobs]}
    res = subprocess.run(["node", os.path.join(HERE, "tiers_cli.js")], input=json.dumps(payload),
                         capture_output=True, text=True, check=True)
    for recs, out in zip(jobs, json.loads(res.stdout)):
        by = {o["id"]: o for o in out}
        for r in recs:
            o = by[r["id"]]
            for k in ("tier", "ss_confirmed", "season_rank", "month_rank"):
                r[k] = o[k]
            if o["up_from"]:
                r["up_from"], r["story"] = o["up_from"], o["story"]
    return jobs


def assign_tiers(records, prev_records, today):
    assign_tiers_many([records], prev_records, today)
    return records


TIER_RANK = {"-": 0, "C": 1, "B": 2, "A": 3, "S": 4, "SS": 5}
UPGRADES = os.path.join(HERE, "upgrades.json")   # 月末の決選投票で決まった格上げ(vote.py が書く)


def hall_ok(r):
    """SS は余韻(民衆の声):30日後もまだ語られていたら殿堂入り"""
    d30 = (r.get("buzz") or {}).get("d30")
    return d30 is not None and d30 >= CFG["buzz"]["hall_min"]


# ---------- 画面用の1試合 ----------
CAREER = {}   # 選手id → {"hi": 今季より前の最高得点, "gp": 今季より前の試合数}


def career_before(pid, season):
    """その選手の、今季より前のNBAでの最高得点と試合数(レギュラーシーズン+プレーオフ。プレシーズンは除く)。
    過去の成績は変わらないので保存して使い回す"""
    key = f"{pid}_{season}"
    if key in CAREER:
        return CAREER[key]
    path = _cache_path("career", "v2_" + key)   # v2 = 通算得点と所属チームの歴史も持つ版
    if os.path.exists(path):
        CAREER[key] = _cache_load(path)
        return CAREER[key]
    url = f"https://site.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{pid}/gamelog"
    first = get(url)
    opts = next((f["options"] for f in first.get("filters", []) if f.get("name") == "season"), [])
    hi, gp, tot = 0, 0, 0
    stints = []   # 所属チームの歴史 [略称, 最後の試合日, 試合数](古い順)
    for o in sorted(opts, key=lambda o: str(o.get("value"))):
        try:
            y = int(o["value"])
        except Exception:
            continue
        if y >= season:
            continue
        d = get(f"{url}?season={y}")
        i = d.get("names", []).index("points") if "points" in d.get("names", []) else None
        for stype in d.get("seasonTypes", []):
            if "Preseason" in stype.get("displayName", ""):
                continue
            for cat in stype.get("categories", []):
                for ev in cat.get("events", []):
                    try:
                        p = int(float(ev["stats"][i]))
                        hi = max(hi, p); gp += 1; tot += p
                    except Exception:
                        pass
        evs = sorted(((e.get("gameDate", ""), (e.get("team") or {}).get("abbreviation")) for e in d.get("events", {}).values()))
        for day, ab in evs:
            if not ab:
                continue
            if stints and stints[-1][0] == ab:
                stints[-1][1] = day[:10]; stints[-1][2] += 1
            else:
                stints.append([ab, day[:10], 1])
    CAREER[key] = {"hi": hi, "gp": gp, "pts": tot, "teams": stints}
    _cache_save(path, CAREER[key])
    return CAREER[key]


def load_careers(records, limit, first=()):
    """キャリアハイ・記録目前・古巣対決に必要な選手の過去の成績を集める。1回の更新で limit 人まで。
    first = 先に集める選手(エース表の選手・日本人選手)。ほかは25点以上取った試合がある選手"""
    season = max((r.get("season") or 0) for r in records) if records else 0
    want = [(pid, season) for pid in first]
    for r in records:
        for lu in (r.get("lineup") or {}).values():
            for pid, (nm, pts) in (lu.get("pts") or {}).items():
                if pts >= R["career_high_min_pts"] and f"{pid}_{r.get('season')}" not in CAREER:
                    want.append((pid, r.get("season")))
    done = 0
    for pid, season in dict.fromkeys(want):
        if done >= limit and not os.path.exists(_cache_path("career", f"v2_{pid}_{season}")):
            continue
        try:
            fresh = not os.path.exists(_cache_path("career", f"v2_{pid}_{season}"))
            career_before(pid, season)
            done += 1 if fresh else 0
        except Exception:
            pass


def apply_surprise(records):
    """② の追加:その選手にしては大爆発した夜(覚醒)は、格が低くても倍率を引き上げる。
    大爆発 = 爆発が始まる前の今季平均(surprise_min_games 試合以上)の surprise_ratio 倍以上、かつ surprise_min_pts 点以上。
    1回目は最低 tier_mult.surprise1(エース級)、2試合連続から最低 tier_mult.surprise(AS級)。連続が続く限りAS級扱い。
    何度計算しても同じになるよう content_raw から計算し直す"""
    hist, streak = {}, {}   # (シーズン, 選手id) → それまでの得点 / {"n": 連続回数, "base": 爆発前の平均}
    season_best = hist      # 今季のそれまでの得点(キャリアハイの判定にも使う)
    def judge(key, raw):
        h, st = hist.get(key, []), streak.get(key, {"n": 0, "base": None})
        if len(h) < R["surprise_min_games"]:
            return False, None, 0
        base = st["base"] if st["n"] else sum(h) / len(h)
        ok = base > 0 and raw >= R["surprise_min_pts"] and raw >= R["surprise_ratio"] * base
        return ok, base, (st["n"] + 1 if ok else 0)
    for r in sorted(records, key=lambda r: (r["date"], r["id"])):
        base_pts = r.get("content_raw", r.get("content_base", r["content"]))
        bd = r.get("bd")
        delta = 0.0
        if bd:
            bd["stars"]["items"] = [x for x in bd["stars"]["items"] if not x.get("added")]   # 前回足したキャリアハイ分は消して作り直す
            # キャリアハイ(その選手の今季より前の最高を超えた夜。25点以上、過去に50試合以上の選手だけ)
            for ab, lu in (r.get("lineup") or {}).items():
                for pid, (nm, pts) in (lu.get("pts") or {}).items():
                    c = CAREER.get(f"{pid}_{r.get('season')}")
                    if not c or pts < R["career_high_min_pts"] or c["gp"] < R["career_high_min_games"]:
                        continue
                    best = max([c["hi"]] + season_best.get((r.get("season"), pid), []))
                    if pts <= best:
                        continue
                    x = next((x for x in bd["stars"]["items"] if x.get("k") == "star" and x.get("id") == pid), None)
                    if x:
                        x["career_high"] = best
                    else:
                        m = CFG["tier_mult"][tier_of(ab, nm)]
                        bd["stars"]["items"].append({"k": "star", "id": pid, "who": nm, "team": ab, "why": ["キャリアハイ"],
                                                     "base": P["career_high"], "mult": m, "mult0": m,
                                                     "pts": round(P["career_high"] * m, 1), "raw": pts,
                                                     "career_high": best, "added": True})
            items = [x for x in bd["stars"]["items"] if x.get("k") == "star"]
            mom = sum(x["pts"] for x in bd["stars"]["items"] if x.get("k") == "moment")
            for x in items:   # まず元の倍率に戻す(前回の計算が残っていても同じ結果になるように)
                x.pop("surprise", None); x.pop("streak", None)
                x["mult"] = x.get("mult0", x["mult"]); x["mult0"] = x["mult"]; x["pts"] = round(x["base"] * x["mult"], 1)
            before = min(P["star_cap"], sum(x["pts"] for x in items if not x.get("added")) + mom)
            for x in items:
                ok, avg, n = judge((r.get("season"), x.get("id")), x.get("raw", 0))
                if ok:
                    floor = CFG["tier_mult"]["surprise"] if n >= 2 else CFG["tier_mult"]["surprise1"]
                    if x["mult"] < floor:   # もともと格が高い選手は変わらない(内訳にも出さない)
                        x["mult"] = floor
                        x["pts"] = round(x["base"] * x["mult"], 1)
                        x["surprise"], x["streak"] = round(avg, 1), n
                if x.get("career_high") is not None and x["mult"] < CFG["tier_mult"]["surprise1"]:
                    x["mult"] = CFG["tier_mult"]["surprise1"]   # キャリアハイの夜も、1回目の大爆発と同じく最低エース級
                    x["pts"] = round(x["base"] * x["mult"], 1)
            after = min(P["star_cap"], sum(x["pts"] for x in items) + mom)
            bd["stars"]["pts"] = round(after, 1)
            delta = after - before
        r["content_raw"] = base_pts
        r["content_base"] = round(base_pts + delta, 1)
        r["content"] = r["content_base"]
        # この試合の結果で、各選手の平均と連続記録を更新
        for lu in (r.get("lineup") or {}).values():
            for pid, (nm, pts) in (lu.get("pts") or {}).items():
                key = (r.get("season"), pid)
                ok, avg, n = judge(key, pts)
                streak[key] = {"n": n, "base": avg if ok else None}
                hist.setdefault(key, []).append(pts)
    return records


def apply_absence(records):
    """⑥ いつものスタメンの欠場。
    いつものスタメン = そのチームの直近 usual_window 試合で、半分以上スタメンだった選手(usual_min_games 試合たってから)。
    その選手が試合に出なかったら1人 starter_out 点(1チーム starter_out_cap まで)。エース表の選手はカードの格で
    もう引いているので数えない。両チームとも depleted_min 人以上欠けていたら、さらに both_depleted 点。
    展開の点が absence_soften_flow 以上の試合(控え中心でももつれた試合は面白い)は減点を absence_soften_mult 倍にする。
    何度計算しても同じになるよう、元の点(content_base)から引き直す"""
    starts = {}   # (シーズン, チーム) → 直近の試合のスタメン一覧
    for r in sorted(records, key=lambda r: (r["date"], r["id"])):
        r.setdefault("content_base", r["content"])   # apply_surprise が先に正しい値を入れる
        lu = r.get("lineup") or {}
        missing = {}
        for ab in (r["home"], r["away"]):
            hist = starts.get((r["season"], ab), [])[-R["usual_window"]:]
            if len(hist) < R["usual_min_games"] or ab not in lu:
                continue
            cnt, names = {}, {}
            for st in hist:
                for pid, nm in st.items():
                    cnt[pid] = cnt.get(pid, 0) + 1
                    names[pid] = nm
            # カードの格で引いているのはエース(と、エースも休んだときのセカンド)だけ。3番手以降のスターはここで数える
            lst, outs = star_list(ab), set(lu[ab].get("out", []))
            aces = set()
            if lst:
                aces.add(norm(lst[0]["name"]))
                if len(lst) >= 2 and norm(lst[0]["name"]) in outs and norm(lst[1]["name"]) in outs:
                    aces.add(norm(lst[1]["name"]))
            usual = [pid for pid, c in cnt.items() if c / len(hist) >= R["usual_starter_frac"]]
            missing[ab] = [names[pid] for pid in usual
                           if pid not in lu[ab]["pl"] and norm(names[pid]) not in aces]
        pen = sum(max(P["starter_out_cap"], P["starter_out"] * len(m)) for m in missing.values())
        if len(missing) == 2 and all(len(m) >= R["depleted_min"] for m in missing.values()):
            pen += P["both_depleted"]
        if r["flow"] >= R["absence_soften_flow"]:
            pen *= R["absence_soften_mult"]
        r["absence"] = round(pen, 1)
        r["absent"] = {ab: m for ab, m in missing.items() if m}   # 確認用(画面には渡さない)
        r["content"] = round(max(0, r["content_base"] + pen), 1)
        for ab in (r["home"], r["away"]):
            if ab in lu:
                starts.setdefault((r["season"], ab), []).append(lu[ab]["st"])
    return records


COACH_BY_TEAMID = {}   # チームid → 今のHC(ロスターと一緒に取れる)
try:
    COACH_TIES = json.load(open(os.path.join(HERE, "coach_ties.json"), encoding="utf-8"))
except Exception:
    COACH_TIES = {"ties": [], "kinds": {}}


def fetch_roster(tid):
    """チームの選手名簿。取れたら保存しておき、取れなかったら前回の分を使う(1チームの失敗で全体を止めない)"""
    path = _cache_path("roster", str(tid))
    try:
        d = get(f"{API}/teams/{tid}/roster")
        _cache_save(path, {"coach": d.get("coach"), "athletes": d.get("athletes", [])})
    except Exception:
        STALE["roster"] += 1
        d = _cache_load(path) if os.path.exists(path) else {}
    c = (d.get("coach") or [{}])[0]
    if c.get("lastName"):
        COACH_BY_TEAMID[str(tid)] = f"{c.get('firstName', '')} {c['lastName']}".strip()
    return d.get("athletes", [])


def _cn(name):
    import unicodedata
    t = unicodedata.normalize("NFKD", name or "")
    return re.sub(r"[^a-z]", "", "".join(c for c in t if not unicodedata.combining(c)).lower())


def valid_ties(ids, roster_cache, pair=None):
    """今も成り立っているHCの因縁(HCが代わった・選手が移籍したものは出さない)。ids = チーム略称→チームid"""
    out = []
    for t in COACH_TIES.get("ties", []):
        if pair and set(t["teams"]) != set(pair):
            continue
        ok = all(_cn(COACH_BY_TEAMID.get(str(ids.get(ab)), "")) == _cn(nm) for ab, nm in t.get("coaches", {}).items())
        for ab, pl in t.get("players", {}).items():
            names = {_cn(a.get("displayName")) for a in roster_cache.get(ids.get(ab), [])}
            ok = ok and _cn(pl) in names
        if ok:
            out.append(dict(t, label=COACH_TIES.get("kinds", {}).get(t["kind"], "")))
    return sorted(out, key=lambda t: t["rank"])


def coaches_by_abbr():
    try:
        ids = {t["team"]["abbreviation"]: t["team"]["id"] for t in get(f"{API}/teams")["sports"][0]["leagues"][0]["teams"]}
    except Exception:
        return {}
    return {ab: COACH_BY_TEAMID.get(str(tid), "") for ab, tid in ids.items()}


def coach_ties_all(roster_cache):
    """使い方ページの一覧用(今も成り立っているものだけ)"""
    try:
        ids = {t["team"]["abbreviation"]: t["team"]["id"] for t in get(f"{API}/teams")["sports"][0]["leagues"][0]["teams"]}
    except Exception:
        return []
    return [{"teams": t["teams"], "label": t["label"], "text": t["text"]} for t in valid_ties(ids, roster_cache)]


def star_jerseys(tinfo, roster_cache):
    """エース表の選手の背番号(背番号アイコン用)。今日〜あさってに試合がないチームのロスターもここで取る"""
    ids = {}
    try:
        for t in get(f"{API}/teams")["sports"][0]["leagues"][0]["teams"]:
            ids[t["team"]["abbreviation"]] = t["team"]["id"]
    except Exception:
        pass
    out = {}
    for ab, lst in STARS.items():
        if ab.startswith("_") or ab not in ids:
            continue
        tid = ids[ab]
        if tid not in roster_cache:
            try:
                roster_cache[tid] = fetch_roster(tid)
            except Exception:
                roster_cache[tid] = []
        by = {norm(a["displayName"]): a.get("jersey", "") for a in roster_cache[tid]}
        out[ab] = {p["name"]: by.get(norm(p["name"]), "") for p in lst}
    return out


JP_NAMES = {"Rui Hachimura": "八村塁", "Yuki Kawamura": "河村勇輝", "Keisei Tominaga": "富永啓生", "Yuta Watanabe": "渡邊雄太"}
EVENTS = os.path.join(HERE, "events.json")   # 人が入れる小話(永久欠番セレモニーなど){"items": [{date, teams, text, big}]}


def _milestone(total):
    step = 5000 if total >= 10000 else 1000
    return (total // step + 1) * step


def add_trivia(games_days, records, roster_cache):
    """試合ごとの小話(ちなみに)。試合前にわかることだけ(終わった試合も、その試合より前の情報で作る)。
    big=True は見どころの一言にも出す大きい小話"""
    team_ja = load_json(os.path.join(HERE, "names_ja.json"), {}).get("teams", {})
    events = load_json(EVENTS, {}).get("items", [])
    star_set = {norm(p["name"]) for ab, lst in STARS.items() if not ab.startswith("_") for p in lst}
    recs = sorted(records, key=lambda r: r["date"])
    season_now = max((r.get("season") or 0) for r in recs) if recs else 0
    for day in games_days:
        for g in day["games"]:
            items = []
            gd = g["date"]
            us = (dt.date.fromisoformat(gd) - dt.timedelta(days=1)).strftime("%m-%d")
            ab_ids = {t["abbr"]: t["id"] for t in g["teams"]}
            outs = {ab: {norm(p["name"]) for p in g["rosters"].get(ab, []) if p.get("status") in ("欠場", "出場停止")} for ab in ab_ids}
            before = [r for r in recs if r["date"] < gd]
            # 人が入れた小話(セレモニーなど)
            us_full = (dt.date.fromisoformat(gd) - dt.timedelta(days=1)).isoformat()
            ev_players = set()
            for ev in events:   # date=日本の日付、us=アメリカの日付(どちらか)。teamsは両チームとも含まれていること
                hit_date = ev.get("date") == gd or ev.get("us") == us_full
                if hit_date and set(ev.get("teams", [])) <= set(ab_ids) and ev.get("teams"):
                    items.append({"kind": ev.get("kind", "イベント"), "text": ev["text"], "big": bool(ev.get("big", True)),
                                  "rank": 0, "heat": bool(ev.get("heat", True))})
                    if ev.get("player"):
                        ev_players.add(norm(ev["player"]))
            # HCの因縁(全部)
            for t in valid_ties(ab_ids, roster_cache, list(ab_ids)):
                items.append({"kind": t["label"], "text": t["text"], "big": t["rank"] <= 2, "rank": 10 + t["rank"]})
            # その会場での迎えられ方(ブーイング/スタンディングオベーション)。選手がアウェイで乗り込む試合だけ
            home = next(t["abbr"] for t in g["teams"] if t["homeAway"] == "home")
            away = next(t["abbr"] for t in g["teams"] if t["homeAway"] == "away")
            away_names = {norm(a.get("displayName", "")): a.get("displayName") for a in roster_cache.get(ab_ids[away], [])}
            for rc in load_json(os.path.join(HERE, "receptions.json"), {}).get("items", []):
                nm = away_names.get(norm(rc["player"]))
                if rc["arena"] == home and nm and norm(nm) not in outs.get(away, set()):
                    if rc["type"] == "boo":
                        items.append({"kind": "ブーイング", "text": f"{short_name(nm)}はこの会場では毎回ブーイング({rc['why']})", "big": True, "rank": 1, "heat": True})
                    else:
                        items.append({"kind": "大歓迎", "text": f"{short_name(nm)}がここに帰ってくると毎回スタンディングオベーション({rc['why']})", "big": True, "rank": 1, "heat": True})
            for ab, tid in ab_ids.items():
                opp = next(x for x in ab_ids if x != ab)
                hist_games = [r for r in before if ab in (r["home"], r["away"]) and ab in (r.get("lineup") or {})]
                for a in roster_cache.get(tid, []):
                    nm, pid = a.get("displayName", ""), a.get("id")
                    out = norm(nm) in outs.get(ab, set())
                    is_star = norm(nm) in star_set
                    jp = (a.get("birthPlace") or {}).get("country") == "Japan"
                    if jp and not out:
                        items.append({"kind": "日本人選手", "text": f"{JP_NAMES.get(nm, nm)}({ab})" + ("のチームの試合" if g.get("completed") else "が出場予定"),
                                      "big": True, "rank": 5})
                    dob = (a.get("dateOfBirth") or "")[:10]
                    if dob[5:] == us and not out and (is_star or jp):
                        age = int(gd[:4]) - int(dob[:4]) - (1 if gd[5:] < dob[5:] else 0)
                        items.append({"kind": "誕生日", "text": f"{short_name(nm)}の{age}歳の誕生日", "big": False, "rank": 40})
                    if not (is_star or jp) or out:
                        continue
                    c = CAREER.get(f"{pid}_{season_now}")
                    season_pts = [lu["pts"][pid][1] for r in before for lu in (r.get("lineup") or {}).values()
                                  if pid in (lu.get("pts") or {})]
                    # 長期離脱からの復帰(チームの直近8試合以上に出ていない主力が、出場予定に戻ってきた)
                    miss = 0
                    for r in reversed(hist_games):
                        if pid in r["lineup"][ab].get("pl", []):
                            break
                        miss += 1
                    if miss >= 8 and len(hist_games) > miss:
                        items.append({"kind": "復帰", "text": f"{short_name(nm)}が{miss}試合ぶりの復帰見込み", "big": True, "rank": 2})
                    if c:
                        # 記録目前(通算得点のキリのいい数字まであと少し)
                        tot = c.get("pts", 0) + sum(season_pts)
                        if tot >= 5000 and "pts" in c:
                            goal = _milestone(tot)
                            left = goal - tot
                            avg = (sum(season_pts) / len(season_pts)) if season_pts else (c["pts"] / max(1, c["gp"]))
                            if avg > 0 and left <= 1.3 * avg:
                                items.append({"kind": "記録目前", "text": f"{short_name(nm)}、通算{goal:,}点まであと{left}点。今日届くかも",
                                              "big": True, "rank": 3})
                            elif avg > 0 and left <= 4 * avg and tot >= 10000:
                                items.append({"kind": "記録目前", "text": f"{short_name(nm)}、通算{goal:,}点まであと{left}点",
                                              "big": False, "rank": 30})
                        # 古巣対決(過去に相手チームに在籍していた)
                        stints = list(c.get("teams", []))
                        for r in before:   # 今季の途中移籍も足す
                            for tab, lu in (r.get("lineup") or {}).items():
                                if pid in (lu.get("pts") or {}):
                                    if stints and stints[-1][0] == tab:
                                        stints[-1] = [tab, r["date"], stints[-1][2] + 1]
                                    else:
                                        stints.append([tab, r["date"], 1])
                        past = [x for x in stints if x[0] == opp]
                        if past and (not stints or stints[-1][0] != opp):
                            games = sum(x[2] for x in past)
                            last_other = [x for x in stints if x[0] != ab]
                            homecoming = bool(last_other) and last_other[-1][0] == opp and \
                                not any(opp in (r["home"], r["away"]) for r in hist_games)
                            jn = team_ja.get(opp, opp)
                            if homecoming and norm(nm) not in ev_players:   # 「元◯◯」だけの小話はみんな知ってるので出さない。移籍後はじめての古巣戦だけ
                                items.append({"kind": "古巣対決", "text": f"{short_name(nm)}、移籍後はじめての古巣{jn}戦", "big": True, "rank": 1, "heat": True})
            items.sort(key=lambda x: x["rank"])
            # 会場が熱くなる小話(ブーイング・大歓迎・古巣への凱旋・セレモニー)がある試合は、期待度を★1つ上げる。
            # 試合後のランク(点数)には入れない(盛り上がるのは試合前の空気で、試合の中身とは別なので)
            heat = next((x for x in items if x.get("heat")), None)
            if heat and not g.get("completed") and g.get("expect"):
                g["expect"] = min(3, g["expect"] + 1)
            g["heat"] = heat["text"] if heat else None
            g["trivia"] = [{k: v for k, v in x.items() if k not in ("rank", "heat")} for x in items]
            big = next((x for x in items if x["big"]), None)
            if g.get("preview") is not None and isinstance(g["preview"], dict):
                g["preview"]["tie"] = {"label": big["kind"], "text": big["text"]} if big else None


def preview_line(teams, out_pre, pdet, clash, roster_cache=None):
    """試合前の見どころ(ネタバレにならない、試合前にわかることだけ)"""
    order = {"mvp": 0, "allstar": 1, "ace": 2}
    tops = []
    for t in sorted(teams, key=lambda t: t["homeAway"] != "away"):   # アウェイ vs ホーム の順
        avail = [p for p in star_list(t["abbr"]) if norm(p["name"]) not in out_pre.get(t["abbr"], set())]
        avail.sort(key=lambda p: order.get(p["tier"], 3))
        tops.append([t["abbr"], avail[0]["name"], short_name(avail[0]["name"])] if avail else None)
    out = []
    riv = load_json(os.path.join(HERE, "rivalries.json"), {}).get("-".join(sorted(t["abbr"] for t in teams)))
    if riv and riv.get("type") == "hot":
        out.append(f"因縁:{riv.get('name')}")
    for x in pdet:
        if x["txt"] == "両チーム勝率5割以上":
            out.append("両チーム勝ち越し")
        elif x["txt"].startswith("NBAカップ") or "争いの直接対決" in x["txt"]:
            out.insert(0, x["txt"])
        elif "欠場" in x["txt"]:
            ab = x["txt"].split(":")[0]
            lst = star_list(ab)
            outs = [short_name(p["name"]) for p in lst[:2] if norm(p["name"]) in out_pre.get(ab, set())]
            if outs:
                out.append(f"{'と'.join(outs)}欠場")
    if clash:
        out.append("強み同士の対決")
    ties = valid_ties({t["abbr"]: t["id"] for t in teams}, roster_cache or {}, [t["abbr"] for t in teams])
    tie = {"label": ties[0]["label"], "text": ties[0]["text"]} if ties else None
    return {"vs": tops if all(tops) else None, "rest": " / ".join(out[:3]), "tie": tie}


def build_game_safe(e, start, season, strong, roster_cache):
    """1試合の取得に失敗しても、その回の更新全体は止めない。
    中身が取れなかった試合は対戦カードと時刻だけ出し(stale)、記録(採点)は前回のまま"""
    try:
        return build_game(e, start, season, strong, roster_cache)
    except Exception:
        STALE["game"] += 1
        try:
            g, _ = build_game(e, start, season, strong, roster_cache, summary={"_stale": True, "_failed": True})
            return g, None
        except Exception:
            return None, None


def build_game(e, start, season, strong, roster_cache, summary=None):
    comp = e["competitions"][0]
    state = comp["status"]["type"]["state"]
    completed = comp["status"]["type"].get("completed", False)
    if summary is None:
        summary = get_summary(e["id"], completed, fresh=True)
    failed = bool(summary.get("_failed"))
    teams, home_id, winning = parse_teams(comp, completed)

    inj = {}
    for blk in summary.get("injuries", []):
        ab = blk["team"]["abbreviation"]
        for i in blk.get("injuries", []):
            nm = i.get("athlete", {}).get("displayName", "")
            det = i.get("details", {}) or {}
            # 注意:ESPNの欠場コメント(文章)には試合結果が書かれていることがあるので使わない。項目だけ使う
            inj.setdefault(ab, {})[norm(nm)] = {
                "status": INJ_JA.get(i.get("status"), i.get("status", "")),
                "detail": injury_reason(det),
                "return": return_label(det.get("returnDate"), start.date())}
    out_pre = {ab: {k for k, v in d.items() if v["status"] in ("欠場", "出場停止")} for ab, d in inj.items()}

    rosters = {}
    for t in teams:
        if t["id"] not in roster_cache:
            roster_cache[t["id"]] = fetch_roster(t["id"])
        rows = []
        for a in roster_cache[t["id"]]:
            st = inj.get(t["abbr"], {}).get(norm(a["displayName"]))
            rows.append({"name": a["displayName"], "jersey": a.get("jersey", ""),
                         "pos": a.get("position", {}).get("abbreviation", ""),
                         "status": st["status"] if st else "", "detail": st["detail"] if st else "",
                         "ret": st["return"] if st else "",
                         "tier": tier_of(t["abbr"], a["displayName"])})
        order = {"mvp": 0, "allstar": 1, "ace": 2, "other": 3}
        rows.sort(key=lambda r: (order[r["tier"]], r["name"]))
        rosters[t["abbr"]] = rows

    pdet = []
    card_pre = card_quality(teams, out_pre, winning, pdet, context_items(comp, teams, start.date()))
    clash = P["strength_clash"] if all(strong.get(t["abbr"]) for t in teams) else 0
    st = e.get("season", {})
    g = {
        "id": e["id"], "date": start.date().isoformat(), "start": start.strftime("%H:%M"), "start_utc": e.get("date"),
        "type": game_type(e) or "other", "cup": cup_stage(comp),
        "state": state, "completed": completed,
        "venue": comp.get("venue", {}).get("fullName", ""),
        "teams": [{k: v for k, v in t.items() if not k.startswith("_")} for t in teams],
        "rosters": rosters,
        "strengths": {t["abbr"]: strong.get(t["abbr"], []) for t in teams},
        "expect": expect_stars(card_pre, clash),
        "preview": preview_line(teams, out_pre, pdet, clash, roster_cache),
        "result": None,
    }
    if summary.get("_stale"):
        # 今回はESPNから取れなかったので前回の情報(欠場など)を出している/何も取れなかった
        g["stale"] = "nodata" if failed else "prev"
    rec = None
    if completed and not failed:
        rec = score_record(e, start, summary, season, strong)
        box = box_players(summary)
        lines = {}
        for c in comp["competitors"]:
            lines[c["team"]["abbreviation"]] = [int(num(x.get("value", x.get("displayValue", 0)))) for x in c.get("linescores", [])]
        bx = {ab: [{"name": r["name"], "starter": r["starter"], "dnp": r["dnp"], "reason": r["reason"],
                    **{k: r["stats"].get(k, "") for k in ("MIN", "PTS", "REB", "AST", "STL", "BLK", "FG", "3PT", "FT", "TO", "+/-")}}
                   for r in rows] for ab, rows in box.items()}
        # ボックススコアに載らない欠場者(ケガ人リストの欠場・エース表なのにいない選手)も「欠場」として下に足す
        for ab, rows in bx.items():
            present = {norm(r["name"]) for r in rows}
            extra = []
            for p in roster_cache.get(next((t["id"] for t in teams if t["abbr"] == ab), None), []):
                nm = p["displayName"]
                if norm(nm) in present:
                    continue
                st = inj.get(ab, {}).get(norm(nm))
                if st and st["status"] in ("欠場", "出場停止"):
                    extra.append({"name": nm, "dnp": True, "absent": True,
                                  "reason": st["status"] + (f"・{st['detail']}" if st["detail"] else "")})
                elif tier_of(ab, nm) != "other":
                    extra.append({"name": nm, "dnp": True, "absent": True, "reason": "欠場・理由の発表なし"})
            rows.extend(extra)
        g["result"] = {
            "score": {t["abbr"]: t["_score"] for t in teams},
            "linescores": lines,
            "box": bx,
        }
    return g, rec


def suggested_pairs():
    """みんなから届いた因縁の候補を、カードごとに件数だけまとめる(本文は画面に出さない)"""
    items = load_json(os.path.join(HERE, "suggestions.json"), {}).get("items", [])
    out = {}
    for i in items:
        if i.get("kind") == "rivalry" and i.get("status") != "rejected":
            o = out.setdefault(i["pair"], {"hot": 0, "tradition": 0})
            o[i.get("heat", "hot")] += 1
    return out


# ---------- 記録の読み書き ----------
def load_json(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def is_fav(rec, favs=None):
    favs = CFG["favorites"] if favs is None else favs
    return rec["home"] in favs or rec["away"] in favs


NOW = dt.datetime.now(JST)


def is_hidden(rec, favs=None):
    """お気に入りの試合は、終わってから24時間は判定・比較・通知から隠す。24時間たったら表に入れる。
    終わった時刻はアプリに渡さない(延長のヒントになる)ので、アプリと同じく「始まった時刻+余裕+24時間」で数える"""
    if not is_fav(rec, favs):
        return False
    start = rec.get("start_utc")
    if not start:
        return True
    return NOW < jst(start) + dt.timedelta(hours=CFG.get("favorite_hide_hours", 24) + CFG.get("favorite_hide_pad_hours", 3))


def backfill(dates, season, strong, archive, label=None, fallback=None):
    """fallback = 前回の記録。取れなかった試合は前回の記録を使う(黙って消えて順位がずれないように)"""
    fallback = fallback or {}
    failed = []
    def one(d):
        out = []
        try:
            evs = events_for_jst_date(d)
        except Exception:
            out.extend(r for r in fallback.values() if r["date"] == d.isoformat())
            failed.append(d.isoformat())
            return out
        for e, start in evs:
            comp = e["competitions"][0]
            if not comp["status"]["type"].get("completed"):
                continue
            if game_type(e) in ("pre", None):
                continue
            try:
                fresh = (_today_jst() - start.date()).days <= FRESH_DAYS
                summary = get_summary(e["id"], True, fresh)
                out.append(score_record(e, start, summary, season, strong, label))
            except Exception:
                if e["id"] in fallback:
                    out.append(fallback[e["id"]])
                failed.append(e["id"])
        return out
    with ThreadPoolExecutor(8) as ex:
        for recs in ex.map(one, dates):
            for r in recs:
                old = archive.get(r["id"], {})
                r["buzz"] = old.get("buzz", r["buzz"])
                if "buzz_raw" in old:
                    r["buzz_raw"] = old["buzz_raw"]
                archive[r["id"]] = r
    if failed:
        print(f"backfill: {len(failed)} 件取れず(前回の記録を使用)")


def season_dates(any_us_date):
    """ESPNのカレンダーからシーズン中の試合日(アメリカの日付)を取る"""
    sb = get(f"{API}/scoreboard?dates={any_us_date}")
    cal = sb["leagues"][0].get("calendar", [])
    ds = set()
    for c in cal:
        s = c if isinstance(c, str) else c.get("startDate")
        if s:
            ds.add(jst(s).date())
    return sorted(ds)


# ---------- 通知文 ----------
def tomorrow_lines(games):
    """21時の通知の「明日の見どころ」。まだ始まっていない試合なので、期待度と小話だけ(結果は出ようがない)"""
    pick = [g for g in games if g.get("expect", 0) >= 3 or g.get("heat")]
    pick.sort(key=lambda g: (-(g.get("expect") or 0), not g.get("heat"), g.get("start", "")))
    out = []
    for g in pick[:4]:
        home = next(t["abbr"] for t in g["teams"] if t["homeAway"] == "home")
        away = next(t["abbr"] for t in g["teams"] if t["homeAway"] == "away")
        big = next((x for x in g.get("trivia", []) if x.get("big")), None)
        t = (g.get("heat") or (big or {}).get("text") or "")
        note = f"\n  └ {t[:60]}{'…' if len(t) > 60 else ''}" if t else ""
        out.append(f"{g.get('start','')} {away} @ {home} {'★' * (g.get('expect') or 1)}{' 🔥' if g.get('heat') else ''}{note}")
    return out


NOTIFY_STATE = os.path.join(HERE, "notify_state.json")   # どの時間の通知を送ったか(同じ時間に2回送らない)


def notify_due(now):
    """今この回で送るべき定期通知の時間(9・12・15・18・21時のどれか)。
    GitHubの定期実行は遅れたり抜けたりするので、「その時間の lead 分前を過ぎて、まだ送っていなければ送る」。
    遅れすぎ(max_late 分以上)なら、その時間の分は送らない(次の時間を待つ)"""
    lead = dt.timedelta(minutes=CFG.get("notify_lead_minutes", 20))
    max_late = dt.timedelta(minutes=CFG.get("notify_max_late_minutes", 150))
    sent = load_json(NOTIFY_STATE, {}).get("last")
    for slot in sorted(CFG["notify_slots_jst"], reverse=True):
        t = now.replace(hour=slot, minute=0, second=0, microsecond=0) - lead
        if t <= now:
            key = f"{now.date().isoformat()}T{slot:02d}"
            if sent == key or now - t > max_late:
                return None
            return slot, key
    return None


def mark_notified(key):
    json.dump({"last": key}, open(NOTIFY_STATE, "w"))


def notify_text(now, games_today, arch, favs=None, tomorrow=None, slot=None):
    favs = CFG["favorites"] if favs is None else favs
    slots = CFG["notify_slots_jst"]
    cur = now.hour * 60 + now.minute
    if slot is None:
        slot = min(slots, key=lambda h: abs(h * 60 - cur))
    prev = [h for h in slots if h < slot]
    if prev:
        prev_t = now.replace(hour=max(prev), minute=0, second=0, microsecond=0)
    else:
        prev_t = (now - dt.timedelta(days=1)).replace(hour=slots[-1], minute=0, second=0, microsecond=0)
    done = [g for g in games_today if g["completed"]]
    lines = [f"【{slot}:00】本日の注目試合"]
    recs = [arch[g["id"]] for g in done if g["id"] in arch]
    ranked = sorted([r for r in recs if not is_hidden(r, favs) and r.get("tier") in CFG["notify_ranks"]],
                    key=lambda r: (-fs(r), str(r["id"])))
    names = CFG["tier_names"]
    for i, r in enumerate(ranked, 1):
        new = ""
        if r.get("end_utc") and jst(r["end_utc"]) > prev_t:
            new = " NEW"
        tag = r["tier"] + ("候補" if r["tier"] == "SS" and not r.get("ss_confirmed") else "")
        lines.append(f"{i}位 {tag}({names[r['tier']]}) {r['away']} @ {r['home']}{new}")
    if not ranked:
        lines.append("SS〜Aランクの試合はまだなし")
    if any(g["teams"][0]["abbr"] in favs or g["teams"][1]["abbr"] in favs for g in games_today):
        lines.append("推しチームの試合:対象外")
    lines.append(f"終了 {len(done)}/{len(games_today)}試合")
    if tomorrow and slot == slots[-1]:   # 夜の通知だけ、明日の見どころを足す
        tl = tomorrow_lines(tomorrow)
        if tl:
            lines.append("")
            lines.append("【明日の見どころ】")
            lines += tl
    return "\n".join(lines)


def bsky_daily(today, games_today, arch):
    """公式Blueskyに1日1回流す「今日の見るべき試合」(対戦カードとランクだけ。スコア・勝敗は書かない)"""
    favs = CFG["favorites"]
    done = [g for g in games_today if g["completed"]]
    recs = [arch[g["id"]] for g in done if g["id"] in arch and not is_hidden(arch[g["id"]], favs)]
    ranked = sorted([r for r in recs if r.get("tier") in CFG["notify_ranks"]], key=lambda r: (-fs(r), str(r["id"])))
    names = CFG["tier_names"]
    lines = []
    for r in ranked:
        tag = r["tier"] + ("候補" if r["tier"] == "SS" and not r.get("ss_confirmed") else "")
        up = f"・{r['up_from']}→{r['tier']}" if r.get("up_from") and r["up_from"] != r["tier"] else ""
        lines.append(f"{tag}({names[r['tier']]}{up}) {r['away']} @ {r['home']}")
    return {"date": today.isoformat(), "total": len(games_today), "done": len(done), "lines": lines}


PRE_STATE = os.path.join(HERE, "prenotify_state.json")   # 試合前の通知を同じ試合に2回送らないための記録


def prenotify_build(now, games, channel_keys):
    """試合前の通知。チャンネルごとの文面 {"_owner": 持ち主, "none"/"hou"…: みんなのチャンネル}。
    ・🔥か★3の試合(みんな共通)
    ・そのチャンネルの推しチームの試合(★に関係なく)
    どちらも始まる prenotify_minutes 分前まで。試合前なので結果は出ようがない。同じ試合には1回だけ送る"""
    win = CFG.get("prenotify_minutes", 50)
    state = load_json(PRE_STATE, {})
    def mins_to(g):
        if g.get("state") != "pre" or not g.get("start_utc"):
            return None
        m = (jst(g["start_utc"]) - now).total_seconds() / 60
        return m if 0 < m <= win else None
    def line(g, m, fav=False):
        home = next(t["abbr"] for t in g["teams"] if t["homeAway"] == "home")
        away = next(t["abbr"] for t in g["teams"] if t["homeAway"] == "away")
        big = next((x for x in g.get("trivia", []) if x.get("big")), None)
        t = g.get("heat") or (big or {}).get("text") or ""
        out = [f"{'【推し】' if fav else ''}{g['start']}開始(あと{int(m)}分) {away} @ {home} {'★' * (g.get('expect') or 1)}{' 🔥' if g.get('heat') else ''}"]
        if t:
            out.append(f"  └ {t[:60]}{'…' if len(t) > 60 else ''}")
        return out
    common = [(m, g) for g in games for m in [mins_to(g)]
              if m is not None and str(g["id"]) not in state and (g.get("expect", 0) >= 3 or g.get("heat"))]
    common.sort(key=lambda x: x[0])
    common_ids = {g["id"] for _, g in common}
    owner_fav = (CFG.get("favorites") or [None])[0]
    texts, fav_sent = {}, set()
    for key in ["_owner"] + list(channel_keys):
        fav = owner_fav if key == "_owner" else (None if key == "none" else key.upper())
        favs = [(m, g) for g in games for m in [mins_to(g)]
                if fav and m is not None and g["id"] not in common_ids and f"fav:{g['id']}" not in state
                and fav in {t["abbr"] for t in g["teams"]}]
        if not common and not favs:
            continue
        lines = ["まもなく開始"]
        for m, g in favs:
            lines += line(g, m, True)
            fav_sent.add(g["id"])
        for m, g in common:
            lines += line(g, m, fav in {t["abbr"] for t in g["teams"]})
        texts[key] = "\n".join(lines)
    today = now.date().isoformat()
    for _, g in common:
        state[str(g["id"])] = today
    for gid in fav_sent:
        state[f"fav:{gid}"] = today
    if texts:
        cut = (now.date() - dt.timedelta(days=10)).isoformat()   # 古い記録は捨てる
        json.dump({k: v for k, v in state.items() if v >= cut}, open(PRE_STATE, "w"))
    return texts


# ---------- メイン ----------
ALERT_STATE = os.path.join(HERE, "alert_state.json")   # 同じ知らせを何度も送らないための記録
SIGNAL_JA = {"news": "ニュース", "national": "地元以外のニュース", "yt": "フルハイライト動画", "clips": "切り抜き動画", "bsky": "Bluesky"}


def health_alerts():
    """余韻の測定が壊れていないかの見張り。測ろうとした数が少ないときは判定しない"""
    out = []
    for k in SIGNALS:
        n = TRIES[k]
        if n < 6:
            continue
        if STATS[k] / n < 0.3:
            out.append(f"・{SIGNAL_JA[k]}:{n}試合中{STATS[k]}試合しか測れてない(サイトの作りが変わったかも)")
        elif STATS[k] and NONZERO[k] == 0:
            out.append(f"・{SIGNAL_JA[k]}:{STATS[k]}試合測れたのに全部0(読み取り方が壊れてるかも)")
    if MOMENT_STATS["tries"] >= 6 and MOMENT_STATS["ok"] / MOMENT_STATS["tries"] < 0.3:
        out.append(f"・名場面(切り抜き):{MOMENT_STATS['tries']}試合中{MOMENT_STATS['ok']}試合しか測れてない")
    return out


def star_suggestions(records, today):
    """エース表の見直し候補(月1回)。中身(勝敗)は使わず、出場と平均得点だけ見る
    - 入れる候補:そのチームの直近15試合のうち8試合以上出て、平均22点以上なのにエース表にいない選手
    - 外す候補:エース表にいるのに、そのチームの直近8試合に1度も出ていない選手(長期離脱・トレードかも)"""
    by_team = {}
    for r in sorted(records, key=lambda r: r["date"]):
        if r.get("type") != "regular":
            continue
        for ab, lu in (r.get("lineup") or {}).items():
            by_team.setdefault(ab, []).append(lu.get("pts", {}))
    add, drop = [], []
    for ab, games in sorted(by_team.items()):
        listed = {norm(p["name"]) for p in star_list(ab)}
        last15 = games[-15:]
        tot, cnt, names = {}, {}, {}
        for g in last15:
            for pid, (nm, pts) in g.items():
                tot[pid] = tot.get(pid, 0) + pts
                cnt[pid] = cnt.get(pid, 0) + 1
                names[pid] = nm
        for pid, c in cnt.items():
            ppg = tot[pid] / c
            if c >= 8 and ppg >= 22 and norm(names[pid]) not in listed:
                add.append(f"{ab} {names[pid]}(平均{ppg:.1f}点・{c}試合)")
        if len(games) >= 8:
            recent = {norm(nm) for g in games[-8:] for nm, _ in g.values()}
            for p in star_list(ab):
                if norm(p["name"]) not in recent:
                    drop.append(f"{ab} {p['name']}(直近8試合出場なし)")
    return add, drop


def aura_suggestions(today, records=()):
    """華の見直し候補(月1回)。直近30日、選手の切り抜き動画が1試合あたりどれだけ見られたか(チームの試合数で割る)を
    リーグ全体で並べる。みんなが見たがっている選手=華。上位 aura_top 人で華がない選手を「付ける候補」、
    華があるのに上位 aura_keep 人に入っていない選手を「外す候補」にする。決めるのは持ち主"""
    hist = load_json(CLIP_HIST, {})
    since = (today - dt.timedelta(days=30)).isoformat()
    games, views = {}, {}
    for h in hist.values():
        if h.get("d", "") < since or "p" not in h:
            continue
        for ab in (h["h"], h["a"]):
            games[ab] = games.get(ab, 0) + 1
        for n, (ab, v) in h["p"].items():
            views[(n, ab)] = views.get((n, ab), 0) + v
    if not views:
        return [], []
    # 直近30日に3試合以上出た選手だけ(長期離脱中でケガのニュース動画だけ見られている選手を除く)
    played = {}
    for r in records:
        if r["date"] >= since:
            for lu in (r.get("lineup") or {}).values():
                for nm, _ in (lu.get("pts") or {}).values():
                    played[norm(nm)] = played.get(norm(nm), 0) + 1
    ok = (lambda n: played.get(norm(n), 0) >= 3) if records else (lambda n: True)
    avg = sorted(((v / max(1, games.get(ab, 1)), n, ab) for (n, ab), v in views.items()
                  if games.get(ab, 0) >= 5 and ok(n)), reverse=True)
    rank = {n: i + 1 for i, (_, n, _) in enumerate(avg)}
    aura = {norm(p["name"]): (ab, p["name"]) for ab, lst in STARS.items() if not ab.startswith("_") for p in lst if p.get("aura")}
    A = CFG.get("aura", {"top": 12, "keep": 40})
    # MVP級はもう一番上の格なので、華を付けても変わらない → 候補から外す
    add = [f"{ab} {n}(1試合あたり{a / 1e4:.0f}万再生・{rank[n]}位)" for a, n, ab in avg[:A["top"]]
           if norm(n) not in aura and tier_of(ab, n) != "mvp"]
    drop = [f"{ab} {n}({rank.get(n, '圏外')}位)" for k, (ab, n) in aura.items()
            if rank.get(n, 10**6) > A["keep"] and games.get(ab, 0) >= 5]
    return add, drop


def send_alerts(today, records):
    """持ち主にだけ知らせること(見張り・エース表の見直し)を alert.txt に書く。ワークフローが NTFY_TOPIC に送る"""
    state = load_json(ALERT_STATE, {})
    parts = []
    h = health_alerts()
    if h and state.get("health") != today.isoformat():
        parts.append("【余韻の測定がおかしいかも】\n" + "\n".join(h))
        state["health"] = today.isoformat()
    month = today.strftime("%Y-%m")
    if (today.day == 1 or "--suggest-stars" in sys.argv) and state.get("stars") != month:
        add, drop = star_suggestions(records, today)
        if add or drop:
            parts.append("【エース表の見直し候補】\n" + "\n".join(["入れる候補:"] + (add or ["なし"]) + ["外す候補:"] + (drop or ["なし"])))
        gaps = rating_gaps(records)
        if gaps:
            parts.append("【みんなの評価とランクのずれ(先月まで)】\n" + "\n".join(gaps[-20:]))
        add, drop = aura_suggestions(today, records)
        if add or drop:
            parts.append("【華の見直し候補(切り抜き動画の見られ方・直近30日)】\n" + "\n".join(["華を付ける候補:"] + (add or ["なし"]) + ["華を外す候補:"] + (drop or ["なし"])))
        state["stars"] = month
    if parts:
        open(os.path.join(HERE, "alert.txt"), "w").write("\n\n".join(parts))
    json.dump(state, open(ALERT_STATE, "w"), ensure_ascii=False)


EAST = {"ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DET", "IND", "MIA", "MIL", "NY", "ORL", "PHI", "TOR", "WSH"}


WL_BEFORE = {}   # 日付 → その日より前の勝敗 {チーム: [勝, 敗]}


def conf_ranks(wl):
    """カンファレンスごとの順位(勝率順。同じなら勝ち数、チーム名)"""
    out = {}
    for conf in (True, False):
        ts = [t for t in wl if (t in EAST) == conf]
        ts.sort(key=lambda t: (-(wl[t][0] / max(1, sum(wl[t]))), -wl[t][0], t))
        for i, t in enumerate(ts, 1):
            out[t] = i
    return out


def race_label(teams, date):
    """順位争いの直接対決か。ネタバレを避けるため、前の日の試合は入れない(2日前までの順位で見る)。
    同じカンファレンスで、両方が5〜8位(6位=プレーオフ直行の境目)か9〜12位(10位=プレーインの境目)、差がrace_gb試合以内"""
    if date.month >= 7 or date.strftime("%m-%d") < R.get("race_from", "02-01"):
        return None
    keys = sorted(k for k in WL_BEFORE if k <= (date - dt.timedelta(days=1)).isoformat())
    if not keys:
        return None
    wl = WL_BEFORE[keys[-1]]
    a, b = (t["abbr"] for t in teams)
    if a not in wl or b not in wl or (a in EAST) != (b in EAST):
        return None
    rk = conf_ranks(wl)
    gb = abs((wl[a][0] - wl[a][1]) - (wl[b][0] - wl[b][1])) / 2
    if gb > R.get("race_gb", 3):
        return None
    if all(5 <= rk[t] <= 8 for t in (a, b)):
        return "プレーオフ直行(6位)争いの直接対決"
    if all(9 <= rk[t] <= 12 for t in (a, b)):
        return "プレーイン(10位)争いの直接対決"
    return None


def context_items(comp, teams, date):
    """試合の重み(NBAカップの勝ち抜け戦・順位争い)。試合前にわかることだけ"""
    out = []
    stg = cup_stage(comp)
    if stg == "final":
        out.append({"txt": "NBAカップ決勝", "pts": P.get("cup_final", 10)})
    elif stg in ("qf", "sf"):
        out.append({"txt": f"NBAカップ{CUP_JA[stg]}(負けたら終わり)", "pts": P.get("cup_knockout", 5)})
    rl = race_label(teams, date)
    if rl:
        out.append({"txt": rl, "pts": P.get("race", 5)})
    return out


def standings_snapshots(today):
    """エース表の並び用の順位(レギュラーシーズンの勝敗)。今日の試合は入れない(順位の動きで勝敗がわかるので)。
    昨日まで(asof)と、おとといまで(prev)の2つを返す。推しの試合を隠している間はアプリが prev を使う"""
    start = min(season_dates(today.strftime("%Y%m%d")))
    wl, snaps = {}, {}
    d = start
    while d < today:
        WL_BEFORE[d.isoformat()] = {k: list(v) for k, v in wl.items()}   # その日の試合より前の勝敗(順位争いの判定用)
        try:
            evs = events_for_jst_date(d)
        except Exception:
            evs = []
        for e, _ in evs:
            comp = e["competitions"][0]
            if SEASON_TYPE.get(e.get("season", {}).get("type")) != "regular" or not comp["status"]["type"].get("completed"):
                continue
            if cup_stage(comp) == "final":   # カップ決勝は順位に数えない
                continue
            for c in comp["competitors"]:
                ab = c["team"]["abbreviation"]
                r = wl.setdefault(ab, [0, 0])
                r[0 if c.get("winner") else 1] += 1
        if d == today - dt.timedelta(days=2):
            snaps["prev"] = {"asof": d.isoformat(), "wl": {k: list(v) for k, v in wl.items()}}
        d += dt.timedelta(days=1)
    snaps["asof"] = (today - dt.timedelta(days=1)).isoformat()
    snaps["wl"] = {k: list(v) for k, v in wl.items()}
    snaps["east"] = sorted(EAST)
    return snaps


NAMES_JA = load_json(os.path.join(HERE, "names_ja.json"), {"players": {}})
STAR_WHY = {"50点以上": "50点超え", "40点以上": "40点超え", "30点・高効率": "高効率の30点超え", "25点・超高効率": "超高効率の25点超え",
            "守備": "守備で大暴れ", "トリプルダブル": "トリプルダブル", "キャリアハイ": "キャリアハイ"}


def short_name(name):
    """表示用の名前。カタカナに直さず、英語のフルネームのまま(例:Trae Young)"""
    return name


def why_line(out):
    """なぜこのランクなのかの一言(内訳の点が大きい順に2つまで)。結果を開いた人・タップした人にだけ見せる"""
    cand = []
    fl = {x["txt"]: x["pts"] for x in out["flow"]["items"]}
    close = [k for k in ("クラッチ(終盤の接戦)", "最後の攻撃までもつれた") if k in fl]
    if close:
        cand.append((sum(fl.pop(k) for k in close), "最後までもつれた接戦" if len(close) == 2 else ("終盤の接戦" if "クラッチ(終盤の接戦)" in close else "最後の攻撃までもつれた")))
    for k, v in fl.items():
        cand.append((v, {"終盤の大逆転": "終盤の大逆転", "20点差以上の逆転": "20点差からの逆転", "ブザービーター": "ブザービーター",
                         "延長": "延長戦", "終盤の猛追": "終盤の猛追", "試合を決める守備": "試合を決める守備"}.get(k, k)))
    stars = sorted([x for x in out["stars"]["items"] if x.get("k") == "star"], key=lambda x: -x["pts"])
    for x in stars[:2]:
        w = "キャリアハイ" if x.get("career_high") is not None and "キャリアハイ" not in x["why"] and x["why"][0] not in ("50点以上", "40点以上") else x["why"][0]
        tag = "(覚醒中)" if (x.get("streak") or 0) >= 2 else ""
        cand.append((x["pts"], f"{short_name(x['who'])}{tag}の{STAR_WHY.get(w, w)}", x.get("raw", 0)))
    if any(x.get("k") == "moment" for x in out["stars"]["items"]):
        cand.append((P["star_moment"], "スターが試合を決めた"))
    if out["record"]["pts"]:
        cand.append((out["record"]["pts"] + 50, "歴史的な記録"))
    if out["card"]["pts"] >= P["card_cap"]:
        cand.append((out["card"]["pts"] * 0.6, "豪華カード"))
    if out["buzz"]["pts"] >= 8:
        cand.append((out["buzz"]["pts"], "試合後も大きな話題"))
    if out["moment"]["pts"]:
        cand.append((out["moment"]["pts"], "名場面がバズった"))
    cand = [c for c in cand if c[0] > 0]
    cand.sort(key=lambda c: -c[0])
    if not cand:
        neg = []
        if out["absence"]["pts"] <= -6 or any(x["pts"] < 0 for x in out["card"]["items"]):
            neg.append("主力の欠場")
        return "見せ場が少なかった" + (f"({'・'.join(neg)}も響いた)" if neg else "")
    top = cand[:2]
    if all(len(c) > 2 for c in top):   # 選手が2人並ぶときは、実際に点を多く取った順
        top.sort(key=lambda c: -c[2])
    line = "+".join(dict.fromkeys(c[1] for c in top))
    if out["absence"]["pts"] <= -6:
        line += "(主力欠場で減点)"
    return line


RATINGS_DATA = load_json(os.path.join(HERE, "ratings.json"), {})


def rating_gaps(records):
    """みんなの評価とランクがずれている試合(3票以上)。基準を直す材料として持ち主に知らせる"""
    out = []
    for r in records:
        v = list((RATINGS_DATA.get(str(r["id"])) or {}).values())
        if len(v) < 3:
            continue
        good, bad = v.count("good") / len(v), v.count("bad") / len(v)
        t = r.get("tier") or "-"
        if t in ("SS", "S", "A") and bad >= 0.5:
            out.append(f"{r['date']} {r['away']}@{r['home']}:{t}なのにイマイチが{v.count('bad')}/{len(v)}")
        elif t in ("C", "-") and good >= 0.5:
            out.append(f"{r['date']} {r['away']}@{r['home']}:{'ランク外' if t == '-' else t}なのに面白かったが{v.count('good')}/{len(v)}")
    return out


LATE = {"クラッチ(終盤の接戦)", "最後の攻撃までもつれた", "終盤の猛追", "終盤の大逆転", "ブザービーター", "試合を決める守備"}


def watch_guide(out, tier):
    """どこから見ればいいか(タイパ用)。終盤の流れがわかるのでネタバレ。結果を開いた人・理由ボタンを押した人にだけ見せる"""
    fl = {x["txt"] for x in out["flow"]["items"]}
    late = bool(fl & LATE) or "延長" in fl
    whole = "20点差以上の逆転" in fl
    star = out["stars"]["pts"] >= 25
    top = tier in ("SS", "S", "A")
    if top and whole:
        return "本編を最初から(約2時間15分)。大逆転の流れごと見てほしい"
    if top and late:
        return "本編を最初から(約2時間15分)。時間がなければ第4Qから(約40分)"
    if top:
        return "本編を最初から(約2時間15分)"
    if late and not star:
        return "第4Qから(約40分)で十分"
    if late:
        return "後半から(約1時間10分)"
    if star:
        return "フルハイライト(約10分)で十分。スターの見せ場はそこに入ってる"
    return "フルハイライト(約10分)で十分" if tier in ("B", "C") else "見なくてもいいかも。気になるならハイライトで"


def write_ics(games_days, out_dir):
    """まだ始まっていない試合のカレンダー用ファイル(docs/cal/<試合id>.ics)。
    ファイルとして置くと、ホーム画面のアプリからでも、どのカレンダーアプリでも開ける"""
    d = os.path.join(out_dir, "cal")
    os.makedirs(d, exist_ok=True)
    stamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    for day in games_days:
        for g in day["games"]:
            if g.get("state") != "pre" or not g.get("start_utc"):
                continue
            st = dt.datetime.fromisoformat(g["start_utc"].replace("Z", "+00:00"))
            en = st + dt.timedelta(minutes=150)
            f = lambda x: x.strftime("%Y%m%dT%H%M%SZ")
            h = next(t for t in g["teams"] if t["homeAway"] == "home")
            a = next(t for t in g["teams"] if t["homeAway"] == "away")
            lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//nba-navi//JP", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
                     "BEGIN:VEVENT", f"UID:{g['id']}@nba-navi", f"DTSTAMP:{stamp}", f"DTSTART:{f(st)}", f"DTEND:{f(en)}",
                     f"SUMMARY:NBA {a['abbr']} @ {h['abbr']}", f"LOCATION:{g.get('venue', '')}",
                     "DESCRIPTION:NBA 観戦ナビ https://tak0913.github.io/nba-navi/",
                     "BEGIN:VALARM", "TRIGGER:-PT15M", "ACTION:DISPLAY", "DESCRIPTION:まもなく試合開始", "END:VALARM",
                     "END:VEVENT", "END:VCALENDAR"]
            open(os.path.join(d, f"{g['id']}.ics"), "w", encoding="utf-8", newline="").write("\r\n".join(lines) + "\r\n")


def write_breakdowns(records, out_dir):
    """結果を開いたときだけ読み込む「計算の内訳」(docs/b/<試合id>.json)。普段のデータには入れない"""
    d = os.path.join(out_dir, "b")
    os.makedirs(d, exist_ok=True)
    W = CFG["buzz"]
    for r in records:
        if not r.get("bd"):
            continue
        b = latest_buzz(r)
        buzz_pts = 0 if b is None else round((W["weight_up"] if b >= 0.5 else W["weight_down"]) * (b - 0.5) * 2, 1)
        out = dict(r["bd"])
        out["absence"] = {"pts": r.get("absence", 0), "who": r.get("absent", {})}
        out["buzz"] = {"pts": buzz_pts, "d": r.get("buzz", {})}
        out["moment"] = {"pts": moment_bonus(r), "ratio": (r["moment"][1] if _moment_ok(r.get("moment")) else None)}
        out["total"] = fs(r)
        out["why"] = why_line(out)
        out["guide"] = watch_guide(out, r.get("tier") or "-")
        v = list((RATINGS_DATA.get(str(r["id"])) or {}).values())
        out["votes"] = {k: v.count(k) for k in ("good", "ok", "bad")}
        json.dump(out, open(os.path.join(d, f"{r['id']}.json"), "w"), ensure_ascii=False, separators=(",", ":"))


def main():
    global NOW
    now = dt.datetime.now(JST)
    if "--now" in sys.argv:
        now = dt.datetime.fromisoformat(sys.argv[sys.argv.index("--now") + 1]).astimezone(JST)
    NOW = now
    today = now.date()
    season, strong = load_league()
    archive = {r["id"]: r for r in load_json(ARCHIVE, [])}
    try:   # 順位(エース表の並び+順位争いの判定)。採点より先に作る
        STAND = standings_snapshots(today)
    except Exception:
        STAND = {}

    if "--backfill" in sys.argv:
        i = sys.argv.index("--backfill")
        a, b = dt.date.fromisoformat(sys.argv[i + 1]), dt.date.fromisoformat(sys.argv[i + 2])
        dates = [a + dt.timedelta(days=k) for k in range((b - a).days + 1)]
        backfill(dates, season, strong, archive)
        json.dump(sorted(archive.values(), key=lambda r: r["date"]), open(ARCHIVE, "w"), ensure_ascii=False)
        print(f"OK: archive has {len(archive)} games")
        return

    if "--backfill-prev" in sys.argv:
        prev = {}
        dates = [d for d in season_dates("20260101") if d <= dt.date(2026, 7, 1)]
        backfill(dates, season, strong, prev, label="昨季・参考")
        apply_surprise(list(prev.values()))
        apply_absence(list(prev.values()))
        json.dump(sorted(prev.values(), key=lambda r: r["date"]), open(ARCHIVE_PREV, "w"), ensure_ascii=False)
        ps = {}
        for r in prev.values():
            ps.setdefault(r["type"], []).append(int(round(r["content"])))
        json.dump({k: sorted(v) for k, v in ps.items()}, open(PREV_SCORES, "w"), separators=(",", ":"))
        print(f"OK: prev-season reference has {len(prev)} games")
        return

    # 記録は毎回作り直す(試合の中身の点数は何度計算しても同じになる)。余韻だけは保存してあるものを使う
    if "--rebuild" in sys.argv:
        start = min(d for d in season_dates(today.strftime("%Y%m%d")))
        dates = [start + dt.timedelta(days=k) for k in range((today - start).days)]
        old = archive
        archive = {}
        backfill(dates, season, strong, archive, fallback=old)
    bstate = {} if "--remeasure" in sys.argv else load_json(BUZZ_STATE, {})
    for r in archive.values():
        if r["id"] in bstate:
            v = bstate[r["id"]]
            r["buzz"] = {"d1": v[0], "d7": v[1], "d30": v[2]}
            if len(v) > 3 and _moment_ok(v[3]):   # 古い版の名場面は捨てて測り直す
                r["moment"] = v[3]

    # 通常の更新:直近3日を画面用に取り直し、終わった試合を記録に追加
    days, roster_cache = [], {}
    for back in (0, 1, 2):
        d = today - dt.timedelta(days=back)
        evs = [x for x in events_for_jst_date(d)
               if game_type(x[0]) != "pre"]
        with ThreadPoolExecutor(6) as ex:
            res = [x for x in ex.map(lambda es: build_game_safe(es[0], es[1], season, strong, roster_cache), evs) if x[0]]
        for g, rec in res:
            if rec:
                old = archive.get(rec["id"], {})
                rec["buzz"] = old.get("buzz", rec["buzz"])
                if "buzz_raw" in old:
                    rec["buzz_raw"] = old["buzz_raw"]
                if "moment" in old:
                    rec["moment"] = old["moment"]
                archive[rec["id"]] = rec
        days.append({"date": d.isoformat(), "games": [g for g, _ in res]})

    # 明日・あさっての予定(まだ始まっていないので、ロスター・欠場情報・見どころだけ)
    future = []
    for ahead in (1, 2):
        d = today + dt.timedelta(days=ahead)
        try:
            evs = [x for x in events_for_jst_date(d)
                   if game_type(x[0]) != "pre"]
            with ThreadPoolExecutor(6) as ex:
                res = [x for x in ex.map(lambda es: build_game_safe(es[0], es[1], season, strong, roster_cache), evs) if x[0]]
        except Exception:
            res = []
        future.append({"date": d.isoformat(), "games": [g for g, _ in res]})

    # 小話用に、エース表の選手と日本人選手の過去の成績を先に集める
    star_set = {norm(p["name"]) for ab, lst in STARS.items() if not ab.startswith("_") for p in lst}
    first = [a["id"] for ath in roster_cache.values() for a in ath
             if norm(a.get("displayName", "")) in star_set or (a.get("birthPlace") or {}).get("country") == "Japan"]
    if "--no-buzz" not in sys.argv:
        load_careers(list(archive.values()), CFG.get("career_fetch_per_run", 25), first)
    else:
        load_careers(list(archive.values()), 0, first)   # 保存してある分だけ使う
    apply_surprise(list(archive.values()))
    apply_absence(list(archive.values()))
    add_trivia(days + future, list(archive.values()), roster_cache)
    prev_records = load_json(PREV_SCORES, None) or load_json(ARCHIVE_PREV, [])
    # 余韻の測定(お気に入りは対象外)
    allrec = list(archive.values())
    if "--no-buzz" not in sys.argv:
        measure_buzz([r for r in allrec if not is_hidden(r)], today, CFG["buzz"]["max_games_per_run"])
        measure_moments([r for r in allrec if not is_hidden(r)], today)
    moment_scores(allrec)   # 測らない回(--no-buzz)も、今ある再生数で付け直す
    send_alerts(today, allrec)
    # 隠している間のお気に入りの試合は、比べる相手にも入れない(順位の抜けからバレないように)
    for r in allrec:
        for k in ("tier", "ss_confirmed", "season_rank", "month_rank"):
            r.pop(k, None)
    assign_tiers([r for r in allrec if not is_hidden(r)], prev_records, today)
    records = allrec
    records.sort(key=lambda r: r["date"])
    arch = {r["id"]: r for r in records}
    json.dump(records, open(ARCHIVE, "w"), ensure_ascii=False)
    bs = {r["id"]: [r["buzz"].get(k) for k in ("d1", "d7", "d30")] + ([r["moment"]] if r.get("moment") else [])
          for r in records
          if any(v is not None for v in (r.get("buzz") or {}).values()) or r.get("moment")}
    json.dump(bs, open(BUZZ_STATE, "w"), separators=(",", ":"))

    # 画面用:全試合の「最終点」だけ渡す。推しを除いたランク付けは各自のスマホで計算する
    #(中身の内訳やスコアは渡さない)
    # 展開の点・終わった時刻は渡さない(データを覗くと「接戦だった」「延長があった」がわかってしまうので)
    pub = [{k: r.get(k) for k in ("id", "date", "season", "type", "home", "away", "start_utc", "legend", "cup")}
           | {"score": fs(r), "hall_ok": hall_ok(r)} for r in records]
    tinfo = team_info()
    try:   # 画面(index.html)の版。古い画面のままのスマホに読み込み直してもらうため
        app_ver = re.search(r'APP_VER="([^"]+)"', open(os.path.join(HERE, "..", "docs", "index.html"), encoding="utf-8").read()).group(1)
    except Exception:
        app_ver = None
    data = {"generated_at": now.isoformat(timespec="minutes"), "today": today.isoformat(), "app_ver": app_ver,
            "owner_favorites": CFG["favorites"], "fav_hide_hours": CFG.get("favorite_hide_hours", 24),
            "fav_hide_pad_hours": CFG.get("favorite_hide_pad_hours", 3),
            "standings": STAND,
            "tier_names": CFG["tier_names"], "tier_desc": CFG["tier_desc"], "tiers": T, "rules": R, "points": P, "moment": CFG["moment"],
            "prev": prev_records if isinstance(prev_records, dict) else {},
            "teams": {k: v["full"] for k, v in sorted(tinfo.items())},
            "team_colors": {k: v["color"] for k, v in tinfo.items()},
            "topic_prefix": CFG["public_topic_prefix"],
            "stars": {k: v for k, v in STARS.items() if not k.startswith("_")},
            "jerseys": star_jerseys(tinfo, roster_cache),
            "coach_ties": coach_ties_all(roster_cache),
            "coaches": coaches_by_abbr(),
            "days": days, "future": future, "archive": pub, "upgrades": load_json(UPGRADES, {}), "vote": CFG.get("vote", {}),
            "suggest_topic": CFG.get("suggest_topic"), "rivalries": load_json(os.path.join(HERE, "rivalries.json"), {}),
            "suggested": suggested_pairs()}
    out_dir = os.environ.get("OUT_DIR", HERE)
    json.dump(data, open(os.path.join(out_dir, "data.json"), "w"), ensure_ascii=False)
    write_breakdowns(records, out_dir)
    write_ics(days + future, out_dir)

    # 通知:持ち主用(config の favorites)
    # 試合前の通知(定期の回だけ送る。送るかどうかは workflow が prenotify.txt の中身で決める)
    ch_keys = ["none"] + [k.lower() for k in sorted(tinfo)]
    pre = prenotify_build(now, days[0]["games"] + (future[0]["games"] if future else []), ch_keys) if "--prenotify" in sys.argv else {}
    json.dump(pre, open(os.path.join(HERE, "prenotify.json"), "w"), ensure_ascii=False)
    # 定期通知:送る時間なら notify_now を置く(workflow はこのファイルがあるときだけ送る)
    due = notify_due(now) if "--notify-auto" in sys.argv else None
    slot = due[0] if due else None
    flag = os.path.join(HERE, "notify_now")
    if due or "--notify-now" in sys.argv:
        open(flag, "w").write(due[1] if due else "manual")
        if due:
            mark_notified(due[1])
    elif os.path.exists(flag):
        os.remove(flag)
    open(os.path.join(HERE, "notify.txt"), "w").write(notify_text(now, days[0]["games"], arch, tomorrow=future[0]["games"] if future else None, slot=slot))
    # Bluesky の毎日の速報(持ち主の推しは24時間隠したまま)。投稿は bsky_daily.py
    json.dump(bsky_daily(today, days[0]["games"], arch), open(os.path.join(HERE, "bsky_daily.json"), "w"), ensure_ascii=False)
    # 通知:みんな用。推しチームごとのチャンネル(推しなし + 30チーム)
    channels, fav_list, jobs = {}, [None] + sorted(tinfo), []
    for fav in fav_list:
        favs = [fav] if fav else []
        jobs.append([dict(r) for r in records if not is_hidden(r, favs)])
    assign_tiers_many(jobs, prev_records, today)   # 31チャンネル分を1回で計算
    for fav, recs in zip(fav_list, jobs):
        favs = [fav] if fav else []
        channels[(fav or "none").lower()] = notify_text(now, days[0]["games"], {r["id"]: r for r in recs}, favs,
                                                         tomorrow=future[0]["games"] if future else None, slot=slot)
    json.dump(channels, open(os.path.join(HERE, "notify_channels.json"), "w"), ensure_ascii=False)
    print(f"OK: {sum(len(d['games']) for d in days)} recent games, archive {len(records)} games")
    if any(STALE.values()):
        print("取れずに前回の分で埋めた: " + ", ".join(f"{k}={v}" for k, v in STALE.items()))
    print("buzz signals collected (counts only): " + ", ".join(f"{k}={v}" for k, v in STATS.items()))


if __name__ == "__main__":
    main()
