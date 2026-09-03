import httpx
import pytest

from utils.webhook import SpamVerdict, check_spam, parse_webhook_response


class TestParseWebhookResponse:
    def test_2xx_is_not_spam(self):
        v = parse_webhook_response(200, b"")
        assert v == SpamVerdict(False, "none", "Webhook: 200")

    def test_4xx_without_body_is_hangup(self):
        v = parse_webhook_response(403, b"")
        assert v.is_spam and v.action == "hangup"
        assert v.reason == "Webhook: 403"

    def test_4xx_with_action_json(self):
        v = parse_webhook_response(403, b'{"action": "voicemail", "reason": "telnavi 1.2"}')
        assert v == SpamVerdict(True, "voicemail", "telnavi 1.2")

    def test_4xx_with_announce(self):
        v = parse_webhook_response(403, b'{"action": "announce"}')
        assert v.action == "announce"
        assert v.reason == "Webhook: 403"  # reason省略時は従来形式

    def test_4xx_with_invalid_json_falls_back_to_hangup(self):
        assert parse_webhook_response(403, b"not json").action == "hangup"

    def test_4xx_with_unknown_action_falls_back_to_hangup(self):
        assert parse_webhook_response(403, b'{"action": "explode"}').action == "hangup"

    def test_4xx_with_non_string_action_falls_back_to_hangup(self):
        assert parse_webhook_response(403, b'{"action": 42}').action == "hangup"
        assert parse_webhook_response(403, b'{"action": null}').action == "hangup"

    def test_4xx_with_non_dict_json_falls_back_to_hangup(self):
        assert parse_webhook_response(403, b'["voicemail"]').action == "hangup"

    def test_non_string_reason_is_ignored(self):
        v = parse_webhook_response(403, b'{"action": "hangup", "reason": 5}')
        assert v.reason == "Webhook: 403"

    def test_5xx_is_fail_open(self):
        assert parse_webhook_response(500, b"") == SpamVerdict(False, "none", None)

    def test_3xx_is_fail_open(self):
        assert parse_webhook_response(301, b"") == SpamVerdict(False, "none", None)

    def test_429_is_spam(self):
        assert parse_webhook_response(429, b"").action == "hangup"


class TestCheckSpam:
    async def test_no_url_returns_not_spam(self):
        async with httpx.AsyncClient() as client:
            v = await check_spam(client, None, "0312345678", "0398765432", None)
        assert v == SpamVerdict(False, "none", None)

    async def test_sends_query_params_and_parses(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["params"] = dict(request.url.params)
            return httpx.Response(403, json={"action": "voicemail"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            v = await check_spam(client, "http://wh/check", "0312345678", "0398765432", "+81312345678")
        assert seen["params"] == {"from": "0312345678", "to": "0398765432", "pai": "+81312345678"}
        assert v.action == "voicemail"

    async def test_connection_error_is_fail_open(self):
        def handler(request):
            raise httpx.ConnectError("boom")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            v = await check_spam(client, "http://wh/check", "03", "03", None)
        assert v == SpamVerdict(False, "none", None)

    async def test_invalid_url_is_fail_open(self):
        async with httpx.AsyncClient() as client:
            v = await check_spam(client, "not a url", "03", "03", None)
        assert v == SpamVerdict(False, "none", None)
