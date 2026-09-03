import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("Transcribe")

# 既定はGroqのOpenAI互換エンドポイント（envで任意のOpenAI互換APIに差し替え可能）
DEFAULT_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


async def transcribe(
    client: httpx.AsyncClient,
    api_key: Optional[str],
    model: str,
    wav_path: str,
    timeout: float = 60.0,
    url: str = DEFAULT_TRANSCRIPTION_URL,
) -> tuple[Optional[str], Optional[str]]:
    """録音WAVをOpenAI互換の文字起こしAPIで文字起こしする（既定: Groq Whisper）

    いかなる失敗も例外を投げず(None, エラー理由)で返す。
    呼び出し元(CallSession)はこの契約に依存して通知を必ず送る。

    Returns:
        (text, error): 成功時は(テキスト, None)、失敗時は(None, エラー理由)
    """
    if not api_key:
        logger.info("OPENAI_API_KEYが未設定のため文字起こしをスキップします")
        return None, "OPENAI_API_KEY not set"

    try:
        with open(wav_path, "rb") as f:
            wav_bytes = f.read()
    except OSError as e:
        return None, f"録音ファイルの読み込みに失敗: {e}"

    try:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            data={"model": model, "language": "ja", "response_format": "json"},
            files={"file": (os.path.basename(wav_path), wav_bytes, "audio/wav")},
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        return None, f"{type(e).__name__}: {e}"

    if response.status_code != 200:
        return None, f"Transcription API error: {response.status_code} {response.text[:200]}"

    try:
        data = response.json()
    except ValueError:
        return None, "Transcription API error: レスポンスがJSONではありません"
    text = data.get("text") if isinstance(data, dict) else None
    if not isinstance(text, str):
        return None, "Transcription API error: レスポンスにtextがありません"

    logger.info(f"文字起こしに成功しました: {len(text)}文字")
    return text, None
