# hai-hermes-agent-imagegen

[Hermes Agent](https://hermes-agent.nousresearch.com/) 用の **HAI 画像生成バックエンドプラグイン** です。
[HAI](https://hai.hcloud.ltd) の OpenAI 互換画像生成 API (`/v1/images/generations`) 経由で
**Krea 2 Medium Turbo** (`krea-2-medium-turbo`, 自社DC) を利用できます。

- テキスト → 画像 (text-to-image)
- 参照画像 1 枚 → 画像 (image-to-image)
- 出力は PNG (base64) を `$HERMES_HOME/cache/images/` に保存
- API キーは **環境変数 `HAI_API_KEY`** から取得 (`.env` への自動書き込みはしない)

> HAI は [OpenAI 互換](https://hai.hcloud.ltd/docs) で、`/v1/images/generations` は
> OpenAI SDK の `images.generate` とそのまま互換です。本プラグインはそれを
> Hermes の `image_generate` ツールから呼べる形に包んだものです。

---

## 仕組み

Hermes の `image_generate` ツール自体は**内蔵**のものです。画像生成の「実体」は
**プラグイン単位で差し替え可能**なプロバイダーとして実装されており、このプラグインは
その 1 つ (`provider: hai`) です。

```
image_generate (Hermes 内蔵ツール)
        │
        └─ image_gen.provider の設定でプロバイダー選択
               ├─ (内蔵) fal / openai / openrouter / krea / meta-ai / deepinfra / xai / ...
               └─ (このプラグイン) hai  ← HAI / krea-2-medium-turbo
```

### 依存

- Hermes Agent (本プラグインは `agent.image_gen_provider` の公開 API にのみ依存)
- Python 標準ライブラリ + `openai` (Hermes に同梱)

Hermes の非公開内部モジュールには触れず、バージョンアップ後も動くよう自包含にしています。

---

## インストール

### 事前準備

1. **HAI API キー** を取得 ([hai.hcloud.ltd](https://hai.hcloud.ltd))。
   画像生成は**有料キー** (`hai_...`) が必要で、無料トライアルキーでは
   `/v1/images/generations` を呼べません。
2. キーを**環境変数 `HAI_API_KEY`** に設定します。

   ```bash
   export HAI_API_KEY=hai_xxxxxxxxxxxx
   ```

   持続させる場合、Hermes の `.env` (`~/.hermes/.env`、またはアクティブ
   プロファイルの `.env`) に `HAI_API_KEY=hai_...` を追記してください。
   本プラグインは `.env` を自動で書き込まず、環境変数から読む想定です。

### プラグインの取得

```bash
hermes plugins install HCloud-Ltd/hai-hermes-agent-imagegen
```

インストール時に「有効化しますか?」と聞かれるので `y` (あるいは `--enable` 付きで
非対話)。

> リポジトリがプライベートの場合、`hermes plugins install` は非認証の
> `https` クローンを使うため、**リポジトリを公開**するか、
> 認証付きの Git URL を指定してください:
>
> ```bash
> hermes plugins install https://<token>@github.com/HCloud-Ltd/hai-hermes-agent-imagegen.git
> ```

### 設定

`config.yaml` (または `hermes config set`) に以下を追加します。

```yaml
image_gen:
  provider: hai
  model: krea-2-medium-turbo      # 任意 (この 1 つしかないため既定)
  hai:
    # base_url: https://hai-api.hcloud.ltd/v1   # 任意 (既定)
```

`hermes config set` 版:

```bash
hermes config set image_gen.provider hai
hermes config set image_gen.model krea-2-medium-turbo
```

> ユーザープラグインは**オプトイン**です。`plugins.enabled` に
> `hai-imagegen` を含める必要があります (インストール時の確認で `y` を
> 選べば自動で書かれます)。手動で:
>
> ```bash
> hermes plugins enable hai-imagegen
> ```

### 反映

プロバイダーの切替は**セッション単位**です。CLI は再起動、ゲートウェイ /
デスクトップは `/reset` (新規セッション) で新しいプロバイダーが有効になります。

---

## 使い方

### Hermes 内から

エージェントに画像生成を依頼するだけです。`image_generate` ツールが
`image_gen.provider: hai` を検出して HAI にルーティングします。

```
「朝の水面に浮かぶ赤い紙船を水彩風で描いて」
```

生成された画像は `$HERMES_HOME/cache/images/hai_*.png` として保存され、
チャットに添付として届けられます。

### OpenAI SDK で直接 (参照用)

プラグインなしで HAI を直接叩く場合:

```python
from openai import OpenAI
import base64

client = OpenAI(
    base_url="https://hai-api.hcloud.ltd/v1",
    api_key="hai_...",   # HAI_API_KEY
)

resp = client.images.generate(
    model="krea-2-medium-turbo",
    prompt="A red paper boat on a calm pond, soft morning light",
    size="1536x1024",      # 3:2。省略時は 1024x1024 (1:1)
)
open("out.png", "wb").write(base64.b64decode(resp.data[0].b64_json))
```

---

## 設定項目

| 場所 | キー | 既定 | 説明 |
|------|------|------|------|
| 環境変数 | `HAI_API_KEY` | (必須) | HAI API キー (`hai_...`) |
| `config.yaml` | `image_gen.provider` | — | `hai` に設定して有効化 |
| `config.yaml` | `image_gen.model` | `krea-2-medium-turbo` | 利用するモデル ID |
| `config.yaml` | `image_gen.hai.base_url` | `https://hai-api.hcloud.ltd/v1` | API ベース URL |
| 環境変数 | `HAI_IMAGE_BASE_URL` | (同上) | base_url の環境変数上書き |
| 環境変数 | `HAI_IMAGE_MODEL` | (model の上) | モデル ID の環境変数上書き |

### アスペクト比

Hermes の `image_generate` は `aspect_ratio` をセマンティック値
(`landscape` / `square` / `portrait`) で受け取り、内部で HAI が受理する
`size` に変換します:

| aspect_ratio | size | 比 |
|--------------|------|----|
| `landscape` (既定) | `1536x1024` | 3:2 |
| `square` | `1024x1024` | 1:1 |
| `portrait` | `1024x1536` | 2:3 |

HAI の `krea-2-medium-turbo` は `1:1 / 4:3 / 3:2 / 16:9 / 4:5 / 2:3 / 9:16`
を対応します。`16:9` 等のより狭い比は HAI 側で `aspect_ratio` として
指定できます (Hermes 標準の 3 値に収まらないため、このプラグインでは未提供)。

### image-to-image (参照画像 1 枚)

`image_generate` の `image_url` に画像 (ローカルパス / URL / data URL) を渡すと、
HAI の `input_references` に変換されて image-to-image が実行されます。
**最大 1 枚**です。

---

## 課金・制限

- 生成画像のトークン数 × 単価で課金されます (1K 1 枚 ≈ 4,175 image tokens
  ≈ 約 ¥3)。プロンプトのテキストトークンは課金されません。
- 失敗したリクエストは課金されません。
- 1 リクエスト = 1 枚 (`n > 1` は非対応、複数枚はリクエスト分割で)。
- `stream` / `response_format: url` / `output_format` は非対応 (400)。
- 定額プランの対象外で、クレジットから消費します。

詳細は [HAI ドキュメント](https://hai.hcloud.ltd/docs) を参照してください。

---

## トラブルシューティング

| 症状 | 原因 / 対処 |
|------|------------|
| `HAI_API_KEY が設定されていません` | 環境変数にキーがない。`export HAI_API_KEY=hai_...` または `.env` に追記 |
| `HAI が API キーを拒否しました (HTTP 401/403)` | トライアルキーまたは無効キー。有料キー (`hai_...`) を使用 |
| `openai パッケージが必要です` | `pip install openai` (Hermes には通常同梱) |
| `image_gen.provider='hai' is set but no plugin registered that name` | プラグインがインストール / 有効化されていない。`hermes plugins list` で確認し、`hermes plugins enable hai-imagegen` を実行 |
| 変更が反映されない | セッション単位で反映される。CLI は再起動、ゲートウェイは `/reset` |

---

## 免責

本プラグインは HAI の API を利用します。利用条件・料金・可用性は
[HAI](https://hai.hcloud.ltd) の各ページに準じます。

## ライセンス

[MIT](./LICENSE)
