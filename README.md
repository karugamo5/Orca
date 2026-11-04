# Orca

Orcaは、CodexからローカルLLMへテキストやコードの生成を依頼するためのmacOS向けMCPサービスです。複数のモデルを登録し、使用するモデルやツールごとの割り当てをメニューバーとCLIから管理できます。

（このソフトウェアの開発にはAIの利用が含まれます）
## 主な機能

- 使用許可がありオンラインのモデルから、速度と空き状況に応じて依頼先を選択します。
- モデルごとの同時実行数を制限し、空きがない場合は受付順に待機します。
- 接続障害、タイムアウト、HTTP 429/503では最大2回再試行します。
- モデルの追加・編集、接続確認、ローカルモデルサーバーの起動・終了に対応します。
- 公開するMCPツールと、各ツールへ割り当てるモデルを設定できます。
- メニューバーで稼働状態、モデルの使用許可・接続状態、ツールの割り当て、キューを確認できます。

## 動作環境

- macOS 13以上
- Python 3.11以上
- Swift tools 5.9以上
- Git
- モデル一覧の取得と非ストリーミングのChat Completionsに対応したOpenAI互換APIのモデルサーバー

モデルサーバーとモデルは利用者が別途用意します。

現在は初期版（alpha）です。アプリは導入時にソースからビルドし、署名・公証済みバイナリは配布していません。実モデル、最低対応macOS、利用者環境での導入・更新の確認はまだ完了していません。

## インストール

ターミナルで実行します。

```sh
git clone https://github.com/karugamo5/Orca.git
cd Orca
./setup.sh
```

最新の正式なGitHub Releaseを取得してインストールします。取得やチェックサムの確認に失敗した場合は中止します。手元のソースをインストールする場合は、代わりに`./setup.sh --source`を実行します。

最初の正式Releaseが公開されるまでは、`./setup.sh --source`を使用してください。

セットアップは`~/.local/bin/orca`を配置し、`~/.zprofile`へPATH設定を追記します。最後にCodexへのMCP登録を確認するため、連携する場合は承諾してください。既存のCodex設定は保持し、異なるOrca登録がある場合は競合として登録を中止します。

完了後に新しいターミナルを開きます。初回はモデル未登録、自動起動無効の状態です。Orcaとモデルサーバーは自動起動しません。

```sh
orca --version
orca --help
```

## モデルの登録と起動

### モデルを登録する

```sh
orca model --set
```

ID、APIモデル名、URL、認証方式、同時実行数、速度を順に入力します。同じIDを指定すると既存モデルを編集し、新しいIDなら追加します。Enterで表示された既定値を採用します。

- IDはOrca内の識別子です。CLIのモデル指定にも使用します。
- APIモデル名は接続先のモデル一覧に掲載されているIDです。
- URLは`http(s)://host[:port]/v1`の形式で指定します。
- 認証方式は認証なし、または既存のKeychain登録を指定できます。
- 速度に`auto`を指定すると実測値を使い、未計測なら100 tokens/secを使います。

保存後の`Restart Orca to reload settings?`に`yes`と答えるとOrcaを再起動します。再起動すると待機中・実行中の依頼はキャンセルされます。空入力・`no`では再起動せず、稼働中のOrcaはモデル定義の変更を自動反映します。

### 接続を確認して起動する

モデルサーバーを起動した状態で実行します。

```sh
orca test
orca launch
orca monitor
```

`test`は全登録モデルの認証と`/v1/models`への接続、設定モデルの掲載を確認します。Orcaが停止中でも実行できます。推論は実行しません。

`launch`はOrcaとメニューバーを起動します。`monitor`は実行中・待機中の依頼を受付順に一度表示して終了します。

```sh
orca stop
orca restart
orca menu
```

`stop`は待機中・実行中の依頼をキャンセルしてOrcaを停止します。`restart`は設定を読み直して起動し、メニューバーも開きます。`menu`はメニューバーだけを開きます。Codexやメニューバーを閉じてもOrcaは継続します。停止中のMCP呼び出しからOrcaは自動起動しません。

### 使用するモデルを限定する

```sh
orca launch --model local-a local-b
orca test --model local-a local-b
```

