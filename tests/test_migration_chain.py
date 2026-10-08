from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_contract_migration_extends_the_accepted_ownership_migration() -> None:
    root = Path(__file__).parents[1]
    config = Config(str(root / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["0004"]
    assert scripts.get_revision("0004").down_revision == "0003"
    assert scripts.get_revision("0003").down_revision == "0002"
