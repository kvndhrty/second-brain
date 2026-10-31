"""YAML and frontmatter I/O that keeps comments and key order.

ruamel.yaml in round-trip mode is used so that when the tool changes one field
in a file you also edit by hand, your comments and layout survive.

YAML timestamps are loaded as plain strings and parsed by pydantic, because
ruamel's own timestamp type does not round-trip timezone offsets reliably.
As a result, dates written by the tool are quoted ('2026-10-30').
"""

from __future__ import annotations

import io
from datetime import date, datetime
from enum import Enum
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError

FORMAT_VERSION = 1


class FormatError(ValueError):
    """A file could not be parsed. The message names the problem."""


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 100
    y.indent(mapping=2, sequence=4, offset=2)
    y.constructor.add_constructor("tag:yaml.org,2002:timestamp", lambda self, node: self.construct_scalar(node))
    return y


def load_yaml(text: str) -> CommentedMap:
    try:
        data = _yaml().load(text)
    except YAMLError as e:
        raise FormatError(f"invalid YAML: {e}") from e
    if data is None:
        return CommentedMap()
    if not isinstance(data, CommentedMap):
        raise FormatError("expected a YAML mapping at the top level")
    return data


def dump_yaml(data: Any) -> str:
    buf = io.StringIO()
    _yaml().dump(data, buf)
    return buf.getvalue()


def plain(value: Any) -> Any:
    """Convert model-dumped values to YAML-safe scalars (ISO strings for dates)."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [plain(v) for v in value]
    return value


def merge_into(target: CommentedMap, values: dict[str, Any]) -> CommentedMap:
    """Write values into an existing mapping in place.

    Existing keys keep their position and comments; new keys are appended;
    keys whose new value is None and that were absent stay absent. Nested
    mappings are merged recursively so comments inside them survive too.
    """
    for key, value in values.items():
        value = plain(value)
        if value is None and key not in target:
            continue
        current = target.get(key)
        if isinstance(value, dict) and isinstance(current, CommentedMap):
            merge_into(current, value)
            for stale in [k for k in current if k not in value]:
                del current[stale]
        elif isinstance(value, list) and isinstance(current, CommentedSeq) and list(current) == value:
            continue  # unchanged list: keep its formatting
        else:
            target[key] = value
    return target


def split_frontmatter(text: str) -> tuple[str, str]:
    """Split '---\\n<yaml>\\n---\\n<body>' into (yaml, body)."""
    text = text.replace("\r\n", "\n")
    if text.startswith("﻿"):
        text = text[1:]
    if not text.startswith("---\n"):
        raise FormatError("file must start with a '---' frontmatter line")
    end = text.find("\n---\n", 3)
    if end == -1:
        if text.endswith("\n---"):
            end = len(text) - 4
        else:
            raise FormatError("frontmatter is not closed with a '---' line")
    fm = text[4 : end + 1]
    body = text[end + 5 :]
    return fm, body


def join_frontmatter(fm: CommentedMap, body: str) -> str:
    body = body.replace("\r\n", "\n")
    if body and not body.endswith("\n"):
        body += "\n"
    return f"---\n{dump_yaml(fm)}---\n{body}"


def check_format(data: dict[str, Any], what: str) -> None:
    version = data.get("format", FORMAT_VERSION)
    if not isinstance(version, int):
        raise FormatError(f"{what}: 'format' must be an integer")
    if version > FORMAT_VERSION:
        raise UnsupportedFormat(
            f"{what} uses format {version}, but this version of brain reads format "
            f"{FORMAT_VERSION}. Upgrade brain before writing to this store."
        )


class UnsupportedFormat(FormatError):
    pass
