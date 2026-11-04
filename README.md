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



## インストール

ターミナルで実行します。

```sh
git clone https://github.com/karugamo5/Orca.git
cd Orca
./setup.sh
```

最新の正式なGitHub Releaseを取得してインストールします。取得やチェックサムの確認に失敗した場合は中止します。手元のソースをインストールする場合は、代わりに`./setup.sh --source`を実行します。

最初の正式Releaseが公開されるまでは、`./setup.sh --source`を使用してください。

セットアップは`~/.local/bin/orca`を配置し、`~/.zprofile`へPATH設定を追記します。


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

## メニューバー

メニューバーのOrcaを開くと、次の4つの欄を表示します。

| 欄 | 表示と操作 |
|---|---|
| 状態 | 稼働中・停止中・エラーを表示 |
| モデル | `id (model)`と接続状態を表示。チェックで使用許可を切り替え、更新ボタンで接続状態を再確認。追加ボタンでモデルを登録 |
| ツール | ツールIDと割り当て件数を表示。件数ボタンからモデルを複数選択し、追加ボタンでツールを登録 |
| キュー | 待機件数・上限、依頼の送信元・状態・経過時間を表示 |



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


```json
{"keychain": {"service": "orca-local-llm", "account": "local-a"}}
```

```json
{"token": "replace-with-your-token"}
```

`keychain`と`token`は併用できません。両方を省略すると認証なしです。


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

```sh
orca model --start local-a
orca model --status
orca model --stop local-a
```


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


## ログイン時の自動起動

既定では無効です。モデルサーバーを起動してから実行します。

```sh
orca --auto_start true
```

無効にする場合は`orca --auto_start false`を実行します。

## 更新とアンインストール

更新する場合は、クローンしたディレクトリで再度実行します。

```sh
./setup.sh
```
アンインストールは次を実行し、削除確認に承諾します。

```sh
./uninstall.sh
```


## データの保存

本体と履歴・ログの配置先は`~/Library/Application Support/Orca`です。リクエストのメタ情報は終了から30日間保持します。

## ライセンス

Apache License 2.0

第三者ライブラリのライセンスは[THIRD_PARTY_NOTICES.md](third_party/THIRD_PARTY_NOTICES.md)にまとめています。