`--model`には`models.json`の`id`を指定します。`launch`で指定した範囲は、そのOrcaが停止するまで監視・依頼先の対象になります。使用許可とオンライン状態も必要です。稼働中の対象範囲を変更する場合は、一度`orca stop`を実行します。`restart`や停止後の通常の`launch`では全モデルが対象に戻ります。

## Codexからの利用

セットアップ時にMCP登録を承諾した環境では、設定したツールをCodexから呼び出して処理を依頼できます。初期状態では`generate_text`を公開します。ツール名とモデルの割り当ては`tools.json`で変更できます。

すべてのツールは次の入力を受け取り、生成されたテキストを返します。

| 入力 | 内容 |
|---|---|
| `prompt` | 必須。処理内容を記載した文字列。空白のみは不可、UTF-8で128KiBまで |
| `max_tokens` | 任意。出力トークン数の上限。1〜65536の整数 |

`generate_code`などの名前でツールを追加した場合も、コードを文字列として返します。ツール自体はファイル編集やコード実行を行いません。

## メニューバー

メニューバーのOrcaを開くと、次の4つの欄を表示します。

| 欄 | 表示と操作 |
|---|---|
| 状態 | 稼働中・停止中・エラーを表示 |
| モデル | `id (model)`と接続状態を表示。チェックで使用許可を切り替え、更新ボタンで接続状態を再確認。追加ボタンでモデルを登録 |
| ツール | ツールIDと割り当て件数を表示。件数ボタンからモデルを複数選択し、追加ボタンでツールを登録 |
| キュー | 待機件数・上限、依頼の送信元・状態・経過時間を表示 |

モデルの接続状態は、オンラインが緑、オフラインが赤、未確認が灰色です。使用許可を解除しても、実行中の依頼は継続します。全モデルの使用許可を解除すると、新規依頼は受け付けられません。使用許可は再起動後も保持します。

ツールの「すべてのモデル（追加分も含む）」を選ぶと、今後登録するモデルも対象になります。全解除すると、そのツールの依頼先がなくなります。割り当ての変更は保存して即時反映します。

モデル・ツールの追加はOrca停止中でも可能です。使用許可・割り当て変更・接続状態の更新は稼働中に行います。ローカルモデルの起動設定がある場合は、モデル行から「開始」「終了」も操作できます。

## 設定ファイル

設定ファイルは`~/.config/orca/`に配置します。UTF-8のJSONで記述し、ファイルごとの上限は1MiBです。未知の項目、重複キー、不正な型は受け付けません。

| ファイル | 内容 | 反映 |
|---|---|---|
| `config.json` | 監視間隔、キュー、タイムアウトなどの共通設定 | Orcaの起動・再起動時 |
| `models.json` | 接続するモデルの定義 | 稼働中は1秒周期で変更を検出 |
| `tools.json` | 公開するMCPツールとモデルの割り当て | 稼働中は1秒周期で変更を検出 |

モデル・ツール定義の不正な変更は反映せず、直前の正常な設定を使用します。起動時に設定が不正な場合は起動できません。

### 共通設定

`config.json`の既定値です。

```json
{
  "auto_start": false,
  "health_check": {"interval_seconds": 300},
  "queue": {"max_pending": 10},
  "timeouts": {
    "connect_seconds": 10,
    "request_seconds": 120,
    "health_seconds": 10
  }
}
```

| 項目 | 内容 |
|---|---|
| `auto_start` | ログイン時の自動起動。CLIの`--auto_start true/false`で変更 |
| `health_check.interval_seconds` | 接続状態の監視間隔。1秒以上の整数 |
| `queue.max_pending` | 実行中を除く待機上限。0以上の整数。0は無制限 |
| `timeouts.connect_seconds` | HTTP接続の期限（秒） |
| `timeouts.request_seconds` | 推論1試行全体の期限（秒） |
| `timeouts.health_seconds` | 監視・接続確認の期限（秒） |

タイムアウトには0より大きい数値を指定します。監視と接続確認は`/v1/models`を使い、推論を行いません。

### モデル定義

`models.json`へ直接記載する場合の例です。`model`と`base_url`を利用するサーバーに合わせて変更してください。

```json
{
  "models": [
    {
      "id": "local-a",
      "model": "backend-model-a",
      "base_url": "http://127.0.0.1:8000/v1",
      "max_concurrent_requests": 1,
      "tokens_per_second": null
    }
  ]
}
```

