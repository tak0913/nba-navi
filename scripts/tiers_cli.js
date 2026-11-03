// サーバー(collect.py)からランク付けを呼ぶための入口。計算そのものは docs/tiers.js(アプリと共通)
// 入力(標準入力):{"jobs": [{"records": [...], "opts": {...}}, ...]}  出力:同じ順番で records を返す
const { assignTiers } = require("../docs/tiers.js");
let buf = "";
process.stdin.on("data", d => (buf += d));
process.stdin.on("end", () => {
  const { jobs } = JSON.parse(buf);
  const out = jobs.map(j => assignTiers(j.records, j.opts).map(r => ({
    id: r.id, tier: r.tier, ss_confirmed: r.ss_confirmed, season_rank: r.season_rank,
    month_rank: r.month_rank, up_from: r.up_from, story: r.story,
  })));
  process.stdout.write(JSON.stringify(out));
});
