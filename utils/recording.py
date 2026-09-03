import os
import uuid
import wave
from datetime import datetime, timezone


def new_recording_path(recordings_dir: str) -> str:
    """新しい録音ファイルのパス(*.part.wav)を生成する

    - 末尾は必ず.wav: pjsua2のAudioMediaRecorderは拡張子で録音形式を決めるため
    - CallerIDはファイル名に含めない（sanitize問題と衝突の回避。番号はMQTT側にある）
    """
    os.makedirs(recordings_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}_{uuid.uuid4().hex[:6]}.part.wav"
    return os.path.join(recordings_dir, name)


def finalize_recording(part_path: str) -> str:
    """録音完了した*.part.wavファイルを*.wavにrenameする"""
    final_path = part_path.replace(".part.wav", ".wav")
    os.rename(part_path, final_path)
    return final_path


def wav_duration_sec(path: str) -> float:
    """WAVの長さをフレーム数から算出する（壁時計は使わない）"""
    with wave.open(path, "rb") as w:
        return w.getnframes() / w.getframerate()


def validate_prompt_wav(path: str) -> None:
    """プロンプトWAVが8kHz mono 16bit PCMであることを検証する"""
    with wave.open(path, "rb") as w:
        actual = (w.getframerate(), w.getnchannels(), w.getsampwidth())
    if actual != (8000, 1, 2):
        raise ValueError(
            f"{path}: 8kHz mono 16bit PCM WAVが必要です "
            f"(rate={actual[0]}, channels={actual[1]}, sampwidth={actual[2]})"
        )
