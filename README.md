# unattended-job-audit

定期実行のジョブ(launchd・cron・GitHub Actions)を棚卸しして、**黙って壊れる作りになっていないか**を点検する Claude Code のスキルです。

自動で動く仕組みは、動かなくなったことに気づけるかどうかで価値が決まります。
このスキルは、いま登録されているジョブを一覧にして、失敗が人に届くか・二度打ちしないか・
出力が古くなったら気づけるかを、機械で当たれる範囲まで当てます。

```
$ python3 scripts/scan_jobs.py --repo ~/src/myapp

ジョブ                              いつ                  点検
---------------------------------------------------------------------
com.example.backup               日曜 20:00           1 失敗が届かない / 3 二度打ちの防ぎがない
watch.yml                        17 1 1,15 * * (UTC)  2 通知が失敗時だけか要確認
com.example.daily-report         9:40                 ひっかかりなし

28 件中 8 件に見るところがある。番号は reference/checklist.md に対応。
4(出力の鮮度)・5(あるべき一覧との差)・8(人へ渡す経路)・10(端末が寝ていたとき)は目で見る。
```

## 入れ方

Claude Code のスキルとして置きます。

```
git clone https://github.com/houchi-unyo/unattended-job-audit.git \
  ~/.claude/skills/unattended-job-audit
```

プロジェクト単位で使うなら `<リポジトリ>/.claude/skills/` の下に置いてください。
Claude に「無人ジョブの点検をして」と頼むと、このスキルが読み込まれます。

スキャナだけを単体で使うこともできます。**標準ライブラリだけで動き、外部のパッケージは要りません。**

```
python3 scripts/scan_jobs.py            # launchd + cron + カレントの .github/workflows
python3 scripts/scan_jobs.py --json     # 機械で読む形で出す
python3 scripts/scan_jobs.py --all      # アプリが置いた常駐ジョブも含める
```

## 何を見るか

| # | 問い |
|---|---|
| 1 | 失敗したとき、人に届くか |
| 2 | 成功したときは黙っているか |
| 3 | 同じ仕事を二度しないか |
| 4 | 出力が古くなったら気づけるか |
| 5 | 登録されている一覧と、あるべき一覧が一致するか |
| 6 | 秘密が環境変数・権限 600 のファイルにあるか |
| 7 | 失敗したときに止まるか、暴走するか |
| 8 | 自動でできなかった分を、人が拾える形で残すか |
| 9 | ログが残り、いつのものか分かるか |
| 10 | 端末が寝ていたときの前提が書いてあるか |

1〜3・5〜7・9 はスクリプトが当たります。4・8・10 は人が見ます。
それぞれの中身と、実際に起きた壊れ方は [reference/checklist.md](reference/checklist.md) に書きました。

**直す順は 1 → 3 → 4 です。**この 3 つが無い自動化は、動いているように見えて、
止まった日から誰も気づきません。

## 断らないこと

- このスキルは**読むだけ**です。ジョブの登録・解除・書き換えはしません
- 判定は文字列の手がかりによるものです。**当たらないこともあります**(通知を別の仕組みに任せている、など)。
  引っかかった行を見て、人が決めてください
- macOS では `launchctl list`、Linux では `crontab -l` を読みます。どちらも読み取りだけです

## この 10 点の出どころ

個人で運用している無人の仕組み(毎朝の記録・週次の集計・動画の生成と投稿)を 2 か月動かして
実際に踏んだ失敗から書き起こしました。3 週間気づかずに毎晩落ち続けたジョブ、
意図せず登録されて毎晩通知を送り続けたジョブ、投稿の直後に落ちて同じものを二度出したジョブが元になっています。

## ライセンス

MIT

---

## English

A Claude Code skill that audits scheduled jobs (launchd, cron, GitHub Actions) for the
failure modes that make unattended automation break silently: no failure notification,
no idempotency guard, no staleness watch.

Standard library only, read-only. `python3 scripts/scan_jobs.py --help`.
The ten checks, and the real incidents behind each one, are in `reference/checklist.md` (Japanese).
