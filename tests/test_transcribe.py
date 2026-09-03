import httpx

from utils.transcribe import GROQ_TRANSCRIPTION_URL, transcribe

MODEL = "whisper-large-v3-turbo"


def make_wav(tmp_path):
    p = tmp_path / "v.wav"
    p.write_bytes(b"RIFF....WAVE")  # 中身はAPIに渡すだけなのでダミーで良い
    return str(p)


async def test_no_api_key_skips(tmp_path):
    async with httpx.AsyncClient() as client:
        text, err = await transcribe(client, None, MODEL, make_wav(tmp_path))
    assert text is None
    assert err == "GROQ_API_KEY not set"


async def test_success(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"text": "こんにちは"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        text, err = await transcribe(client, "gsk_test", MODEL, make_wav(tmp_path))
    assert (text, err) == ("こんにちは", None)
    assert seen["auth"] == "Bearer gsk_test"
    assert seen["url"] == GROQ_TRANSCRIPTION_URL


async def test_api_error_returns_error(tmp_path):
    transport = httpx.MockTransport(lambda r: httpx.Response(429, text="rate limited"))
    async with httpx.AsyncClient(transport=transport) as client:
        text, err = await transcribe(client, "gsk_test", MODEL, make_wav(tmp_path))
    assert text is None
    assert "429" in err


async def test_network_error_returns_error(tmp_path):
    def handler(request):
        raise httpx.ConnectError("boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        text, err = await transcribe(client, "gsk_test", MODEL, make_wav(tmp_path))
    assert text is None
    assert "ConnectError" in err


async def test_missing_file_returns_error(tmp_path):
    async with httpx.AsyncClient() as client:
        text, err = await transcribe(client, "gsk_test", MODEL, str(tmp_path / "no.wav"))
    assert text is None
    assert err is not None


async def test_200_with_invalid_json_returns_error(tmp_path):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, text="not json"))
    async with httpx.AsyncClient(transport=transport) as client:
        text, err = await transcribe(client, "gsk_test", MODEL, make_wav(tmp_path))
    assert text is None
    assert err is not None


async def test_200_with_missing_or_non_string_text_returns_error(tmp_path):
    for payload in [{}, {"text": 42}, [1, 2]]:
        transport = httpx.MockTransport(lambda r, p=payload: httpx.Response(200, json=p))
        async with httpx.AsyncClient(transport=transport) as client:
            text, err = await transcribe(client, "gsk_test", MODEL, make_wav(tmp_path))
        assert text is None
        assert err is not None
