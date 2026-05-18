import re
from html import unescape


_BLOCK_END = re.compile(r"</(p|li|tr|div|h[1-6])>", re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+\n|\n[ \t]+|[ \t]{2,}")


def html_to_plain(html: str) -> str:
    if not html:
        return ""
    s = unescape(html)
    s = _BLOCK_END.sub("\n", s)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = _TAG.sub("", s)
    s = _WS.sub(" ", s)
    lines = [ln.strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()
