# spaburo-call

> 固定電話スパムブロッカー＆ロガー

PJSIP を用いて Python プログラムを固定電話の VoIP 子機として参加させます。
着信ログを MQTT で配信するとともに、Webhook により動的なスパム判定を行い、スパム判定されれば子機側で（着信を）ワン切りすることで擬似的に着信拒否をすることができます。

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
  "action": "received|spam_detected|legitimate|blocked",
  "from": "09012345678",
  "p_asserted_identity": null,
  "to": "0312345678",
  "reason": "Webhook: 403"
}
```
