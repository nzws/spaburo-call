from utils.sip_identity import (
    extract_number,
    get_sip_headers,
    resolve_caller_number,
    split_addr_list,
)

# NTT RT-500MI からの実際のINVITE（PAIが折り返し＋sip/telのカンマ区切り）
RT500MI_INVITE = "\r\n".join(
    [
        "INVITE sip:3@192.168.1.10:5060 SIP/2.0",
        "Via: SIP/2.0/UDP 192.168.1.1:5060;branch=z9hG4bK1234",
        "From: <sip:07011112222@ntt-west.ne.jp>;tag=abcd1234",
        "To: <tel:0399998888;phone-context=ntt-west.ne.jp>",
        "Call-ID: 1234567890@192.168.1.1",
        "CSeq: 1 INVITE",
        "P-Called-Party-ID: <sip:0399998888@ntt-west.ne.jp>",
        "P-Asserted-Identity:",
        ' "07011112222"<sip:07011112222@ntt-west.ne.jp>,',
        ' "07011112222"<tel:07011112222;phone-context=ntt-west.ne.jp>',
        "Content-Type: application/sdp",
        "Content-Length: 0",
        "",
        "",
    ]
)


def make_msg(*headers: str, body: str = "") -> str:
    lines = ["INVITE sip:3@192.168.1.10:5060 SIP/2.0", *headers, "", body]
    return "\r\n".join(lines)


class TestResolveCallerNumber:
    def test_rt500mi_folded_pai(self):
        assert (
            resolve_caller_number(RT500MI_INVITE, "sip:07011112222@ntt-west.ne.jp")
            == "07011112222"
        )

    def test_tel_only_pai(self):
        msg = make_msg("P-Asserted-Identity: <tel:07033334444;phone-context=ntt-west.ne.jp>")
        assert resolve_caller_number(msg, "sip:anonymous@anonymous.invalid") == "07033334444"

    def test_multiple_pai_header_lines_uses_first_usable(self):
        msg = make_msg(
            "P-Asserted-Identity: <sip:07055556666@ntt-west.ne.jp>",
            "P-Asserted-Identity: <tel:07077778888>",
        )
        assert resolve_caller_number(msg, "sip:0312345678@example.com") == "07055556666"

    def test_without_pai_falls_back_to_from(self):
        msg = make_msg("From: <sip:0312345678@example.com>;tag=1")
        assert resolve_caller_number(msg, "sip:0312345678@example.com") == "0312345678"

    def test_anonymous_pai_falls_back_to_from(self):
        msg = make_msg("P-Asserted-Identity: <sip:anonymous@anonymous.invalid>")
        assert resolve_caller_number(msg, "<sip:0312345678@example.com>") == "0312345678"

    def test_all_anonymous_is_unknown(self):
        msg = make_msg("P-Asserted-Identity: <sip:anonymous@anonymous.invalid>")
        assert resolve_caller_number(msg, "sip:anonymous@anonymous.invalid") == "unknown"

    def test_e164_keeps_plus(self):
        msg = make_msg('P-Asserted-Identity: "caller"<sip:+819012345678@example.com>')
        assert resolve_caller_number(msg, "sip:anonymous@anonymous.invalid") == "+819012345678"


class TestExtractNumber:
    def test_strips_visual_separators(self):
        assert extract_number("sip:03-1234-5678@x") == "0312345678"

    def test_ignores_uri_params(self):
        assert extract_number("<sip:0312345678@example.com;user=phone>") == "0312345678"

    def test_no_user_part_is_none(self):
        assert extract_number("sip:example.com") is None

    def test_unknown_scheme_is_none(self):
        assert extract_number("<http:0312345678@example.com>") is None


class TestSplitAddrList:
    def test_does_not_split_inside_quoted_display_name(self):
        parts = split_addr_list('"Aida, Yuzuki"<sip:03@x>, <tel:06>')
        assert parts == ['"Aida, Yuzuki"<sip:03@x>', "<tel:06>"]


class TestGetSipHeaders:
    def test_unfolds_continuation_lines_case_insensitively(self):
        values = get_sip_headers(RT500MI_INVITE, "p-asserted-identity")
        assert values == [
            '"07011112222"<sip:07011112222@ntt-west.ne.jp>, '
            '"07011112222"<tel:07011112222;phone-context=ntt-west.ne.jp>'
        ]

    def test_accepts_lf_only_line_endings(self):
        msg = RT500MI_INVITE.replace("\r\n", "\n")
        assert len(get_sip_headers(msg, "P-Asserted-Identity")) == 1

    def test_does_not_read_body(self):
        msg = make_msg(
            "Content-Type: application/sdp",
            body="P-Asserted-Identity: <sip:09099998888@evil.example>",
        )
        assert get_sip_headers(msg, "P-Asserted-Identity") == []
