import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Protocol

from utils.recording import finalize_recording, new_recording_path, wav_duration_sec
from utils.webhook import SpamVerdict

logger = logging.getLogger("CallSession")

# メディア確立(onCallMediaState)を待つ上限秒数
MEDIA_ACTIVE_TIMEOUT = 10.0
# hangupアクションでCONFIRMED(ACK)を待つ上限秒数
CONFIRM_TIMEOUT = 5.0

# _raceの「通話が先に終端した」ことを表すsentinel
TERMINATED = object()

# wav_duration_secリトライの最大回数と間隔（録音停止直後のファイルクローズが非同期な場合に備える）
_WAV_DURATION_RETRY_COUNT = 20
_WAV_DURATION_RETRY_INTERVAL_SEC = 0.1


async def _wav_duration_sec_with_retry(path: str) -> float:
    """wav_duration_secをリトライ付きで実行する

    stop_recording()直後はWAVファイルのクローズが非同期な場合があり、
    直後の読み取りが失敗し得るため、一定回数リトライする。
    """
    last_error: Optional[Exception] = None
    for attempt in range(_WAV_DURATION_RETRY_COUNT):
        try:
            return wav_duration_sec(path)
        except Exception as e:
            last_error = e
            if attempt < _WAV_DURATION_RETRY_COUNT - 1:
                await asyncio.sleep(_WAV_DURATION_RETRY_INTERVAL_SEC)
    assert last_error is not None
    raise last_error


class CallOperationError(Exception):
    """切断との競合などでCall操作が失敗したことを表す（pjsua2非依存）

    pjsua2依存層(media.py)がpj.Errorをこの例外に変換する。
    """


@dataclass(frozen=True)
class CallerInfo:
    """発信者情報"""

    from_number: str
    p_asserted_identity: Optional[str]
    to_number: str


class CallControl(Protocol):
    """pjsua2のCall操作を抽象化したインターフェース（テスト用フェイク差し替え可能）"""

    def answer(self) -> None: ...

    def hangup(self) -> None: ...

    @property
    def terminated(self) -> bool: ...

    async def wait_terminated(self, timeout: Optional[float]) -> bool: ...

    async def wait_confirmed(self, timeout: float) -> bool: ...

    async def wait_media_active(self, timeout: float) -> bool: ...

    async def play_wav(self, path: str) -> None: ...

    def start_recording(self, path: str) -> None: ...

    def stop_recording(self) -> None: ...

    async def wait_record_end(self, timeout: float) -> None: ...


@dataclass(frozen=True)
class SessionConfig:
    auto_block_enabled: bool
    voicemail_enabled: bool
    answer_delay_sec: float
    max_duration_sec: float
    greeting_wav: str
    beep_wav: str
    reject_wav: str
    recordings_dir: str


@dataclass(frozen=True)
class SessionDeps:
    check_spam: Callable[[CallerInfo], Awaitable[SpamVerdict]]
    transcribe: Callable[[str], Awaitable[tuple[Optional[str], Optional[str]]]]
    log: Callable[..., None]


