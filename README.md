# spaburo-call

> 固定電話スパムブロッカー＆ロガー

PJSIP を用いて Python プログラムを固定電話の VoIP 子機として参加させます。
着信ログを MQTT で配信するとともに、Webhook により動的なスパム判定を行い、スパム判定されれば子機側で通知・切断することで擬似的に着信拒否をすることができます。

Webhook はレスポンスコードに 2xx を返すことでスパムではない（無視）、4xx を返すことでスパムである（着拒）とシンプルに判定できるため、ユーザーが柔軟にスパム検出ロジックを組むことができます。

あくまで固定電話の VoIP 子機としてプログラムを参加させるため、PBX 等を追加で設置させる必要がなく、簡易的に実行できることが特徴です。

## 使い方

### 1. 環境変数の設定

```bash
cp .env.example .env
# .envを編集
```

### 2. 起動

```bash
docker compose up -d --build
```

### 3. ログ確認

```bash
docker compose logs -f
```

## 設定

### 必須

```env
SIP_DOMAIN=192.168.1.1
SIP_USER=4
SIP_AUTH_USER=0004
SIP_PASSWORD=your_password
AUTO_BLOCK_ENABLED=true
```

### Webhook（スパム判定）

```env
WEBHOOK_URL=http://your-server/api/check-spam
```

GETリクエストで `?from={発信番号}&to={着信番号}&pai={P-Asserted-Identity}` を送信。

- 200番台: 非スパム → 着信を無視（親機等で受信できる状態にする）
- 400番台: スパム → 着信をワン切りしてブロック
- タイムアウト/エラー: 非スパム扱い

挙動をテストするには、Webhook URL として `https://httpbin.org/status/403` 等を設定します。

400番台のレスポンスにJSONボディを付けると、ブロック方法を指定できます（省略時はワン切り）:

```json
{"action": "voicemail", "reason": "telnavi score 1.2"}
```

- `hangup`: 即応答→即切断（ワン切り、従来動作）
- `announce`: アナウンスを再生してから切断（既定は「お受けできません」）
- `voicemail`: 強制留守電（挨拶→録音→文字起こし）

`announce` は `message` でアナウンスの種類を選べます:

```json
{"action": "announce", "message": "sales"}
```

`assets/announce_<name>.wav` が起動時に自動検出され、`message` の名前で選択されます（同梱: `sales` = 「リストからこの番号を削除してください。セールスや勧誘のお電話は固くお断りしております。」）。
`message` 省略時・未知の名前のときは既定の拒否アナウンス（`REJECT_ANNOUNCE_WAV`）を再生します。独自のアナウンスは 8kHz mono 16bit PCM の WAV を `assets/announce_<name>.wav` として置くだけで追加できます。

`AUTO_BLOCK_ENABLED=false` の場合はどのactionでも通話操作をせず、判定ログのみ記録します
（`VOICEMAIL_ENABLED=true` なら通常留守電のタイマーは動作します）。

### 留守電

```env
VOICEMAIL_ENABLED=false
VOICEMAIL_ANSWER_DELAY_SEC=20
VOICEMAIL_MAX_DURATION_SEC=120
VOICEMAIL_GREETING_WAV=
REJECT_ANNOUNCE_WAV=
RECORDINGS_DIR=./recordings
OPENAI_API_KEY=
TRANSCRIBE_API_URL=
TRANSCRIBE_MODEL=whisper-large-v3-turbo
```

- `VOICEMAIL_ENABLED=true` にすると、スパム判定に関わらず、着信からN秒（`VOICEMAIL_ANSWER_DELAY_SEC`）応答がない場合にボットが代わりに応答し、留守電として動作します（電話機がN秒以内に応答すればボットは何もしません）。
- Webhookの `action=voicemail` によって強制的に留守電にする場合はこの遅延を待たず即座に応答します。
- 挨拶・拒否アナウンスの音声は `VOICEMAIL_GREETING_WAV` / `REJECT_ANNOUNCE_WAV` で差し替え可能です（8kHz mono 16bit PCMのWAV）。未設定時は同梱のデフォルト音声（`assets/`）を使用します。
- 録音は `RECORDINGS_DIR`（コンテナ内は `RECORDINGS_DIR=./recordings` で `/app/recordings` を指す）に保存され、Docker Composeでは `./recordings:/app/recordings` としてホスト側にも公開されます（`compose.yml` 参照）。
- 録音は `VOICEMAIL_MAX_DURATION_SEC` に達するか、相手が切断する、メディアが失われるまで継続します。
- 録音完了後、`OPENAI_API_KEY` を設定していればOpenAI互換の文字起こしAPIへ録音ファイルを送信して文字起こしを行います。エンドポイントは `TRANSCRIBE_API_URL` で指定でき、未設定時はGroq（`https://api.groq.com/openai/v1/audio/transcriptions`）を使用します。**録音音声が外部APIに送信される**ため、取り扱いに注意してください。`OPENAI_API_KEY` 未設定の場合は文字起こしをスキップします（`transcription_error` にその旨が記録されます）。

### MQTT（ログ送信）

```env
MQTT_BROKER=mqtt.example.com
MQTT_PORT=1883
MQTT_TOPIC=spaburo-call/logs
MQTT_USERNAME=user  # オプション
MQTT_PASSWORD=pass  # オプション
```

以下のJSON形式でログを送信:

```json
{
  "timestamp": "2026-02-06T18:30:00.000Z",
  "action": "received|spam_detected|legitimate|blocked|voicemail_recorded",
  "from": "09012345678",
  "p_asserted_identity": null,
  "to": "0312345678",
  "reason": "Webhook: 403"
}
```

`action=voicemail_recorded` は留守電の録音が完了した際に送信され、以下のフィールドが追加されます:

```json
{
  "timestamp": "2026-02-06T18:30:00.000Z",
  "action": "voicemail_recorded",
  "from": "09012345678",
  "p_asserted_identity": null,
  "to": "0312345678",
  "duration_sec": 12.3,
  "recording_path": "/app/recordings/20260206-183000_a1b2c3.wav",
  "transcription": "お世話になっております。〜",
  "transcription_error": null
}
```

- `duration_sec`: 録音の長さ（秒）。0秒の場合もあります。
- `recording_path`: 保存された録音ファイルのパス（コンテナ内パス）。
- `transcription`: 文字起こし結果。取得できなかった場合は `null`。
- `transcription_error`: 文字起こしに失敗・スキップした理由（`OPENAI_API_KEY` 未設定、シャットダウン中断など）。成功時は `null`。

今後 `action` の種類が増える可能性があるため、**subscriber は未知のactionを無視するように実装してください**。
