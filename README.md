# nba-navi(NBA 観戦ナビ)

ネタバレなしで「この試合は見るべきか」を教えてくれる、自分用のNBA観戦アプリ。

- 画面:`docs/`(GitHub Pages で公開。iPhoneは Safari で開いて「ホーム画面に追加」)
- データ収集と採点:`scripts/collect.py`(ESPN)、余韻の測定:`scripts/buzz.py`(Googleニュース・YouTube)
- 設定:`scripts/config.json`(お気に入り・ランク基準・点数)、エース表:`scripts/stars.json`
- 自動更新:`.github/workflows/update.yml`(毎時更新、日本時間 9・12・15・18・21時ごろに ntfy で通知)

ネタバレ防止のため、スクリプトは試合の中身をログに出しません。結果はアプリの確認ボタンを押したときだけ表示されます。
