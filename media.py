"""pjsua2のメディア操作ラッパー

pjsua2はmainThreadOnly=Trueで動かすため、SIPシグナリング系コールバックは
asyncioループのスレッドで発火する。ただしメディア系コールバック(onEof2)は
pjmediaの別スレッドから呼ばれ得るため、call_soon_threadsafeで橋渡しする。
"""

import asyncio
import logging
from typing import Optional

import pjsua2 as pj

from call_session import CallOperationError

logger = logging.getLogger("Media")


def configure_media(ep: pj.Endpoint) -> None:
    """null音声デバイスとコーデック優先順位(PCMU>PCMA、他無効)を設定する"""
    ep.audDevManager().setNullDev()
    for codec in ep.codecEnum2():
        if codec.codecId.startswith("PCMU/8000"):
            priority = 255
        elif codec.codecId.startswith("PCMA/8000"):
            priority = 254
        else:
            priority = 0  # 無効化
        ep.codecSetPriority(codec.codecId, priority)
    logger.info("コーデック優先順位を設定しました: PCMU/8000 > PCMA/8000")


async def _wait_any(events: list[asyncio.Event], timeout: Optional[float]) -> bool:
    """いずれかのEventがsetされるまで待つ。タイムアウトならFalse"""
    tasks = [asyncio.ensure_future(e.wait()) for e in events]
    done, pending = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    for p in pending:
        p.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    return bool(done)


class _EofPlayer(pj.AudioMediaPlayer):
    """EOFをasyncio.Eventに橋渡しするプレイヤー"""

    def __init__(self, loop: asyncio.AbstractEventLoop, done: asyncio.Event):
        super().__init__()
        self._loop = loop
        self._done = done

    def onEof2(self) -> None:
        # メディアコールバックはpjmediaの別スレッドから呼ばれ得るため
        # threadsafeにループへ渡す。プレイヤーの破棄はここでは絶対にしない。
        self._loop.call_soon_threadsafe(self._done.set)


class PjCallControl:
    """CallControl Protocolのpjsua2実装（sip_bot.MyCallをラップ）

    pj.Errorはすべて CallOperationError に変換する
    （CallSessionはpjsua2非依存のため）。
    """

    def __init__(self, call):
        self._call = call
        # Player/Recorder/AudioMediaのPython参照はセッション終了まで保持する
        # （GCに回収されると音が止まる・録音が壊れる）
        self._players: list[pj.AudioMediaPlayer] = []
        self._recorder: Optional[pj.AudioMediaRecorder] = None

    # ---- CallControl実装 ----

    def answer(self) -> None:
        prm = pj.CallOpParam()
        prm.statusCode = pj.PJSIP_SC_OK
        try:
            self._call.answer(prm)
        except pj.Error as e:
            raise CallOperationError(e.info()) from e

    def hangup(self) -> None:
        prm = pj.CallOpParam()
        try:
            self._call.hangup(prm)
        except pj.Error as e:
            raise CallOperationError(e.info()) from e

    @property
    def terminated(self) -> bool:
        return self._call.term.is_set()

    async def wait_terminated(self, timeout: Optional[float]) -> bool:
        return await _wait_any([self._call.term], timeout)

    async def wait_confirmed(self, timeout: float) -> bool:
        await _wait_any([self._call.confirmed, self._call.term], timeout)
        return self._call.confirmed.is_set() and not self._call.term.is_set()

    async def wait_media_active(self, timeout: float) -> bool:
        await _wait_any([self._call.media_active, self._call.term], timeout)
        return self._call.media_active.is_set() and not self._call.term.is_set()

    async def play_wav(self, path: str) -> None:
        done = asyncio.Event()
        player = _EofPlayer(asyncio.get_running_loop(), done)
        try:
            player.createPlayer(path, pj.PJMEDIA_FILE_NO_LOOP)
            player.startTransmit(self._call.audio_media)
        except pj.Error as e:
            raise CallOperationError(e.info()) from e
        self._players.append(player)
        try:
            await done.wait()
        finally:
            # キャンセル(タスク停止)時も送出は止める
            if not self.terminated:
                try:
                    player.stopTransmit(self._call.audio_media)
                except pj.Error as e:
                    logger.info(f"再生停止時に競合しました（正常）: {e.info()}")

    def start_recording(self, path: str) -> None:
        try:
            recorder = pj.AudioMediaRecorder()
            recorder.createRecorder(path)
            self._call.audio_media.startTransmit(recorder)
        except pj.Error as e:
            raise CallOperationError(e.info()) from e
        self._recorder = recorder

    def stop_recording(self) -> None:
        if self._recorder is None:
            return
        if not self.terminated:
            try:
                self._call.audio_media.stopTransmit(self._recorder)
            except pj.Error as e:
                logger.info(f"録音停止時に競合しました（正常）: {e.info()}")
        # 参照を落とすとSWIGデストラクタが走りWAVファイルが閉じられる
        self._recorder = None

    async def wait_record_end(self, timeout: float) -> None:
        # 終端(DISCONNECTED)・メディア喪失(hold/re-INVITE)・録音上限のいずれかまで待つ
        await _wait_any([self._call.term, self._call.media_lost], timeout)
