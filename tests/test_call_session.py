import asyncio
import wave
from dataclasses import dataclass, field
from typing import Optional

import pytest

from call_session import (
    CallerInfo,
    CallOperationError,
    CallSession,
    SessionConfig,
    SessionDeps,
)
from utils.webhook import SpamVerdict


class FakeControl:
    """CallControlのフェイク。呼ばれた操作をopsに記録する"""

    def __init__(self):
        self.ops = []
        self._term = asyncio.Event()
        self.media_ok = True
        self.answer_raises = False
        self.record_seconds = 1.0  # 録音停止時に書き込むダミー音声の長さ
        self.play_gate: Optional[asyncio.Event] = None  # setすると再生がこれを待つ
        self.recording_path: Optional[str] = None

    def terminate(self):
        self._term.set()

    @property
    def terminated(self):
        return self._term.is_set()

    async def wait_terminated(self, timeout):
        try:
            await asyncio.wait_for(self._term.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_confirmed(self, timeout):
        self.ops.append("wait_confirmed")
        return not self.terminated

    def answer(self):
        if self.answer_raises:
            raise CallOperationError("call is disconnected")
        self.ops.append("answer")

    def hangup(self):
        self.ops.append("hangup")

    async def wait_media_active(self, timeout):
        self.ops.append("wait_media")
        return self.media_ok

    async def play_wav(self, path):
        self.ops.append(f"play:{path}")
        if self.play_gate is not None:
            await self.play_gate.wait()

    def start_recording(self, path):
        self.ops.append("record_start")
        self.recording_path = path
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            # 16bit monoなので1フレーム=2バイト
            w.writeframes(b"\x00\x00" * int(8000 * self.record_seconds))

    def stop_recording(self):
        self.ops.append("record_stop")

    async def wait_record_end(self, timeout):
        await self.wait_terminated(timeout)


@dataclass
class LogSpy:
    entries: list = field(default_factory=list)

    def __call__(self, action, from_number, p_asserted_identity, to_number, reason=None, extra=None):
        self.entries.append({"action": action, "reason": reason, "extra": extra})

    def actions(self):
        return [e["action"] for e in self.entries]


CALLER = CallerInfo(from_number="0312345678", p_asserted_identity=None, to_number="0398765432")


class TranscribeSpy:
    def __init__(self, result=("もしもし", None)):
        self.result = result
        self.calls = []

    async def __call__(self, path):
        self.calls.append(path)
        return self.result


def make_session(
    control,
    verdict=SpamVerdict(False, "none", None),
    auto_block=True,
    voicemail=False,
    delay=0.05,
    max_duration=0.2,
    tmp_path=None,
    transcriber=None,
):
    log = LogSpy()
    transcriber = transcriber or TranscribeSpy()

    async def fake_check(caller):
        return verdict

    config = SessionConfig(
        auto_block_enabled=auto_block,
        voicemail_enabled=voicemail,
        answer_delay_sec=delay,
        max_duration_sec=max_duration,
        greeting_wav="greeting.wav",
        beep_wav="beep.wav",
        reject_wav="reject.wav",
        recordings_dir=str(tmp_path),
    )
    deps = SessionDeps(check_spam=fake_check, transcribe=transcriber, log=log)
    return CallSession(control, CALLER, config, deps), log, transcriber


async def test_legitimate_call_without_voicemail_does_nothing(tmp_path):
    control = FakeControl()
    session, log, _ = make_session(control, tmp_path=tmp_path)
    await session.run()
    assert control.ops == []
    assert log.actions() == ["received", "legitimate"]


async def test_spam_hangup_waits_confirmed(tmp_path):
    control = FakeControl()
    session, log, _ = make_session(
        control, verdict=SpamVerdict(True, "hangup", "Webhook: 403"), tmp_path=tmp_path
    )
    await session.run()
    # 既存挙動: 200 OK → CONFIRMED(ACK)を待つ → BYE
    assert control.ops == ["answer", "wait_confirmed", "hangup"]
    assert log.actions() == ["received", "spam_detected", "blocked"]


async def test_spam_with_auto_block_disabled_only_logs(tmp_path):
    control = FakeControl()
    session, log, _ = make_session(
        control,
        verdict=SpamVerdict(True, "voicemail", None),
        auto_block=False,
        tmp_path=tmp_path,
    )
    await session.run()
    assert control.ops == []
    assert log.actions() == ["received", "spam_detected"]


async def test_spam_with_auto_block_disabled_still_runs_voicemail_timer(tmp_path):
    """設計§2.1: AUTO_BLOCK=falseでも通常留守電のタイマーは動作する"""
    control = FakeControl()
    session, log, _ = make_session(
        control,
        verdict=SpamVerdict(True, "hangup", None),
        auto_block=False,
        voicemail=True,
        delay=0.05,
        tmp_path=tmp_path,
    )
    await session.run()
    assert "answer" in control.ops
    assert "record_start" in control.ops
    assert log.actions() == ["received", "spam_detected", "voicemail_recorded"]


async def test_spam_announce_plays_reject_then_hangs_up(tmp_path):
    control = FakeControl()
    session, log, _ = make_session(
        control, verdict=SpamVerdict(True, "announce", None), tmp_path=tmp_path
    )
    await session.run()
    assert control.ops == ["answer", "wait_media", "play:reject.wav", "hangup"]
    assert log.actions() == ["received", "spam_detected", "blocked"]


async def test_forced_voicemail_records_and_notifies(tmp_path):
    control = FakeControl()
    session, log, _ = make_session(
        control, verdict=SpamVerdict(True, "voicemail", None), tmp_path=tmp_path
    )
    await session.run()
    assert control.ops == [
        "answer",
        "wait_media",
        "play:greeting.wav",
        "play:beep.wav",
        "record_start",
        "record_stop",
        "hangup",
    ]
    assert log.actions() == ["received", "spam_detected", "voicemail_recorded"]
    extra = log.entries[-1]["extra"]
    assert extra["transcription"] == "もしもし"
    assert extra["transcription_error"] is None
    assert extra["duration_sec"] == pytest.approx(1.0)
    assert extra["recording_path"].endswith(".wav")
    assert ".part" not in extra["recording_path"]


async def test_normal_voicemail_answers_after_delay(tmp_path):
    control = FakeControl()
    session, log, _ = make_session(control, voicemail=True, delay=0.05, tmp_path=tmp_path)
    await session.run()
    assert "answer" in control.ops
    assert log.actions() == ["received", "legitimate", "voicemail_recorded"]


async def test_normal_voicemail_aborts_if_terminated_during_delay(tmp_path):
    control = FakeControl()
    session, log, _ = make_session(control, voicemail=True, delay=5.0, tmp_path=tmp_path)
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.05)
    control.terminate()  # 他端末が応答した(CANCEL相当)
    await asyncio.wait_for(task, 1.0)
    assert "answer" not in control.ops
    assert log.actions() == ["received", "legitimate"]


