# Asana Skills

CodexからAsanaのタスク・サブタスクを管理するAgent Skillです。日本語の依頼から対象を確認し、担当者・期間・進捗を更新します。操作内容はローカルのJSONLログに記録します。

Asana APIにPAT（個人アクセストークン）で接続します。Python標準ライブラリだけで動作し、MCPサーバーや外部Pythonパッケージは不要です。

## 対応機能

| 機能 | 内容 |
|---|---|
| 参照 | ワークスペース、ユーザー、プロジェクト、セクション、タスク |
| タスク管理 | 作成、更新、完了・再開、担当者と開始日・期限の設定・解除 |
| 担当者照合 | メールからユーザーGIDを取得し、対象ワークスペースへの所属を確認 |
| サブタスク | 作成、一覧取得、更新、担当者・期間の設定 |
| コメント・移動 | コメントと履歴の参照、コメント投稿、同一プロジェクト内のセクション移動 |
| 検索 | プロジェクト内の取得結果をCodexで絞り込み。有料APIによる全体検索にも対応 |
| 操作ログ | 対象名・GID、変更前後、指定値、理由、成否、APIメタ情報を記録・表示 |

CSV一括取り込み、削除、プロジェクト作成、カスタムフィールド編集、添付ファイル、定期同期、ドライランは未対応です。

## 導入

Codex、Python 3、AsanaのPATを用意してください。Skillを利用するプロジェクトの `.agents/skills/asana/` に、同梱の `skills/asana/` をコピーします。既存フォルダがある場合は、内容を確認してから更新してください。

Linux / macOS：

```sh
mkdir -p /path/to/project/.agents/skills
cp -R skills/asana /path/to/project/.agents/skills/
```

Windows PowerShell：

```powershell
New-Item -ItemType Directory -Force C:\path\to\project\.agents\skills
Copy-Item -LiteralPath skills\asana -Destination C:\path\to\project\.agents\skills -Recurse
```

以下のCLI例はリポジトリのルートから実行します。コピー先から使う場合はパスを読み替えてください。Windowsでは `python3` を `py -3` に読み替えます。

## 認証

`ASANA_ACCESS_TOKEN` を実行プロセスへ環境変数として渡します。トークンを会話に貼り付けたり、リポジトリに保存したりする必要はありません。

1Passwordの環境ファイルに参照を設定済みなら、次のように接続を確認できます。

```sh
op run --env-file=/path/to/asana.env -- python3 skills/asana/scripts/asana.py me --reason "接続確認"
```

Codex自体へ環境変数を渡す場合：

```sh
op run --env-file=/path/to/asana.env -- codex
```

起動済みのCodexでも、環境ファイルのパスを伝えれば、ヘルパーを `op run` 経由で実行できます。1Password CLIはこの認証方法を使う場合だけ必要です。

## Codexへの依頼例

```text
$asana taro@example.comにアサインされているタスクを見せて
```

```text
$asana 「モデル実装」をtaro@example.comにアサインして。
開始日は2026/11/14、期限は2026/11/18にして
```

```text
$asana 「モデル実装」に「実装」と「評価」のサブタスクを追加して。
両方とも私にアサインし、期間は2026/11/14〜16と2026/11/17〜18にして
```

```text
$asana 直近のログを読んで、操作対象と変更内容を説明して
```

対象名が重複するときは候補を確認します。担当者をメールで指定した場合は、対象プロジェクトのワークスペースに所属するユーザーか確認します。チームや非公開プロジェクト固有のアクセス権まで保証するものではありません。

## CLI

メールから担当者を照合する例：

```sh
python3 skills/asana/scripts/asana.py resolve-user \
  --email taro@example.com --workspace WORKSPACE_GID \
  --reason "タスク作成前の担当者照合"
```

成功時の `data.gid` を、タスクの `assignee` に指定します。

```sh
python3 skills/asana/scripts/asana.py update --gid TASK_GID \
  --data '{"assignee":"USER_GID","start_on":"2026-11-14","due_on":"2026-11-18"}' \
  --reason "利用者の依頼により担当者と作業期間を設定"
```

`--reason` は操作理由です。Codexには依頼に基づく短い理由を渡すよう指示しています。CLIで省略した場合は「理由未記録」となります。

複数行の説明やJSON入力は `--data-file`、コメントは `--text-file` で渡せます。PowerShellでJSON文字列の引用が難しい場合もファイル入力を利用できます。コマンド一覧と詳細は[操作ガイド](skills/asana/references/operations.md)を参照してください。

## 実行ログ

保存先は作業ディレクトリに依存しません。

| OS | 保存先 |
|---|---|
| Linux / macOS | `~/.agents/logs/asana.jsonl` |
| Windows | 通常 `%USERPROFILE%\.agents\logs\asana.jsonl` |

操作の終了行に、対象名・GID・URL、指定値、変更前後、理由、成否、所要時間、HTTPステータスをまとめます。取得できたリクエストIDと429の再試行履歴も記録します。API単位の開始・終了記録も残します。

トークンや認証ヘッダーは記録しません。説明・コメントは先頭50文字＋省略記号に制限するため、本文の完全復元には使えません。ログには業務データが含まれます。自動削除・ローテーションは行いません。

### 読みやすく表示する

```sh
python3 skills/asana/scripts/read_logs.py --last 10
python3 skills/asana/scripts/read_logs.py --run-id RUN_ID
python3 skills/asana/scripts/read_logs.py --all
```

対象、理由、結果と、次のような変更表をMarkdownで表示します。

| 設定項目 | 変更前 | 指定した値 | 記録された変更後 |
|---|---|---|---|
| 開始日 | 2026-11-14 | 2026-11-15 | 2026-11-15 |
| 期限 | 2026-11-18 | 2026-11-19 | 2026-11-19 |

`--details` でAPI通信の詳細、`--path` で別のログファイルを指定できます。表示スクリプトは読み取り専用で、PATやAPIアクセスは不要です。旧形式にない情報は推測しません。詳細は[ログの読み方](skills/asana/references/logging.md)を参照してください。

## 制約と失敗時の扱い

- 一覧取得の既定上限は100件です。`has_more` と `next_offset` で続きの有無を確認します。有料の全体検索は最大100件で、通常のoffsetページングには非対応です。
- 開始ログが保存できない場合、APIを呼びません。書き込み後のログ保存失敗は `audit_log_finish_failed` として返します。
- 書き込み後の確認取得に失敗した場合は `written_but_unverified` を返します。同じGIDを再取得して確認し、書き込みを再送しないでください。
- 通信断や強制終了では結果が不明な場合があります。ログがあるだけで成功とは判断せず、対象の状態を確認します。

## 検証

```sh
python3 -m unittest discover -s tests -v
```

モックテストで入力検証、ページング、担当者照合、ログ・差分・秘匿処理、失敗時の動作を確認します。実接続ではタスク・サブタスク作成、担当者・期間の設定と解除、コメント、移動、完了・再開、メール照合とログ表示を確認済みです。

実接続の確認はLinuxで実施しました。Windowsのロック分岐はモックで確認していますが、Windows・macOSの実機検証は未実施です。

Skillの入口は [SKILL.md](skills/asana/SKILL.md)です。
