import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger("Webhook")

# 4xxレスポンスボディをJSONとして解釈する上限。
# webhookはユーザー自身の信頼済みエンドポイントなのでDoS対策ではなく、
# 巨大レスポンスを誤って全量パースしないためのパース上限。
MAX_BODY_BYTES = 64 * 1024
VALID_ACTIONS = frozenset({"hangup", "announce", "voicemail"})


@dataclass(frozen=True)
class SpamVerdict:
    """スパム判定の結果"""

    is_spam: bool
    action: str  # "none" | "hangup" | "announce" | "voicemail"
    reason: Optional[str]
    # action=announce のときの再生メッセージ名（announce_<name>.wav を選択）。
    # None なら既定の拒否アナウンスを使う
    message: Optional[str] = None


# announce の message 名として許可する形式（ファイル名に使うため厳格に）
MESSAGE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def parse_webhook_response(status_code: int, body: bytes) -> SpamVerdict:
    """Webhookレスポンスを判定に変換する純粋関数

    - 2xx: 非スパム
    - 4xx: スパム。JSONボディのactionに従う（不正・欠落時はhangup=従来動作）
    - その他(1xx/3xx/5xx): 非スパム扱い（フェイルオープン、従来動作）
    """
    if 200 <= status_code < 300:
        return SpamVerdict(False, "none", f"Webhook: {status_code}")

    if 400 <= status_code < 500:
        action = "hangup"
        reason = f"Webhook: {status_code}"
        try:
            data = json.loads(body[:MAX_BODY_BYTES])
        except ValueError:
            data = None
        message = None
        if isinstance(data, dict):
            raw_action = data.get("action")
            if isinstance(raw_action, str) and raw_action in VALID_ACTIONS:
                action = raw_action
            raw_reason = data.get("reason")
            if isinstance(raw_reason, str):
                reason = raw_reason
            raw_message = data.get("message")
            if (
                action == "announce"
                and isinstance(raw_message, str)
                and MESSAGE_NAME_RE.fullmatch(raw_message)
            ):
                message = raw_message
        return SpamVerdict(True, action, reason, message)

    logger.warning(f"Webhook: 予期しないステータスコード {status_code}")
    return SpamVerdict(False, "none", None)


async def check_spam(
    client: httpx.AsyncClient,
    url: Optional[str],
    from_number: str,
    to_number: str,
    timeout: float = 5.0,
) -> SpamVerdict:
    """Webhookでスパム判定を実行（URL未設定・エラー時は非スパム扱い）"""
    if not url:
        logger.debug("Webhook URLが設定されていません")
        return SpamVerdict(False, "none", None)

    params = {"from": from_number, "to": to_number}

    try:
        logger.info(f"Webhookリクエスト: {url} params={params}")
        response = await client.get(url, params=params, timeout=timeout)
        return parse_webhook_response(response.status_code, response.content)
    except Exception as e:
        logger.warning(f"Webhookエラー: {type(e).__name__}: {e}")
        return SpamVerdict(False, "none", None)
