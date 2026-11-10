"""Versioned prompt files in brain/prompts.

Each file has frontmatter `name`, `version`, and `role`: `layer` prompts run
on the small model, `dream` and `answer` prompts on whichever model the
config assigns. The stamp recorded on every generated file includes the
SHA-256 of the prompt, so an edit made without bumping `version` is visible.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Literal

from brain.core.formats.records import PromptStamp
from brain.core.formats.yamlio import load_yaml, split_frontmatter

Role = Literal["layer", "dream", "answer"]


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    role: Role
    instructions: str
    sha256: str

    @property
    def stamp(self) -> PromptStamp:
        return PromptStamp(name=self.name, version=self.version, sha256=self.sha256)


@cache
def load_prompt(name: str) -> Prompt:
    text = resources.files("brain.prompts").joinpath(f"{name}.md").read_text(encoding="utf-8")
    fm_text, body = split_frontmatter(text)
    fm = load_yaml(fm_text)
    if fm.get("name") != name:
        raise ValueError(f"prompt file {name}.md declares name {fm.get('name')!r}")
    return Prompt(
        name=name,
        version=int(fm["version"]),
        role=fm.get("role", "layer"),
        instructions=body.strip(),
        sha256=hashlib.sha256(text.encode()).hexdigest()[:16],
    )
