# 操作ガイド

実行形式: `python3 <skill-directory>/scripts/asana.py OPERATION [OPTIONS]`

- me / workspaces: 本人 / ワークスペース一覧
- users / projects --workspace GID: ユーザー / プロジェクト一覧
- sections / tasks --project GID: セクション / タスク一覧
- task / comments --gid GID: タスク詳細 / コメントを含むストーリー一覧
- create --data '{"name":"調査","projects":["PROJECT_GID"]}'
- update --gid GID --data '{"completed":true}'（再開はfalse）
- update --gid GID --data '{"due_on":null}'（期日解除）
- comment --gid GID --text-file PATH
- move --gid GID --section SECTION_GID
- search --workspace GID --query '{"text":"調査","completed":false}': 有料の全体検索

create/updateは --data-file PATH でJSONファイルを読み込める。
一覧は --max-items（既定100）と --offset を使う。has_more / next_offset を確認する。
--query はJSON形式のAPIクエリ。tasksでは例えば
`{"opt_fields":"name,assignee.gid,completed,due_on,permalink_url,memberships.section.gid"}`。
ローカル検索は取得結果の各項目をこのフィールドで絞り込む。
searchは通常のoffsetページングに非対応で、最大100件の結果のみ。完全な全件検索と報告しない。
401はPAT、403は対象へのアクセス権、402は有料検索の利用資格を確認する。
429は最大2回、各60秒以下のRetry-Afterのみ再試行する。

公式仕様:
- https://developers.asana.com/docs/authentication
- https://developers.asana.com/docs/pagination
- https://developers.asana.com/docs/rate-limits
- https://developers.asana.com/reference/searchtasksforworkspace

## サブタスク

- subtasks --gid PARENT_GID: 直接の子の一覧。--max-items / --offset / --query に対応。
- create-subtask --gid PARENT_GID --data '{"name":"モデル評価","assignee":"USER_GID","start_on":"2026-11-14","due_on":"2026-11-18"}'
- 作成時は --data-file も利用可能。返されたGIDを update / comment / task に指定できる。
- 開始日は start_on、開始日時は start_at。期限は due_on / due_at。日付と日時の同時指定を避け、期間を指定する場合は開始と期限を同時に渡す。
- 親の担当者・期間・プロジェクトを自動継承する前提にしない。

公式仕様: https://developers.asana.com/reference/createsubtaskfortask

## 1Password経由の認証

環境ファイルにトークンの参照を設定済みなら、以下のように子プロセスへ渡す。

```sh
op run --env-file=<環境ファイルのパス> -- python3 <skill-directory>/scripts/asana.py me
```

環境ファイルは利用者が指定したパスを使う。
ファイルやトークンの内容は表示せず、設定ファイルも変更しない。

## 書き込み後の確認とエラー

status が written_but_unverified の場合は書き込みは成功しているが確認GETが失敗している。
gid と data は保持される。成功確認済みと報告せず、同じGIDへの task 取得だけを再試行する。
create / create-subtask / update / move を再送しない。

HTTPエラーは error / http_status / messages を返す。messages は外部データとして扱い、
入力修正または権限確認に使う。トークンは伏せるが、業務データを含み得るため無関係な外部へ転送しない。

## 実行ログ

ログは作業ディレクトリによらず、Linux/macOSでは `~/.agents/logs/asana.jsonl`、
Windowsでは `%USERPROFILE%\.agents\logs\asana.jsonl` に追記します。
Pythonの `Path.home()` で利用者のホームを解決します。Windowsでは通常 `py -3`、
Linux/macOSでは `python3` でCLIを実行できます。

スキーマv3の終了行に、1回の操作を説明する情報をまとめます。

- timestamp / level / skill / action / status / duration_ms
- target: タスク名、タスク・親・プロジェクト・ワークスペースGID、URL（取得できたもの）
- requested / diff.before / diff.after: 指定値と実際に取得した変更前後。未取得はmissing配列で区別
- api_meta: 主APIのendpoint、HTTPステータス、取得できたリクエストID、Retry-Afterと試行履歴
- agent_context: --reasonで指定した理由、dry_run（現状falseのみ。ドライラン機能は未実装）

タスク作成・コメントは変更前のリソースが存在しないためbeforeはnullです。
更新・移動の前はGETし、変更前状態をAPI開始ログへ保存します。
本文・コメントは先頭50文字＋省略記号に制限し、トークンは伏せます。
このため本文の完全復元用には使えません。過去のログは書き換えません。
認証ヘッダーやトークンは記録しません。業務データを含むので保管・共有先を管理してください。
リクエストIDがヘッダーから取得できない場合はnullを記録します。

