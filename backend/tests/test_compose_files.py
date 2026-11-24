import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
COMPOSE_FILES = sorted(REPO.glob("docker-compose*.yml"))

# Compose's `!reset` always yields an empty value, whatever follows the tag; replacing a list takes `!override`.
_RESET_WITH_VALUE = re.compile(r"^[^#\n]*:\s*!reset[ \t]+(?!\[\s*\]|\{\s*\}|null\b)[^\s#]", re.M)


@pytest.mark.skipif(not COMPOSE_FILES, reason="compose files are not next to the backend (image build)")
@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_should_use_override_when_a_compose_value_is_replaced_instead_of_cleared(path: Path):
    assert not _RESET_WITH_VALUE.findall(path.read_text(encoding="utf-8"))
