#!/usr/bin/env python3

import asyncio
import logging
import os
import signal
from pathlib import Path

from call_session import SessionConfig
from sip_bot import SipBot
from utils.recording import validate_prompt_wav

ASSETS_DIR = Path(__file__).resolve().parent / "assets"


def setup_logging() -> None:
    """ロギングの設定"""
    log_level = os.getenv("LOG_LEVEL") or "INFO"
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_session_config() -> SessionConfig:
    """環境変数からセッション設定を読み込み、プロンプトWAVを検証する

    空文字のenv（.env.exampleの未記入項目）は既定値にフォールバックする。
    """
    greeting = os.getenv("VOICEMAIL_GREETING_WAV") or str(ASSETS_DIR / "greeting.wav")
    reject = os.getenv("REJECT_ANNOUNCE_WAV") or str(ASSETS_DIR / "reject.wav")
    beep = str(ASSETS_DIR / "beep.wav")
    for wav in [greeting, reject, beep]:
        validate_prompt_wav(wav)  # 不正なら起動エラー

    return SessionConfig(
        auto_block_enabled=(os.getenv("AUTO_BLOCK_ENABLED") or "true").lower() == "true",
        voicemail_enabled=(os.getenv("VOICEMAIL_ENABLED") or "false").lower() == "true",
        answer_delay_sec=float(os.getenv("VOICEMAIL_ANSWER_DELAY_SEC") or "20"),
        max_duration_sec=float(os.getenv("VOICEMAIL_MAX_DURATION_SEC") or "120"),
        greeting_wav=greeting,
        beep_wav=beep,
        reject_wav=reject,
        recordings_dir=os.getenv("RECORDINGS_DIR") or "./recordings",
    )


async def pjsip_pump(bot: SipBot, stop: asyncio.Event) -> None:
    """PJSIPイベントを汲み上げる常駐タスク

    libHandleEvents(10)は最大10ms同期ブロックする。
    各ポーリング後に必ずイベントループへ制御を返す。
    """
    logger = logging.getLogger("main")
    while not stop.is_set():
        handled = bot.ep.libHandleEvents(10)
        if handled < 0:
            logger.error(f"libHandleEvents error: {handled}")
        await asyncio.sleep(0)


async def amain() -> None:
    setup_logging()
    logger = logging.getLogger("main")
    logger.info("スパムブロッカー＆ロガーを起動します...")

    bot = SipBot(
        sip_domain=os.getenv("SIP_DOMAIN") or "192.168.1.1",
        sip_user=os.getenv("SIP_USER") or "200",
        sip_auth_user=os.getenv("SIP_AUTH_USER") or None,
        sip_password=os.getenv("SIP_PASSWORD") or "",
        webhook_url=os.getenv("WEBHOOK_URL") or None,
        session_config=load_session_config(),
        groq_api_key=os.getenv("GROQ_API_KEY") or None,
        transcribe_model=os.getenv("TRANSCRIBE_MODEL") or "whisper-large-v3-turbo",
        mqtt_broker=os.getenv("MQTT_BROKER") or None,
        mqtt_port=int(os.getenv("MQTT_PORT") or "1883"),
        mqtt_topic=os.getenv("MQTT_TOPIC") or None,
        mqtt_username=os.getenv("MQTT_USERNAME") or None,
        mqtt_password=os.getenv("MQTT_PASSWORD") or None,
    )

    stop_requested = asyncio.Event()
    pump_stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_requested.set)

    # libCreate〜libDestroyまで同一スレッド(このループのスレッド)で行う
    bot.start()
    pump = asyncio.create_task(pjsip_pump(bot, pump_stop))
    try:
        logger.info("ボットが正常に起動しました")

        # シグナル、またはポンプの異常終了を待つ
        stop_task = asyncio.create_task(stop_requested.wait())
        done, _pending = await asyncio.wait(
            {stop_task, pump}, return_when=asyncio.FIRST_COMPLETED
        )
        stop_task.cancel()
        if pump in done and pump.exception() is not None:
            logger.error("PJSIPポンプが異常終了しました", exc_info=pump.exception())

        logger.info("シャットダウン中...")
        # shutdown中もSIPイベント処理(BYE送信等)が必要なのでポンプは動かしたまま
        await bot.shutdown()
    finally:
        pump_stop.set()
        if not pump.done():
            await pump
        bot.destroy()


def main() -> None:
    try:
        asyncio.run(amain())
    except Exception:
        logging.getLogger("main").error("ボットの起動に失敗しました", exc_info=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
