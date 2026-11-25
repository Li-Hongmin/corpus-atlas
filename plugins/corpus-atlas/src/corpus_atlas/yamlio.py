"""YAML input without a hard dependency.

``load_yaml`` uses PyYAML (``pip install 'corpus-atlas[yaml]'``) when importable,
otherwise a built-in parser that covers the block-style YAML used for record,
preference, and configuration files: nested mappings and sequences (including
compact ``- key: value`` items), single/double-quoted scalars, flow ``[..]`` /
``{..}`` collections, ``|`` and ``>`` block scalars with chomping indicators,
multi-line plain scalars, comments, and null/bool/int/float resolution.

Anchors, aliases, tags, complex ``?`` keys, and multiple documents raise
:class:`YAMLError` with the offending line number. Timestamps are kept as
strings in both back ends so results stay JSON-serialisable.
"""
from __future__ import annotations

import os
import re
from typing import Any


class YAMLError(ValueError):
    pass


def _pyyaml():
    if os.environ.get("CORPUS_ATLAS_YAML", "").lower() == "builtin":
        return None
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    return yaml


def backend() -> str:
    return "pyyaml" if _pyyaml() else "builtin"


def load_yaml(text: str) -> Any:
    yaml = _pyyaml()
    if yaml is None:
        return parse_yaml(text)

    class Loader(yaml.SafeLoader):  # keep dates as strings
        pass

    Loader.yaml_implicit_resolvers = {
        key: [(tag, regex) for tag, regex in values if tag != "tag:yaml.org,2002:timestamp"]
        for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    try:
        return yaml.load(text, Loader=Loader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as error:
        raise YAMLError(str(error)) from error


# ---------------------------------------------------------------- scalars
_INT = re.compile(r"[-+]?(?:0|[1-9][0-9_]*)$")
_HEX = re.compile(r"0x[0-9a-fA-F]+$")
_OCT = re.compile(r"0o[0-7]+$")
_FLOAT = re.compile(r"[-+]?(?:\.[0-9]+|[0-9][0-9_]*(?:\.[0-9_]*)?)(?:[eE][-+]?[0-9]+)?$")
_ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r",
            "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0"}


def resolve_plain(text: str) -> Any:
    value = text.strip()
    if value in ("", "~", "null", "Null", "NULL"):
        return None
    if value in ("true", "True", "TRUE"):
        return True
    if value in ("false", "False", "FALSE"):
        return False
    if _INT.match(value):
        return int(value.replace("_", ""))
    if _HEX.match(value):
        return int(value, 16)
    if _OCT.match(value):
        return int(value[2:], 8)
    if value.lower() in (".inf", "+.inf"):
        return float("inf")
    if value.lower() == "-.inf":
        return float("-inf")
    if value.lower() == ".nan":
        return float("nan")
    if _FLOAT.match(value) and any(ch.isdigit() for ch in value):
        return float(value.replace("_", ""))
    return value


def _fold_quoted(body: str) -> str:
    """Line folding inside multi-line quoted scalars."""
    if "\n" not in body:
        return body
    lines = [line.strip() for line in body.split("\n")]
    out = lines[0]
    blank = False
    for line in lines[1:]:
        if not line:
            out += "\n"
            blank = True
            continue
        out += line if blank or out.endswith("\n") else " " + line
        blank = False
    return out


def _double_quoted(body: str, line: int) -> str:
    out, index = [], 0
    body = _fold_quoted(body)
    while index < len(body):
        ch = body[index]
        if ch != "\\":
            out.append(ch)
            index += 1
            continue
        index += 1
        if index >= len(body):
            raise YAMLError(f"line {line}: dangling backslash in double-quoted string")
        code = body[index]
        if code in _ESCAPES:
            out.append(_ESCAPES[code])
            index += 1
        elif code in "xuU":
            width = {"x": 2, "u": 4, "U": 8}[code]
            digits = body[index + 1:index + 1 + width]
            if len(digits) != width or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                raise YAMLError(f"line {line}: bad \\{code} escape")
            out.append(chr(int(digits, 16)))
            index += 1 + width
        else:
            raise YAMLError(f"line {line}: unknown escape \\{code}")
    return "".join(out)


def _scan_quoted(text: str, start: int, line: int) -> tuple[str, int]:
    """Parse a quoted scalar beginning at text[start]; return (value, index after closing quote)."""
    quote = text[start]
    index = start + 1
    while index < len(text):
        ch = text[index]
        if quote == "'" and ch == "'":
            if text[index + 1:index + 2] == "'":
                index += 2
                continue
            return _fold_quoted(text[start + 1:index]).replace("''", "'"), index + 1
        if quote == '"' and ch == "\\":
            index += 2
            continue
        if quote == '"' and ch == '"':
            return _double_quoted(text[start + 1:index], line), index + 1
        index += 1
    raise _Unterminated(f"line {line}: unterminated quoted string")


class _Unterminated(YAMLError):
    pass


def strip_comment(text: str) -> str:
    quote = None
    index = 0
    while index < len(text):
        ch = text[index]
        if quote:
            if ch == "\\" and quote == '"':
                index += 2
                continue
            if ch == quote:
                if quote == "'" and text[index + 1:index + 2] == "'":
                    index += 2
                    continue
                quote = None
        elif ch in "'\"" and (index == 0 or text[index - 1] in " \t[{,:-?"):
            quote = ch
        elif ch == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index].rstrip()
        index += 1
    return text.rstrip()


