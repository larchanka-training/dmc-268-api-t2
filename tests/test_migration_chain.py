from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_publication_and_sse_extend_accepted_intake_and_ownership_migrations() -> None:
    root = Path(__file__).parents[1]
    config = Config(str(root / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["0009"]
    assert scripts.get_revision("0009").down_revision == "0008"
    assert scripts.get_revision("0008").down_revision == "0005"
    assert scripts.get_revision("0005").down_revision == "0004"
    assert scripts.get_revision("0004").down_revision == "0003"
    assert scripts.get_revision("0003").down_revision == "0002"
