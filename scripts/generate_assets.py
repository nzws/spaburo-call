#!/usr/bin/env python3
"""同梱音声アセットを生成する（macOS前提: sayコマンドとafconvertを使用）

使い方: python3 scripts/generate_assets.py
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


def generate_beep(path: Path, freq: int = 1000, duration: float = 0.5, rate: int = 8000) -> None:
    """1kHzのビープ音を生成する"""
    frames = bytearray()
    n = int(rate * duration)
    for i in range(n):
        # 端でのクリック音を避けるため簡単なフェードイン/アウトを付ける
        env = min(1.0, i / (rate * 0.02), (n - i) / (rate * 0.02))
        sample = int(0.5 * env * 32767 * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<h", sample)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    print(f"生成しました: {path}")


def generate_speech(path: Path, text: str) -> None:
    """sayコマンドで日本語音声を生成し、8kHz mono 16bit PCMに変換する"""
    aiff = path.with_suffix(".aiff")
    try:
        subprocess.run(["say", "-v", "Kyoko", "-o", str(aiff), text], check=True)
        subprocess.run(
            [
                "afconvert", str(aiff), str(path),
                "-f", "WAVE", "-d", "LEI16@8000", "-c", "1",
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
