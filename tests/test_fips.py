"""FIPS mode forbids MD5 and other non-approved algorithms; brain uses SHA-256 only."""

from pathlib import Path

SRC = Path(__file__).parents[1] / "src"


def test_source_never_uses_md5_or_sha1():
    offenders = []
    for p in SRC.rglob("*.py"):
        text = p.read_text().lower()
        for algo in ("md5", "sha1("):
            if algo in text:
                offenders.append(f"{p.relative_to(SRC)}: {algo}")
    assert not offenders