async def test_terminated_during_greeting_stops_flow(tmp_path):
    """挨拶再生中の切断: 録音に進まず、通知も送らない"""
    control = FakeControl()
    control.play_gate = asyncio.Event()  # 再生をブロックしたままにする
    session, log, _ = make_session(
        control, verdict=SpamVerdict(True, "voicemail", None), tmp_path=tmp_path
    )
    task = asyncio.create_task(session.run())
    await asyncio.sleep(0.05)
    control.terminate()
    await asyncio.wait_for(task, 1.0)
    assert "record_start" not in control.ops
    assert "voicemail_recorded" not in log.actions()


async def test_media_timeout_hangs_up(tmp_path):
    control = FakeControl()
    control.media_ok = False
    session, log, _ = make_session(
        control, verdict=SpamVerdict(True, "voicemail", None), tmp_path=tmp_path
    )
    await session.run()
    assert control.ops == ["answer", "wait_media", "hangup"]
    assert "voicemail_recorded" not in log.actions()


async def test_answer_race_with_disconnect_is_swallowed(tmp_path):
    control = FakeControl()
    control.answer_raises = True  # answer()の瞬間に切断済み(pj.Error相当)
    session, log, _ = make_session(
        control, verdict=SpamVerdict(True, "hangup", None), tmp_path=tmp_path
    )
    await session.run()  # 例外が漏れないこと
    assert "voicemail_recorded" not in log.actions()


async def test_transcribe_failure_still_notifies(tmp_path):
    control = FakeControl()
    session, log, _ = make_session(
        control,
        verdict=SpamVerdict(True, "voicemail", None),
        tmp_path=tmp_path,
        transcriber=TranscribeSpy(result=(None, "Groq API error: 429")),
    )
    await session.run()
    extra = log.entries[-1]["extra"]
    assert extra["transcription"] is None
    assert extra["transcription_error"] == "Groq API error: 429"


async def test_zero_length_recording_skips_transcribe_but_notifies(tmp_path):
    """設計§5.1: 0秒録音でも通知は送る。文字起こしは呼ばない"""
    control = FakeControl()
    control.record_seconds = 0.0
    transcriber = TranscribeSpy()
    session, log, _ = make_session(
        control,
        verdict=SpamVerdict(True, "voicemail", None),
        tmp_path=tmp_path,
        transcriber=transcriber,
    )
    await session.run()
    assert transcriber.calls == []  # Groqを呼ばない
    extra = log.entries[-1]["extra"]
    assert extra["transcription"] is None
    assert extra["duration_sec"] == 0.0


async def test_cancel_during_recording_finalizes_and_notifies(tmp_path):
    """設計§4.1: シャットダウン(キャンセル)時も録音を確定して通知する"""
    control = FakeControl()
    transcriber = TranscribeSpy()
    session, log, _ = make_session(
        control,
        verdict=SpamVerdict(True, "voicemail", None),
        max_duration=30.0,
        tmp_path=tmp_path,
        transcriber=transcriber,
    )
    task = asyncio.create_task(session.run())
    while "record_start" not in control.ops:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # 録音は確定され、文字起こしはスキップ、通知は送られる
    assert "record_stop" in control.ops
    assert "voicemail_recorded" in log.actions()
    assert transcriber.calls == []
    extra = log.entries[-1]["extra"]
    assert extra["transcription_error"] == "shutdown"
