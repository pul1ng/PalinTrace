"""The existing bounded shell-fragment checks shared by evidence and recipes."""

from dataclasses import dataclass


class ShellUnsupported(ValueError):
    pass


@dataclass(frozen=True)
class ShellSegment:
    start: int
    end: int
    text: str
    pipeline: bool = False


def _flush(command: str, start: int, end: int, pipeline: bool = False) -> ShellSegment | None:
    raw = command[start:end]
    stripped = raw.strip()
    if not stripped:
        if raw:
            raise ShellUnsupported("empty command between shell operators")
        return None
    offset = start + raw.index(stripped)
    return ShellSegment(start=offset, end=offset + len(stripped), text=stripped, pipeline=pipeline)


def split_top_level(command: str) -> tuple[ShellSegment, ...]:
    """Split a small supported shell subset at top-level command-list operators.

    Quotes, escapes and simple pipelines are retained. Subshells, command
    substitution, backticks and here-documents are rejected rather than guessed.
    """
    segments = []
    start = 0
    quote = None
    escaped = False
    pipeline = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if char == "\\":
                escaped = True
            elif char == '"':
                quote = None
            elif char == "`" or (char == "$" and index + 1 < len(command)
                                 and command[index + 1] == "("):
                raise ShellUnsupported("command substitution is unsupported")
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if char in ("`", "(", ")"):
            raise ShellUnsupported("subshell, function or command substitution is unsupported")
        if char == "$" and index + 1 < len(command) and command[index + 1] == "(":
            raise ShellUnsupported("command substitution is unsupported")
        if command.startswith("<<", index):
            raise ShellUnsupported("here-documents are unsupported")
        if command.startswith("&&", index) or command.startswith("||", index):
            segment = _flush(command, start, index, pipeline)
            if segment is not None:
                segments.append(segment)
            index += 2
            start = index
            pipeline = False
            continue
        if char == "&" and not (index and command[index - 1] in "><"):
            if index + 1 < len(command) and command[index + 1] == ">":
                index += 1
                continue
            segment = _flush(command, start, index + 1, pipeline)
            if segment is not None:
                segments.append(segment)
            index += 1
            start = index
            pipeline = False
            continue
        if char == "|":
            pipeline = True
        if char in (";", "\n"):
            segment = _flush(command, start, index, pipeline)
            if segment is not None:
                segments.append(segment)
            index += 1
            start = index
            pipeline = False
            continue
        index += 1
    if quote is not None:
        raise ShellUnsupported("unterminated shell quote")
    if escaped:
        raise ShellUnsupported("trailing shell escape")
    segment = _flush(command, start, len(command), pipeline)
    if segment is not None:
        segments.append(segment)
    if not segments:
        raise ShellUnsupported("empty command")
    return tuple(segments)


def exact_segment(command: str, quote: str) -> str | None:
    try:
        segments = split_top_level(command)
    except ShellUnsupported:
        return None
    selected = quote.strip()
    if any(segment.text == selected for segment in segments):
        return selected
    return None


def exit_code_applies(command: str, quote: str) -> bool:
    """Only a whole simple call (including its trailing background &) owns its exit.

    Compound subcommands and pipelines require action-specific result evidence.
    This deliberately does not interpret shell control flow.
    """
    try:
        segments = split_top_level(command)
    except ShellUnsupported:
        return False
    if len(segments) != 1 or segments[0].text != quote.strip() or segments[0].pipeline:
        return False
    first = segments[0].text.split(maxsplit=1)[0]
    return first not in {"!", "if", "for", "while", "until", "case", "select", "function", "{", "time"}