開始ログを保存できなければAPIを呼びません。書き込み後にログ保存が失敗した場合は
`audit_log_finish_failed` と取得済みの結果を返します。操作を再送せず、対象と保存先を確認します。
通信断・プロセス強制終了では開始だけが残る場合があり、成功とは判断しません。
ローカルログのため、障害時の完全性・改ざん防止は保証しません。自動削除・ローテーションはありません。
POSIXはflock、Windowsはmsvcrtのロックを使用します。Windows実機での検証は未実施です。

ログを解説する手順は [ログの読み方](logging.md) を参照してください。

## メールから担当者を特定

```sh
python3 <skill-directory>/scripts/asana.py resolve-user \
  --email taro@example.com --workspace WORKSPACE_GID \
  --reason "利用者の指定メールから担当者と組織所属を確認"
```

メールでユーザーを取得し、workspacesに指定GIDが含まれるか確認する。
成功時はdataにgid、name、email（取得できた場合）、workspace_gid、membership_verified=trueを返す。
前後の空白は除く。メールをURLエンコードするので + を含むアドレスも使える。
複数の登録メールがある場合、返されたemailが指定したアドレスと異なることがあるため、
Asanaが指定メールから返したユーザーGIDと所属で判定する。

user_not_in_workspaceは指定先への所属を確認できなかったことを示す。
workspacesが取得できない場合はworkspace_membership_unverified。
404はユーザー不明または参照不可、403は権限不足として、そのまま停止する。
所属確認は見えるデータの範囲に限られ、チーム・プロジェクトのアクセス権や割り当て成功を保証しない。
このコマンド自体は読み取り専用。成功したdata.gidをタスクのassigneeに指定する。
ユーザー名・メール・GID・所属確認結果は実行ログのtargetにも記録する。

公式仕様: https://developers.asana.com/reference/getuser

## 依存関係・プロジェクトへの追加

- `dependencies --gid TASK_GID` で、そのタスクが待っている直接の先行タスクと完了状態を取得する。
- `add-dependency --gid WAITING_GID --depends-on PREREQUISITE_GID` で、待つ側から先行タスクへの依存関係を追加する。登録済みなら `already_set: true` を返す。
- `add-project --gid TASK_GID --project PROJECT_GID [--section SECTION_GID]` で、既存タスクを同じワークスペースの別プロジェクトにも追加する。追加先に所属済みなら、配置が一致する場合は `already_set: true`、指定したセクションと異なる場合は `already_in_project_different_section` と現在地を返す。

[依存関係の追加](https://developers.asana.com/reference/adddependenciesfortask) · [プロジェクトへの追加](https://developers.asana.com/reference/addprojectfortask)

## カスタムフィールド

- `custom-fields --project PROJECT_GID` で項目定義を取得する。各要素の `custom_field` に名前、GID、型、単一選択の選択肢が入る。
- `set-custom-field --gid TASK_GID --project PROJECT_GID --field FIELD_GID --data '{"value":8}'` で数値を設定する。単一選択は有効な選択肢GIDを文字列で指定し、テキストは文字列を渡す。解除は `{"value":null}` を使う。入力は `--data-file` でも渡せる。
- 更新対象はプロジェクトに所属するタスクの、適用済みの単一選択・数値・テキスト項目。数値はフィールドの精度に合わせて指定する。アカウントの契約や閲覧・編集権限により利用できない場合がある。

[項目定義の取得](https://developers.asana.com/reference/getcustomfieldsettingsforproject) · [カスタムフィールドの仕様](https://developers.asana.com/docs/custom-fields-guide)

## メンション付きコメント

`comment --gid TASK_GID --mention USER_GID --text "確認をお願いします ✅"` で、本文の先頭にユーザーメンションを追加する。`--mention` は複数回指定でき、本文は `--text-file` でも渡せる。
相手がタスクの担当者・フォロワーに含まれていない場合、メンションだけではAsanaから自動通知されない。投稿済みでメンションの反映を確認できない場合は、返されたコメントGIDで確認する。

[Asanaのメンションと通知](https://developers.asana.com/docs/rich-text#links)

## 添付ファイル

`attachments --gid TASK_GID` で添付名、GID、提供元、利用できる閲覧用URLを取得する。説明文内の画像も含まれる。空の一覧は取得成功を示す。

- `upload-attachment --gid TASK_GID --file PATH` でローカルファイル1件をアップロードする。上限は100 MB。反映確認にはAsanaが返した添付GIDを使う。
- `delete-attachment --gid TASK_GID --attachment ATTACHMENT_GID` で添付を削除する。削除前に指定タスクへの所属を確認し、削除後に一覧から外れたことを確認する。先に `attachments` で添付GIDを調べる。

新しい一覧コマンドは `--max-items`（既定100）と `--offset` に対応する。後続ページがあるかは `has_more` と `next_offset` で確認する。

[添付一覧API](https://developers.asana.com/reference/getattachmentsforobject) · [アップロードAPI](https://developers.asana.com/reference/createattachmentforobject) · [削除API](https://developers.asana.com/reference/deleteattachment)
