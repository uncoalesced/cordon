# Engineered by uncoalesced

from __future__ import annotations

from pathlib import Path

import pytest

WATERMARK = "# Engineered by uncoalesced"
SOURCE_ROOTS = ("features", "tests")

REPO_ROOT = Path(__file__).resolve().parents[1]


def _source_files() -> list[Path]:
    return sorted(path for root in SOURCE_ROOTS for path in (REPO_ROOT / root).rglob("*.py"))


def test_the_repo_has_source_files_to_check():
    assert len(_source_files()) >= 10


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_every_source_file_is_watermarked(path: Path):
    head = path.read_text(encoding="utf-8").splitlines()[:3]
    assert WATERMARK in head, f"{path.relative_to(REPO_ROOT)} is missing the watermark"
