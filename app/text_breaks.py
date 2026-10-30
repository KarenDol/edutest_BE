"""Clean question text: real line breaks, one backslash per LaTeX command."""

import re

# A line-break escape is "\n" / "\r" not followed by a lowercase letter.
# LaTeX commands continue with a lowercase letter (\neq, \right, \newline).
_LITERAL_NL = re.compile(r"\\n(?![a-z])")
_LITERAL_CR = re.compile(r"\\r(?![a-z])")
# \\alpha or \\\\frac left by a second escape pass. One backslash is already correct.
_DOUBLED_COMMAND = re.compile(r"\\{2,}(?=[A-Za-z])")
# Empty braces we used to insert so a command would end before the next letter.
_EMPTY_COMMAND_GROUP = re.compile(r"(\\[A-Za-z]+)\{\}(?=\s)")


def restore_literal_newlines(text: str) -> str:
    if not text or "\\" not in text and "$" not in text:
        return text
    text = _DOUBLED_COMMAND.sub(lambda _match: "\\", text)
    text = _EMPTY_COMMAND_GROUP.sub(r"\1", text)
    if "\\n" in text or "\\r" in text:
        text = _LITERAL_CR.sub("", text)
        text = _LITERAL_NL.sub("\n", text)
    return text
