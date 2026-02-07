#!/usr/bin/env python3

import logging
import os
import signal
import sys

from sip_bot import SipBot


def setup_logging() -> None:
    """ロギングの設定"""
    log_level = os.getenv("LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    """メイン処理"""
    setup_logging()
    logger = logging.getLogger("main")

    logger.info("スパムブロッカー＆ロガーを起動します...")

    # SIPボットを作成
    bot = SipBot(
        sip_domain=os.getenv("SIP_DOMAIN", "192.168.1.1"),
        sip_user=os.getenv("SIP_USER", "200"),
        sip_auth_user=os.getenv("SIP_AUTH_USER"),
        sip_password=os.getenv("SIP_PASSWORD", ""),
        auto_block_enabled=os.getenv("AUTO_BLOCK_ENABLED", "true").lower() == "true",
        webhook_url=os.getenv("WEBHOOK_URL"),
        mqtt_broker=os.getenv("MQTT_BROKER"),
        mqtt_port=int(os.getenv("MQTT_PORT", "1883")),
        mqtt_topic=os.getenv("MQTT_TOPIC"),
        mqtt_username=os.getenv("MQTT_USERNAME"),
        mqtt_password=os.getenv("MQTT_PASSWORD"),
    )

    # シグナルハンドラー
    def signal_handler(sig: int, frame) -> None:
        logger.info("シャットダウン中...")
        bot.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        bot.start()
        logger.info("ボットが正常に起動しました")

        # メインループ（PJSIPのイベント処理）
        while True:
            import time

            time.sleep(1)

    except Exception as e:
        logger.error(f"ボットの起動に失敗しました: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
