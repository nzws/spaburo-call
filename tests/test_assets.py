from pathlib import Path

from utils.recording import validate_prompt_wav

ASSETS = Path(__file__).resolve().parent.parent / "assets"


def test_bundled_assets_are_valid_prompt_wavs():
    for name in ["beep.wav", "greeting.wav", "reject.wav", "announce_sales.wav"]:
        validate_prompt_wav(str(ASSETS / name))
