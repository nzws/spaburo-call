import httpx
import pytest

from utils.webhook import SpamVerdict, check_spam, parse_webhook_response


class TestParseWebhookResponse:
    def test_2xx_is_not_spam(self):
        v = parse_webhook_response(200, b"")
        assert v == SpamVerdict(False, "none", "Webhook: 200")

    def test_400_without_body_is_hangup(self):
        v = parse_webhook_response(400, b"")
        assert v.is_spam and v.action == "hangup"
        assert v.reason == "Webhook: 400"

    def test_400_with_action_json(self):
        v = parse_webhook_response(400, b'{"action": "voicemail", "reason": "telnavi 1.2"}')
        assert v == SpamVerdict(True, "voicemail", "telnavi 1.2")

    def test_400_with_announce(self):
        v = parse_webhook_response(400, b'{"action": "announce"}')
        assert v.action == "announce"
        assert v.reason == "Webhook: 400"  # reason省略時は従来形式

    def test_400_with_invalid_json_falls_back_to_hangup(self):
        assert parse_webhook_response(400, b"not json").action == "hangup"

    def test_400_with_unknown_action_falls_back_to_hangup(self):
        assert parse_webhook_response(400, b'{"action": "explode"}').action == "hangup"

    def test_400_with_non_string_action_falls_back_to_hangup(self):
        assert parse_webhook_response(400, b'{"action": 42}').action == "hangup"
        assert parse_webhook_response(400, b'{"action": null}').action == "hangup"

    def test_400_with_non_dict_json_falls_back_to_hangup(self):
        assert parse_webhook_response(400, b'["voicemail"]').action == "hangup"

    def test_announce_with_message(self):
        v = parse_webhook_response(400, b'{"action": "announce", "message": "sales"}')
        assert v.action == "announce"
        assert v.message == "sales"

    def test_announce_without_message_has_none(self):
        v = parse_webhook_response(400, b'{"action": "announce"}')
        assert v.message is None

    def test_invalid_message_name_is_ignored(self):
        for bad in ['"../etc"', '"日本語"', '42', 'null', '""']:
            v = parse_webhook_response(
                400, f'{{"action": "announce", "message": {bad}}}'.encode()
            )
            assert v.message is None, bad

    def test_message_is_ignored_for_non_announce_actions(self):
        v = parse_webhook_response(400, b'{"action": "voicemail", "message": "sales"}')
        assert v.message is None

    def test_non_string_reason_is_ignored(self):
        v = parse_webhook_response(400, b'{"action": "hangup", "reason": 5}')
        assert v.reason == "Webhook: 400"

    def test_5xx_is_fail_open(self):
        assert parse_webhook_response(500, b"") == SpamVerdict(False, "none", None)

    def test_3xx_is_fail_open(self):
        assert parse_webhook_response(301, b"") == SpamVerdict(False, "none", None)

    def test_non_400_4xx_is_fail_open(self):
        for status in (401, 403, 404, 429):
            assert parse_webhook_response(status, b"") == SpamVerdict(False, "none", None)


class TestCheckSpam:
    async def test_no_url_returns_not_spam(self):
        async with httpx.AsyncClient() as client:
            v = await check_spam(client, None, "0312345678", "0398765432")
        assert v == SpamVerdict(False, "none", None)

    async def test_sends_query_params_and_parses(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["params"] = dict(request.url.params)
            return httpx.Response(400, json={"action": "voicemail"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            v = await check_spam(client, "http://wh/check", "0312345678", "0398765432")
        assert seen["params"] == {"from": "0312345678", "to": "0398765432"}
        assert v.action == "voicemail"

    async def test_existing_query_params_are_preserved(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["params"] = dict(request.url.params)
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            await check_spam(client, "http://wh/check?token=abc", "0312345678", "0398765432")
        assert seen["params"] == {
            "token": "abc",
            "from": "0312345678",
            "to": "0398765432",
        }

    async def test_connection_error_is_fail_open(self):
        def handler(request):
            raise httpx.ConnectError("boom")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            v = await check_spam(client, "http://wh/check", "03", "03")
        assert v == SpamVerdict(False, "none", None)

    async def test_invalid_url_is_fail_open(self):
        async with httpx.AsyncClient() as client:
            v = await check_spam(client, "not a url", "03", "03", None)
        assert v == SpamVerdict(False, "none", None)
