"""Prompt sets: the five slot texts of a help policy, loaded from files and hashed by text.

A set is a directory with one `<slot>.md` per slot, or another set with some slots replaced
(`{base: <set>, <slot>: <file>}`). The harness only loads, checks, and hashes the texts; the
plugin renders the `{{name}}` placeholders, so the lists below are part of the contract with it
(documented in docs/spec.md).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import Field

from llm_second_opinion.contracts import PromptSet, PromptSlot, PromptTexts, Strict

# The placeholders each slot may use. Changing a list changes what the plugin must render.
_BRIEF = ("task_summary", "tried", "error", "hypothesis", "question", "code", "level", "trigger")
PLACEHOLDERS: dict[PromptSlot, tuple[str, ...]] = {
    PromptSlot.EXECUTOR_GUIDANCE: ("max_consults",),
    PromptSlot.CONSULT_TOOL: ("max_consults",),
    PromptSlot.BRIEF: _BRIEF,
    PromptSlot.ADVISOR_SYSTEM: ("level", "max_answer_tokens"),
    PromptSlot.ADVICE_INJECTION: ("advice", "consults_left"),
}
# Placeholders a slot must use, or the slot would drop what it exists to carry.
REQUIRED: dict[PromptSlot, tuple[str, ...]] = {PromptSlot.ADVICE_INJECTION: ("advice",)}
_PLACEHOLDER = re.compile(r"\{\{(.*?)\}\}")


class PromptError(ValueError):
    pass


class PromptOverride(Strict):
    """A prompt set made from another one with some slots replaced by files."""

    base: str = Field(min_length=1, description="Name of the set this one starts from.")
    executor_guidance: Path | None = None
    consult_tool: Path | None = None
    brief: Path | None = None
    advisor_system: Path | None = None
    advice_injection: Path | None = None

    def slots(self) -> dict[PromptSlot, Path]:
        return {s: p for s in PromptSlot if (p := getattr(self, s.value)) is not None}


PromptSource = Path | PromptOverride


def prompt_hash(texts: PromptTexts) -> str:
    """16 hex chars of SHA-256 over the slot texts; set names and file paths are not in it."""
    canonical = json.dumps(texts.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def read_slot(slot: PromptSlot, path: Path) -> str:
    """A slot's text, checked: non-empty, and only that slot's placeholders.

    Line endings become `\\n` and trailing whitespace is dropped, so a stray final newline does
    not change the hash.
    """
    try:
        text = path.read_text().replace("\r\n", "\n").rstrip()
    except OSError as e:
        raise PromptError(f"prompt slot {slot.value}: cannot read {path}: {e}") from e
    if not text:
        raise PromptError(f"{path}: prompt slot {slot.value} is empty")
    allowed = PLACEHOLDERS[slot]
    used = _PLACEHOLDER.findall(text)
    unknown = [name for name in dict.fromkeys(used) if name not in allowed]
    if unknown:
        names, known = _braces(unknown), _braces(allowed) or "none"
        raise PromptError(
            f"{path}: unknown placeholder(s) {names} in slot {slot.value} (allowed: {known})"
        )
    missing = [name for name in REQUIRED.get(slot, ()) if name not in used]
    if missing:
        raise PromptError(f"{path}: slot {slot.value} must use {_braces(missing)}")
    return text


def _braces(names: Sequence[str]) -> str:
    return ", ".join(f"{{{{{n}}}}}" for n in names)


def read_directory(path: Path) -> dict[PromptSlot, str]:
    """All five slots from `<path>/<slot>.md`."""
    if not path.is_dir():
        raise PromptError(f"prompt set directory not found: {path}")
    expected = {f"{s.value}.md" for s in PromptSlot}
    extra = sorted(p.name for p in path.glob("*.md") if p.name not in expected)
    if extra:
        raise PromptError(f"{path}: not a prompt slot: {', '.join(extra)}")
    missing = sorted(name for name in expected if not (path / name).is_file())
    if missing:
        raise PromptError(f"{path}: missing prompt slot file(s): {', '.join(missing)}")
    return {s: read_slot(s, path / f"{s.value}.md") for s in PromptSlot}


def resolve(name: str, sources: Mapping[str, PromptSource]) -> PromptSet:
    """The named set with its base chain applied; fails on unknown names and cycles."""
    chain: list[str] = []
    current = name
    while True:
        if current in chain:
            cycle = " -> ".join([*chain[chain.index(current) :], current])
            raise PromptError(f"prompt sets form a cycle: {cycle}")
        if current not in sources:
            known = ", ".join(sorted(sources)) or "none"
            where = f" (base of {chain[-1]!r})" if chain else ""
            raise PromptError(f"no prompt set named {current!r}{where}; prompt sets: {known}")
        chain.append(current)
        source = sources[current]
        if isinstance(source, PromptOverride):
            current = source.base
            continue
        texts = read_directory(source)
        break
    # Apply overrides from the root of the chain back to the named set.
    for set_name in reversed(chain[:-1]):
        override = sources[set_name]
        assert isinstance(override, PromptOverride)
        texts |= {slot: read_slot(slot, path) for slot, path in override.slots().items()}
    prompt_texts = PromptTexts(**{s.value: t for s, t in texts.items()})
    return PromptSet(name=name, hash=prompt_hash(prompt_texts), texts=prompt_texts)
