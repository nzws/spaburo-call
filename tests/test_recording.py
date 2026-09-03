import re
import wave

import pytest

from utils.recording import (
    finalize_recording,
    new_recording_path,
    validate_prompt_wav,
    wav_duration_sec,
)


def write_wav(path, rate=8000, channels=1, width=2, seconds=2.0):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\x00" * (int(rate * seconds) * width * channels))


def test_new_recording_path_creates_dir_and_part_name(tmp_path):
    d = tmp_path / "rec"
    path = new_recording_path(str(d))
    assert d.is_dir()
    # 例: 20260903-142301_a1b2c3.part.wav
    # 末尾が.wavであること（pjsua2 Recorderの形式判定のため）、CallerIDを含まないこと
    assert re.fullmatch(r".*/\d{8}-\d{6}_[0-9a-f]{6}\.part\.wav", path)


def test_new_recording_path_is_unique(tmp_path):
    a = new_recording_path(str(tmp_path))
    b = new_recording_path(str(tmp_path))
    assert a != b


def test_finalize_recording_renames(tmp_path):
    part = tmp_path / "x.part.wav"
    part.write_bytes(b"data")
    final = finalize_recording(str(part))
    assert final == str(tmp_path / "x.wav")
    assert not part.exists()
    assert (tmp_path / "x.wav").read_bytes() == b"data"


def test_wav_duration_sec(tmp_path):
    p = tmp_path / "a.wav"
    write_wav(p, seconds=2.0)
    assert wav_duration_sec(str(p)) == pytest.approx(2.0)


def test_validate_prompt_wav_accepts_8k_mono_16bit(tmp_path):
    p = tmp_path / "ok.wav"
    write_wav(p)
    validate_prompt_wav(str(p))  # 例外なし


@pytest.mark.parametrize("kwargs", [{"rate": 44100}, {"channels": 2}, {"width": 1}])
def test_validate_prompt_wav_rejects_wrong_format(tmp_path, kwargs):
    p = tmp_path / "ng.wav"
    write_wav(p, **kwargs)
    with pytest.raises(ValueError):
        validate_prompt_wav(str(p))


def test_validate_prompt_wav_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        validate_prompt_wav(str(tmp_path / "none.wav"))