def split_key(text: str) -> tuple[str, Any, str] | None:
    """Split ``key: rest`` outside quotes/brackets. Returns (raw_key, key, rest) or None."""
    if text.startswith(("[", "{")):
        return None
    if text[:1] in "'\"":
        try:
            key, end = _scan_quoted(text, 0, 0)
        except YAMLError:
            return None
        rest = text[end:].lstrip()
        if rest.startswith(":") and (len(rest) == 1 or rest[1] in " \t"):
            return text[:end], key, rest[1:].strip()
        return None
    match = re.search(r":(?:[ \t]|$)", text)
    if not match:
        return None
    raw = text[:match.start()].rstrip()
    if not raw:
        return None
    return raw, resolve_plain(raw) if not raw.startswith(("&", "*", "!")) else raw, text[match.end():].strip()


# ---------------------------------------------------------------- flow collections
class _Flow:
    def __init__(self, text: str, line: int):
        self.text, self.pos, self.line = text, 0, line

    def ws(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in " \t\n":
            self.pos += 1

    def peek(self) -> str:
        self.ws()
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def value(self, in_map: bool = False) -> Any:
        ch = self.peek()
        if not ch:
            raise _Unterminated(f"line {self.line}: unterminated flow collection")
        if ch == "[":
            self.pos += 1
            items: list[Any] = []
            while True:
                if self.peek() == "]":
                    self.pos += 1
                    return items
                items.append(self.value())
                nxt = self.peek()
                if nxt == ",":
                    self.pos += 1
                elif nxt != "]":
                    if not nxt:
                        raise _Unterminated(f"line {self.line}: unterminated flow sequence")
                    raise YAMLError(f"line {self.line}: expected ',' or ']' in flow sequence")
        if ch == "{":
            self.pos += 1
            mapping: dict[Any, Any] = {}
            while True:
                if self.peek() == "}":
                    self.pos += 1
                    return mapping
                key = self.value(in_map=True)
                value = None
                if self.peek() == ":":
                    self.pos += 1
                    if self.peek() in (",", "}"):
                        value = None
                    else:
                        value = self.value()
                mapping[key] = value
                nxt = self.peek()
                if nxt == ",":
                    self.pos += 1
                elif nxt != "}":
                    if not nxt:
                        raise _Unterminated(f"line {self.line}: unterminated flow mapping")
                    raise YAMLError(f"line {self.line}: expected ',' or '}}' in flow mapping")
        if ch in "'\"":
            value, self.pos = _scan_quoted(self.text, self.pos, self.line)
            return value
        if ch in "&*!":
            raise YAMLError(f"line {self.line}: anchors, aliases and tags are not supported by the built-in YAML parser")
        start = self.pos
        while self.pos < len(self.text):
            c = self.text[self.pos]
            if c in ",]}" or (c == ":" and (self.pos + 1 >= len(self.text) or self.text[self.pos + 1] in " \t,]}\n")):
                break
            self.pos += 1
        return resolve_plain(self.text[start:self.pos].replace("\n", " "))


# ---------------------------------------------------------------- block parser
class _Parser:
    def __init__(self, text: str):
        self.lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if self.lines and self.lines[-1] == "":
            self.lines.pop()  # artifact of the final newline
        self.index = 0
        self.started = False

    def fail(self, message: str, line: int | None = None) -> YAMLError:
        return YAMLError(f"line {line if line is not None else self.index + 1}: {message}")

    def peek(self) -> tuple[int, str, int] | None:
        while self.index < len(self.lines):
            raw = self.lines[self.index]
            content = strip_comment(raw)
            stripped = content.strip()
            if not stripped:
                self.index += 1
                continue
            indent = len(content) - len(content.lstrip(" "))
            if content[indent:indent + 1] == "\t":
                raise self.fail("tabs are not allowed for indentation")
            if indent == 0 and stripped in ("---", "...") or (indent == 0 and stripped.startswith("--- ")):
                if stripped == "..." or self.started:
                    if stripped == "...":
                        self.index += 1
                        if self.peek() is not None:
                            raise self.fail("multiple YAML documents are not supported")
                        return None
                    raise self.fail("multiple YAML documents are not supported")
                self.started = True
                if stripped.startswith("--- "):
                    self.lines[self.index] = "    " + stripped[4:]
                    continue
                self.index += 1
                continue
            if indent == 0 and stripped.startswith("%"):
                raise self.fail("YAML directives are not supported")
            self.started = True
            return indent, stripped, self.index + 1
        return None

    def document(self) -> Any:
        head = self.peek()
        if head is None:
            return None
        value = self.node(head[0])
        if self.peek() is not None:
            raise self.fail("unexpected content (bad indentation or a second document)")
        return value

    def node(self, indent: int) -> Any:
        head = self.peek()
        assert head is not None
        _, content, line = head
        if content == "-" or content.startswith("- "):
            return self.sequence(indent)
        if content.startswith("? "):
            raise self.fail("complex mapping keys ('? ') are not supported", line)
        if split_key(content) is not None:
            return self.mapping(indent)
        self.index += 1
        return self.inline(content, indent, line)

    def mapping(self, indent: int) -> dict[Any, Any]:
        result: dict[Any, Any] = {}
        while (head := self.peek()) is not None:
            ind, content, line = head
            if ind < indent:
                break
            if ind > indent:
                raise self.fail("bad indentation in mapping", line)
            if content == "-" or content.startswith("- "):
                break
            if content.startswith("? "):
                raise self.fail("complex mapping keys ('? ') are not supported", line)
            parts = split_key(content)
            if parts is None:
                raise self.fail(f"expected 'key: value', got {content[:40]!r}", line)
            raw, key, rest = parts
            if raw.startswith(("&", "*", "!")) or rest.startswith(("&", "*", "!")):
                raise self.fail("anchors, aliases and tags are not supported by the built-in YAML parser", line)
            self.index += 1
            result[key] = self.value(rest, indent, line, in_mapping=True)
        return result

    def sequence(self, indent: int) -> list[Any]:
        result: list[Any] = []
        while (head := self.peek()) is not None:
            ind, content, line = head
            if ind < indent or not (content == "-" or content.startswith("- ")):
                if ind > indent:
                    raise self.fail("bad indentation in sequence", line)
                break
            if ind > indent:
                raise self.fail("bad indentation in sequence", line)
            rest = content[1:]
            body = rest.lstrip(" ")
            column = ind + 1 + (len(rest) - len(body))
            if not body:
                self.index += 1
                nxt = self.peek()
                result.append(self.node(nxt[0]) if nxt and nxt[0] > ind else None)
                continue
            if body.startswith(("&", "*", "!")):
                raise self.fail("anchors, aliases and tags are not supported by the built-in YAML parser", line)
            if body == "-" or body.startswith("- ") or (split_key(body) is not None and not body.startswith(("|", ">"))):
                # compact nested node: re-read the remainder as if it started at `column`
                self.lines[self.index] = " " * column + body
                result.append(self.node(column))
                continue
            self.index += 1
            result.append(self.value(body, ind, line, in_mapping=False))
        return result

    def value(self, rest: str, indent: int, line: int, in_mapping: bool) -> Any:
        if rest.startswith(("&", "*", "!")):
            raise self.fail("anchors, aliases and tags are not supported by the built-in YAML parser", line)
        if rest[:1] in ("|", ">"):
            return self.block_scalar(rest, indent, line)
        if rest == "":
            nxt = self.peek()
            if nxt is None:
                return None
            ind, content, _ = nxt
            if ind > indent:
                return self.node(ind)
            if in_mapping and ind == indent and (content == "-" or content.startswith("- ")):
                return self.sequence(ind)
            return None
        return self.inline(rest, indent, line)

    def _continue(self, text: str, indent: int) -> str | None:
        """Append the next raw line (for multi-line quoted/flow scalars)."""
        if self.index >= len(self.lines):
            return None
        raw = self.lines[self.index]
        self.index += 1
        return text + "\n" + raw

    def inline(self, text: str, indent: int, line: int) -> Any:
        if text[:1] in "'\"":
            while True:
                try:
                    value, end = _scan_quoted(text, 0, line)
                    break
                except _Unterminated:
                    raw_more = self._continue(text, indent)
                    if raw_more is None:
                        raise
                    text = raw_more
            tail = strip_comment(text[end:]).strip()
            if tail:
                raise self.fail(f"unexpected text after quoted scalar: {tail[:30]!r}", line)
            return value
        if text[:1] in "[{":
            while True:
                try:
                    parser = _Flow(text, line)
                    value = parser.value()
                    break
                except _Unterminated:
                    if self.index >= len(self.lines):
                        raise
                    text = text + "\n" + strip_comment(self.lines[self.index])
                    self.index += 1
            tail = text[parser.pos:].strip()
            if tail:
                raise self.fail(f"unexpected text after flow collection: {tail[:30]!r}", line)
            return value
        # multi-line plain scalar: more-indented continuation lines fold into one string
        parts = [text]
        while (nxt := self.peek()) is not None and nxt[0] > indent:
            if split_key(nxt[1]) is not None or nxt[1].startswith("- "):
                raise self.fail("mapping/sequence not allowed here (check indentation)", nxt[2])
            parts.append(nxt[1])
            self.index += 1
        if len(parts) > 1:
            return " ".join(parts)
        return resolve_plain(text)

    def block_scalar(self, header: str, indent: int, line: int) -> str:
        match = re.fullmatch(r"([|>])([-+]?)([1-9]?)([-+]?)", header.strip())
        if not match:
            raise self.fail(f"bad block scalar header {header!r}", line)
        style, chomp, explicit = match.group(1), match.group(2) or match.group(4), match.group(3)
        block: list[str] = []
        block_indent = indent + int(explicit) if explicit else None
        while self.index < len(self.lines):
            raw = self.lines[self.index]
            if not raw.strip():
                block.append("")
                self.index += 1
                continue
            ind = len(raw) - len(raw.lstrip(" "))
            if block_indent is None:
                if ind <= indent:
                    break
                block_indent = ind
            if ind < block_indent:
                break
            block.append(raw[block_indent:])
            self.index += 1
        trailing = 0
        while block and block[-1] == "":
            block.pop()
            trailing += 1
        if style == "|":
            body = "\n".join(block)
        else:
            body = block[0] if block else ""
            for prev, cur in zip(block, block[1:]):
                if cur == "":
                    body += "\n"
                elif prev == "":
                    body += cur
                elif cur[:1] in " \t" or prev[:1] in " \t":
                    body += "\n" + cur
                else:
                    body += " " + cur
        if not block:
            return "\n" * trailing if chomp == "+" else ""
        if chomp == "-":
            return body
        if chomp == "+":
            return body + "\n" * (trailing + 1)
        return body + "\n"


def parse_yaml(text: str) -> Any:
    """Built-in parser (see module docstring for the supported subset)."""
    return _Parser(text).document()
