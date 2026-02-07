import logging
from typing import Optional

import requests


logger = logging.getLogger("Webhook")


def check_spam(
    webhook_url: Optional[str],
    from_number: str,
    to_number: str,
    p_asserted_identity: Optional[str],
    timeout: int = 5,
) -> tuple[bool, Optional[str]]:
    """
    Webhookでスパム判定を実行

    Args:
        webhook_url: WebhookのURL（Noneの場合はスキップ）
        from_number: 発信者番号
        to_number: 着信先番号
        p_asserted_identity: P-Asserted-Identity
        timeout: タイムアウト（秒）

    Returns:
        (is_spam, reason)のタプル
        - is_spam: Trueならスパム、Falseなら非スパム
        - reason: 判定理由
    """
    if not webhook_url:
        logger.debug("Webhook URLが設定されていません")
        return False, None

    try:
        params = {
            "from": from_number,
            "to": to_number,
        }
        if p_asserted_identity:
            params["pai"] = p_asserted_identity

        logger.info(f"Webhookリクエスト: {webhook_url} params={params}")
        response = requests.get(webhook_url, params=params, timeout=timeout)

        # 200番台 → 非スパム
        if 200 <= response.status_code < 300:
            logger.info(f"Webhook判定: 非スパム (status={response.status_code})")
            return False, f"Webhook: {response.status_code}"

        # 400番台 → スパム
        if 400 <= response.status_code < 500:
            logger.info(f"Webhook判定: スパム (status={response.status_code})")
            return True, f"Webhook: {response.status_code}"

        # その他 → 非スパム扱い
        logger.warning(f"Webhook: 予期しないステータスコード {response.status_code}")
        return False, None

    except requests.Timeout:
        logger.warning(f"Webhookタイムアウト: {webhook_url}")
        return False, None
    except Exception as e:
        logger.error(f"Webhookエラー: {e}")
        return False, None