| 項目 | 内容 |
|---|---|
| `id` | 必須。一意な識別子。ASCII英数字で開始し、英数字・`_`・`.`・`-`で128文字まで |
| `model` | 必須。APIへ送るモデルID |
| `base_url` | 必須。`http(s)://host[:port]/v1`形式の接続先 |
| `keychain` | 任意。既存のgeneric-passwordを指定する`service`と`account` |
| `token` | 任意。Bearerトークン。JSONの直接編集で登録 |
| `headers` | 任意。追加ヘッダー。`X-Client-Name`と`X-Request-Source`のみ |
| `max_concurrent_requests` | 同時実行上限。1〜1024の整数。省略時1 |
| `tokens_per_second` | 選択に使う速度。正の数値。省略・nullでは実測値、未計測なら100 tokens/sec |
| `local_start` | 任意。同じMac上のモデルサーバーの起動設定 |

認証が必要な場合は、モデル定義へ次のいずれかを追加します。

```json
{"keychain": {"service": "orca-local-llm", "account": "local-a"}}
```

```json
{"token": "replace-with-your-token"}
```

`keychain`と`token`は併用できません。両方を省略すると認証なしです。OrcaはKeychainの既存登録を読み取るだけで、登録・更新・削除は行いません。

トークンは空白・制御文字を含まないASCIIの1〜4096文字で指定します。対話式CLIやメニューバーからは入力できません。既存のトークンは`orca model --set`で`keep`を選ぶと非表示のまま保持します。トークンを記載した設定ファイルは所有者だけがアクセスできるよう管理してください。

### ツール定義

`tools.json`の例です。

```json
{
  "tools": [
    {"id": "generate_text", "models": ["all"]},
    {
      "id": "generate_code",
      "description": "コードの生成・修正を依頼します",
      "models": ["local-a"]
    }
  ]
}
```

`id`がMCPツール名になります。ASCII英数字・`_`・`.`・`-`で1〜128文字、一意に指定します。`description`は任意で、空白のみを除く1024文字以内です。

`models`には`models.json`の`id`を指定します。`["all"]`は全登録モデルで、他のIDと併記できません。空配列は割り当てなしです。`tools`自体を空配列にすると、すべてのツールを非公開にします。

実行時は指定先のうち、使用許可がありオンラインで、起動時のモデル限定にも含まれるモデルを使います。

### ローカルモデルサーバーの起動設定

同じMac上のモデルをOrcaから起動・終了する場合は、モデル定義に`local_start`を追加します。以下はモデル定義内へ追加する項目です。

```json
{
  "local_start": {
    "command": "/absolute/path/to/model-server",
    "args": [],
    "startup_timeout_seconds": 300
  }
}
```

`command`を実行ファイルの絶対パス、`args`をそのサーバーに必要な引数の文字列配列へ置き換えてください。APIのモデル名・ホスト・ポートと起動引数を合わせます。`base_url`のホストは`127.0.0.1`、`localhost`、`[::1]`のいずれかにします。

シェルや`~`・環境変数の展開は使えません。フォアグラウンドで動き続けるコマンドを指定します。`startup_timeout_seconds`は起動完了までの期限で、既定300秒、0より大きく86400秒以下です。

```sh
orca model --start local-a
orca model --status
orca model --stop local-a
```

開始・終了は完了まで待ちます。`--wait false`を付けると要求の受付後に戻り、結果を`--status`で確認できます。Orca停止中でも操作可能です。外部で起動したモデルサーバーは終了できません。

`orca stop`・`orca restart`やメニューバーの終了では、モデルサーバーは終了しません。モデルの終了は実行中・待機中の依頼を待たずに行います。モデルの使用許可と起動・終了は個別に操作します。

## 主なコマンド

