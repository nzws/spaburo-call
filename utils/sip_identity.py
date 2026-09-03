import re
from typing import Optional

# 発信者番号が取得できなかった場合の値
UNKNOWN_NUMBER = "unknown"

# 電話番号として妥当な形（E.164の先頭+を許容）
_NUMBER_RE = re.compile(r"^\+?[0-9]+$")

# 電話番号中の視覚的区切り（RFC 3966のvisual-separator相当）
_VISUAL_SEPARATORS = "-.()"


def get_sip_headers(whole_msg: str, name: str) -> list[str]:
    """SIPメッセージから指定ヘッダの値を全出現分取得する（折り返し展開済み）"""
    target = name.lower()
    unfolded: list[str] = []
    for raw in whole_msg.split("\n"):
        line = raw[:-1] if raw.endswith("\r") else raw
        if line == "":
            # ヘッダとボディの境界。ボディ側は走査しない
            break
        if line[0] in " \t" and unfolded:
            unfolded[-1] = f"{unfolded[-1]} {line.strip()}"
        else:
            unfolded.append(line)

    values: list[str] = []
    for line in unfolded:
        header_name, sep, value = line.partition(":")
        if sep and header_name.strip().lower() == target:
            values.append(value.strip())
    return values


def split_addr_list(value: str) -> list[str]:
    """カンマ区切りのname-addrリストを分割する"""
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    escaped = False
    angle_depth = 0
    for ch in value:
        if escaped:
            current.append(ch)
            escaped = False
            continue
        if in_quotes and ch == "\\":
            current.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
        elif not in_quotes and ch == "<":
            angle_depth += 1
        elif not in_quotes and ch == ">":
            angle_depth = max(0, angle_depth - 1)
        elif ch == "," and not in_quotes and angle_depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def extract_number(uri: str) -> Optional[str]:
    """SIP/tel URIから電話番号を抽出し正規化する（番号として妥当でなければNone）"""
    token = uri.strip()
    start = token.find("<")
    if start != -1:
        end = token.find(">", start)
        addr = token[start + 1 : end] if end != -1 else token[start + 1 :]
    else:
        addr = token.split(";", 1)[0]
    addr = addr.strip()

    scheme, sep, rest = addr.partition(":")
    if not sep:
        return None
    scheme = scheme.strip().lower()
    if scheme in ("sip", "sips"):
        rest = re.split(r"[;?]", rest, maxsplit=1)[0]
        user, at, _host = rest.partition("@")
        if not at:
            return None
        raw = user
    elif scheme == "tel":
        raw = re.split(r"[;?]", rest, maxsplit=1)[0]
    else:
        return None

    number = "".join(c for c in raw if c not in _VISUAL_SEPARATORS and not c.isspace())
    return number if _NUMBER_RE.fullmatch(number) else None


def resolve_caller_number(whole_msg: str, remote_uri: str) -> str:
    """発信者番号を解決する（PAI優先、From（remoteUri）にフォールバック）"""
    for value in get_sip_headers(whole_msg, "P-Asserted-Identity"):
        for entry in split_addr_list(value):
            number = extract_number(entry)
            if number:
                return number
    return extract_number(remote_uri) or UNKNOWN_NUMBER
