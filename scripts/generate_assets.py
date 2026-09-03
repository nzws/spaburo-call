#!/usr/bin/env python3
"""同梱音声アセットを生成する（macOS前提: sayコマンドとffmpegを使用）

使い方: python3 scripts/generate_assets.py

電話回線(G.711)の上限は8kHzだが、聞き取りやすさは仕上げ方で大きく変わるため、
高レートでTTS生成 → soxrで高品質リサンプル → ラウドネス正規化 の順で
8kHz mono 16bit PCM に落とし込む。
"""

import math
import struct
import subprocess
import wave
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"

GREETING_TEXT = (
    "ただいま電話に出ることができません。"
    "発信音のあとに、お名前とご用件をお話しください。"
)
REJECT_TEXT = "この電話はお受けすることができません。"

# 電話向けにやや遅めの話速（sayの既定はおよそ200前後）
SPEECH_RATE = 160


def generate_beep(path: Path, freq: int = 1000, duration: float = 0.5, rate: int = 8000) -> None:
    """1kHzのビープ音を生成する"""
    frames = bytearray()
    n = int(rate * duration)
    for i in range(n):
        # 端でのクリック音を避けるため簡単なフェードイン/アウトを付ける
        env = min(1.0, i / (rate * 0.02), (n - i) / (rate * 0.02))
        sample = int(0.7 * env * 32767 * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<h", sample)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    print(f"生成しました: {path}")


def generate_speech(path: Path, text: str) -> None:
    """sayコマンドで日本語音声を生成し、正規化して8kHz mono 16bit PCMに変換する

    - say: 22.05kHz AIFFで高レート生成（直接8kHzにしない方がリサンプル品質が良い）
    - ffmpeg: 電話帯域に合わせたハイパスの後、ラウドネス正規化(-16 LUFS)で
      音量を電話向けに引き上げ、8kHzへリサンプル
    """
    aiff = path.with_suffix(".aiff")
    try:
        subprocess.run(
            ["say", "-v", "Kyoko", "-r", str(SPEECH_RATE), "-o", str(aiff), text],
            check=True,
        )
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(aiff),
                "-af", "highpass=f=100,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=8000",
                "-ac", "1", "-c:a", "pcm_s16le",
                str(path),
            ],
            check=True,
        )
    finally:
        aiff.unlink(missing_ok=True)
    print(f"生成しました: {path}")


if __name__ == "__main__":
    ASSETS.mkdir(exist_ok=True)
    generate_beep(ASSETS / "beep.wav")
    generate_speech(ASSETS / "greeting.wav", GREETING_TEXT)
    generate_speech(ASSETS / "reject.wav", REJECT_TEXT)