| コマンド | 動作 |
|---|---|
| `orca --help` / `orca -h` | 全体のヘルプ |
| `orca model --help` | 動作別のヘルプ。他の動作も同様 |
| `orca --version` | インストール済みの版番号を表示 |
| `orca --license` | Orcaのライセンス全文を表示 |
| `orca launch` | Orcaとメニューバーを起動 |
| `orca launch --model ID [ID ...]` | 今回使用するモデルを限定して起動 |
| `orca stop` | 待機中・実行中の依頼をキャンセルしてOrcaを停止 |
| `orca restart` | Orcaを再起動し、設定を再読込 |
| `orca monitor` | 実行中・待機中の依頼を一度表示 |
| `orca test` | 全モデルの認証・接続を確認 |
| `orca test --model ID [ID ...]` | 指定モデルの認証・接続を確認 |
| `orca model --set` | モデルを対話式で追加・編集 |
| `orca model --enable ID` / `orca model --disable ID` | モデルの使用許可を切替 |
| `orca model --refresh` | 接続状態を再確認 |
| `orca model --start ID` / `orca model --stop ID` | ローカルモデルサーバーを起動・終了 |
| `orca model --status` | モデルサーバーのプロセス・API状態を表示 |
| `orca menu` | メニューバーを開く |
| `orca --auto_start true` / `orca --auto_start false` | ログイン時の自動起動を有効化・無効化 |

1回の呼び出しで指定できる動作は1つです。`--auto_start true/false`は動作を付けず単独で指定します。ターミナルの表示は英語です。終了コードは0が成功、1が処理失敗、2が引数不正、130が割込みです。

## ログイン時の自動起動

既定では無効です。モデルサーバーを起動してから実行します。

```sh
orca --auto_start true
```

無効にする場合は`orca --auto_start false`を実行します。どちらも全登録モデルの認証・接続確認が成功した場合だけ設定を変更します。モデル未登録・設定不正・確認失敗では変更しません。

有効にすると次回ログイン時にOrcaとメニューバーを起動します。設定変更時の起動・停止状態は変わりません。モデルサーバーは自動起動しません。`orca stop`は自動起動設定を変更しません。

## 更新とアンインストール

更新する場合は、クローンしたディレクトリで再度実行します。

```sh
./setup.sh
```

更新確認に承諾すると、設定・履歴・ログを保持して更新します。不足する設定ファイルがある場合は作成を確認します。Orcaとメニューバーは、更新前に稼働していたものを更新後に再開します。

アンインストールは次を実行し、削除確認に承諾します。

```sh
./uninstall.sh
```

Orcaが起動したモデルサーバー、Orca本体、メニューバーを停止し、本体・専用環境・設定・履歴・専用LaunchAgentを削除します。エラーログ、Keychain、CodexのMCP登録、PATHへの追記は残ります。不要になったMCP登録やPATH設定は利用者が削除してください。

## データの保存

本体と履歴・ログの配置先は`~/Library/Application Support/Orca`です。リクエストのメタ情報は終了から30日間保持します。依頼本文・生成本文・APIキーは履歴やログへ保存しません。例外ログは自動削除しません。

## ライセンス

Apache License 2.0

第三者ライブラリのライセンスは[THIRD_PARTY_NOTICES.md](third_party/THIRD_PARTY_NOTICES.md)にまとめています。

## 開発とリリース

単体試験・Swift/Python通信・アプリのビルド・導入試験は、公開前にローカルで検証します。テスト本体と専用の検証補助ツールはローカルに保持し、公開リポジトリとRelease配布物には含めません。

[CI](https://github.com/karugamo5/Orca/actions/workflows/ci.yml)はpush・pull request・手動実行に対応します。macOS上のPython 3.15で、Python・シェルの構文と、wheel・sdistの版番号・必要ファイル・ライセンスを確認し、`SHA256SUMS`を生成します。単体試験、Swiftビルド、通信・導入試験、配布物の再ビルド照合は実行しません。

Releaseを作成する場合は、ローカルで検証したソースと`.github/workflows/`をコミットしてGitHubへpushし、既定ブランチにRelease workflowを配置します。[Actions → Release](https://github.com/karugamo5/Orca/actions/workflows/release.yml)の **Run workflow** で対象のブランチまたはタグを選び、`version`へ`0.1.0`のような版番号だけを入力します。`v`接頭辞と先頭ゼロは受け付けません。選択した参照の実行時コミットを公開対象にします。

最低限の確認が成功した後、wheel・sdist・`SHA256SUMS`をGitHub Releaseへ公開します。通常のpushやタグ作成では公開しません。既存Releaseと別コミットのタグは上書きせず、添付に失敗した場合は未公開のdraftを残して停止します。再実行前にdraftと添付内容を確認してください。

指定版番号はCIの作業コピーと添付配布物へ反映します。タグは元のコミットを指すため、GitHubの自動生成Source codeアーカイブには元の版番号が残ります。