class CallSession:
    """1着信の一生を管理する状態機械

    終端規則: controlの終端(DISCONNECTED)を検知したら以後のCall操作をしない。
    操作直前の競合で起きるCallOperationErrorは「CANCELとの正常な競合」として握りつぶす。
    """

    def __init__(
        self,
        control: CallControl,
        caller: CallerInfo,
        config: SessionConfig,
        deps: SessionDeps,
    ):
        self.control = control
        self.caller = caller
        self.config = config
        self.deps = deps
        self.t0 = time.monotonic()  # INVITE受信時刻（タイマーの起点）

    async def run(self) -> None:
        try:
            await self._run()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"着信処理でエラーが発生しました: from={self.caller.from_number}")

    async def _run(self) -> None:
        self._log("received")

        verdict = await self._race(self.deps.check_spam(self.caller))
        if verdict is TERMINATED:
            logger.info("スパム判定中に通話が終了しました")
            return

        logger.info(
            f"スパム判定結果: caller={self.caller.from_number}, "
            f"is_spam={verdict.is_spam}, action={verdict.action}, reason={verdict.reason}"
        )
        self._log("spam_detected" if verdict.is_spam else "legitimate", reason=verdict.reason)

        if verdict.is_spam and self.config.auto_block_enabled:
            await self._handle_spam(verdict)
            return

        if verdict.is_spam:
            # 設計§2.1: AUTO_BLOCK無効時は通話操作しないが、通常留守電のタイマーは動く
            logger.info("AUTO_BLOCK_ENABLED=falseのため通話操作をしません")

        if self.config.voicemail_enabled:
            await self._delayed_voicemail()
        else:
            logger.info(f"着信を無視します: {self.caller.from_number}")

    # ---- スパム側フロー ----

    async def _handle_spam(self, verdict: SpamVerdict) -> None:
        if verdict.action == "hangup":
            if not self._safe_answer():
                return
            # 既存挙動の維持: CONFIRMED(ACK受信)を待ってからBYEを送る
            await self.control.wait_confirmed(CONFIRM_TIMEOUT)
            self._safe_hangup()
            self._log("blocked", reason=verdict.reason)
        elif verdict.action == "announce":
            if not await self._answer_with_media():
                return
            try:
                if await self._race(self.control.play_wav(self.config.reject_wav)) is TERMINATED:
                    return
            except CallOperationError as e:
                # 正常な切断競合だけでなくplay_wavタイムアウト(通話は生存)でも
                # このexceptに入り得るため、無音のまま残さないよう必ずhangupする
                # (_safe_hangupは終端済みならno-opなので、正常競合ケースでも安全)
                logger.info(f"アナウンス再生中にエラーが発生しました（切断競合の可能性）: {e}")
                self._safe_hangup()
                return
            self._safe_hangup()
            self._log("blocked", reason=verdict.reason)
        elif verdict.action == "voicemail":
            await self._voicemail(reason=verdict.reason or "webhook: forced voicemail")

    # ---- 通常留守電フロー ----

    async def _delayed_voicemail(self) -> None:
        remaining = self.config.answer_delay_sec - (time.monotonic() - self.t0)
        if remaining > 0:
            if await self.control.wait_terminated(remaining):
                logger.info("応答待ち中に通話が終了しました（他端末が応答 or 発信者切断）")
                return
        await self._voicemail(reason="voicemail: no answer")

    # ---- 留守電本体 ----

    async def _voicemail(self, reason: str) -> None:
        if not await self._answer_with_media():
            return

        try:
            if await self._race(self.control.play_wav(self.config.greeting_wav)) is TERMINATED:
                return
            if await self._race(self.control.play_wav(self.config.beep_wav)) is TERMINATED:
                return
        except CallOperationError as e:
            # 正常な切断競合だけでなくplay_wavタイムアウト(通話は生存)でも
            # このexceptに入り得るため、無音のまま残さないよう必ずhangupする
            # (_safe_hangupは終端済みならno-opなので、正常競合ケースでも安全)
            logger.info(f"挨拶再生中にエラーが発生しました（切断競合の可能性）: {e}")
            self._safe_hangup()
            return

        try:
            part_path = new_recording_path(self.config.recordings_dir)
            self.control.start_recording(part_path)
        except (CallOperationError, OSError) as e:
            logger.error(f"録音の開始に失敗しました: {e}")
            self._safe_hangup()
            return

        cancelled = False
        try:
            # 相手切断・メディア喪失(hold等)・上限到達のいずれかまで録音
            await self.control.wait_record_end(self.config.max_duration_sec)
        except asyncio.CancelledError:
            cancelled = True  # シャットダウン。録音は確定して通知してから再送出する
        finally:
            try:
                self.control.stop_recording()
            except CallOperationError as e:
                logger.warning(f"録音停止時にエラー（切断との競合の可能性）: {e}")
        self._safe_hangup()

        # ここから先はCallに依存しない後処理
        try:
            final_path = finalize_recording(part_path)
            duration = await _wav_duration_sec_with_retry(final_path)
            logger.info(f"録音を保存しました: {final_path} ({duration:.1f}秒)")
        except Exception as e:
            # finalize/durationの失敗でvoicemail_recorded通知が消えないよう、
            # 生存しているパス（rename済みならfinal, 未renameならpart）で通知だけは必ず送る
            logger.error(f"録音の確定に失敗しました: {e}")
            final_candidate = locals().get("final_path")
            if final_candidate and os.path.exists(final_candidate):
                survivor_path = final_candidate
            else:
                survivor_path = part_path
            self._log(
                "voicemail_recorded",
                reason=reason,
                extra={
                    "duration_sec": 0,
                    "recording_path": survivor_path,
                    "transcription": None,
                    "transcription_error": f"finalize failed: {e}",
                },
            )
            if cancelled:
                raise asyncio.CancelledError()
            return

        if cancelled:
            text, error = None, "shutdown"
        elif duration > 0:
            try:
                text, error = await self.deps.transcribe(final_path)
            except asyncio.CancelledError:
                # シャットダウン。voicemail_recordedは送ってから再送出する
                cancelled = True
                text, error = None, "shutdown"
        else:
            # 0秒録音: 文字起こしAPIには送らない
            text, error = None, None

        self._log(
            "voicemail_recorded",
            reason=reason,
            extra={
                "duration_sec": duration,
                "recording_path": final_path,
                "transcription": text,
                "transcription_error": error,
            },
        )
        if cancelled:
            raise asyncio.CancelledError()

    # ---- ヘルパー ----

    async def _answer_with_media(self) -> bool:
        """応答してメディア確立を待つ。失敗時はhangupしてFalse"""
        if not self._safe_answer():
            return False
        if not await self.control.wait_media_active(MEDIA_ACTIVE_TIMEOUT):
            logger.warning("メディアが確立しませんでした")
            self._safe_hangup()
            return False
        return True

    def _safe_answer(self) -> bool:
        if self.control.terminated:
            return False
        try:
            self.control.answer()
            return True
        except CallOperationError as e:
            logger.info(f"応答時に切断と競合しました（正常なCANCEL競合）: {e}")
            return False

    def _safe_hangup(self) -> None:
        if self.control.terminated:
            return
        try:
            self.control.hangup()
        except CallOperationError as e:
            logger.info(f"切断時に競合しました（正常）: {e}")

    async def _race(self, coro):
        """coroを実行するが、通話が先に終端したらキャンセルしてTERMINATEDを返す

        同一tickで両方完了した場合は終端を優先する。
        """
        work = asyncio.ensure_future(coro)
        term = asyncio.ensure_future(self.control.wait_terminated(None))
        try:
            done, pending = await asyncio.wait({work, term}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # 外部からのキャンセル(CancelledError)がasyncio.waitの途中で飛んできた場合も、
            # work/termタスクがorphanしないよう必ず両方を回収する。
            for p in (work, term):
                if not p.done():
                    p.cancel()
            await asyncio.gather(work, term, return_exceptions=True)
        if term in done:
            if work in done and not work.cancelled():
                work.exception()  # 未取得例外の警告を防ぐ
            return TERMINATED
        return work.result()

    def _log(self, action: str, reason: Optional[str] = None, extra: Optional[dict] = None) -> None:
        self.deps.log(
            action,
            self.caller.from_number,
            self.caller.p_asserted_identity,
            self.caller.to_number,
            reason=reason,
            extra=extra,
        )
