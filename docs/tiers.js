// ランク付けの計算(アプリと、サーバーの通知づくりの両方がこのファイルを使う。直すのはここだけ)
// records: [{id, date, season, type, score, legend, hall_ok}] に tier などを書き込む
// opts: {T: ランクの基準, R: ルール, prev: 昨季の点数 {type: [点…]}, today: "YYYY-MM-DD", upgrades: 決選投票の格上げ}
(function (root) {
  function weekStart(d) {
    const x = new Date(d + "T00:00:00Z");
    x.setUTCDate(x.getUTCDate() - ((x.getUTCDay() + 6) % 7));
    return x.toISOString().slice(0, 10);
  }
  function daysBetween(a, b) {
    return Math.round((Date.parse(a + "T00:00:00Z") - Date.parse(b + "T00:00:00Z")) / 864e5);
  }
  // 同点のときの順番:id の小さい方を上にする(中身の点数=展開などのヒントを使わない)
  function before(x, r) {
    return x.score > r.score || (x.score === r.score && String(x.id) < String(r.id));
  }

  function assignTiers(records, opts) {
    const T = opts.T, R = opts.R, prev = opts.prev || {}, ups = opts.upgrades || {};
    records.forEach(r => { r.tier = null; r.ss_confirmed = false; r.season_rank = null; r.month_rank = null; r.up_from = null; r.story = null; });
    const pools = {};
    records.forEach(r => { (pools[r.season + "|" + r.type] = pools[r.season + "|" + r.type] || []).push(r); });
    const gt = (arr, s) => arr.reduce((n, v) => n + (v > s ? 1 : 0), 0);
    Object.values(pools).forEach(pool => {
      const typ = pool[0].type, first = pool.map(r => r.date).sort()[0];
      // シーズン序盤は試合が少ないので、昨季の点数も比べる相手に混ぜる
      const helper = daysBetween(opts.today, first) <= R.prev_season_helper_days ? (prev[typ] || []) : [];
      const seasonScores = pool.map(r => r.score).concat(helper);
      const months = {}, weeks = {};
      pool.forEach(r => {
        (months[r.date.slice(0, 7)] = months[r.date.slice(0, 7)] || []).push(r.score);
        const w = weekStart(r.date); (weeks[w] = weeks[w] || []).push(r.score);
      });
      pool.forEach(r => {
        const s = r.score;
        let month = months[r.date.slice(0, 7)];
        if (month.length < R.min_month_pool)
          month = pool.filter(x => { const k = daysBetween(r.date, x.date); return (k >= 0 && k < 30) || x.date.slice(0, 7) === r.date.slice(0, 7); }).map(x => x.score);
        const ws = weekStart(r.date);
        let week = weeks[ws];
        if (week.length < R.min_week_pool)
          week = pool.filter(x => { const k = daysBetween(r.date, x.date); return (k >= 0 && k < 7) || weekStart(x.date) === ws; }).map(x => x.score);
        const frac = a => gt(a, s) / Math.max(1, a.length);
        r.season_rank = 1 + pool.filter(x => before(x, r)).length;
        r.month_rank = 1 + gt(month, s);
        let t = "-";
        if (s >= T.SS.min && 1 + gt(seasonScores, s) <= T.SS.season_top_n) t = "SS";
        else if (s >= T.S.min && frac(seasonScores) < T.S.season_top_frac) t = "S";
        else if (s >= T.A.min && frac(month) < T.A.month_top_frac) t = "A";
        else if (s >= T.B.min && frac(week) < T.B.week_top_frac) t = "B";
        else if (s >= T.C.min) t = "C";
        if (r.legend) t = "SS";   // 70点以上などの歴史的記録はSS候補(殿堂入りは余韻で決める)
        r.tier = t;
        r.ss_confirmed = t === "SS" && !!r.hall_ok;
      });
    });
    // 月末の決選投票で格上げされた試合(理由も残す)
    const RK = { "-": 0, C: 1, B: 2, A: 3, S: 4, SS: 5 };
    records.forEach(r => {
      const u = ups[r.id];
      if (!u || !r.tier) return;
      if (RK[u.to] > RK[r.tier]) { r.tier = u.to; r.ss_confirmed = r.tier === "SS" && !!r.hall_ok; }
      r.up_from = u.from; r.story = u.why;
    });
    return records;
  }

  // 並べる順番(点数の高い順、同点は id 順)
  function compare(a, b) { return before(a, b) ? -1 : before(b, a) ? 1 : 0; }

  const api = { assignTiers, compare, weekStart, daysBetween };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Tiers = api;
})(typeof window !== "undefined" ? window : this);
