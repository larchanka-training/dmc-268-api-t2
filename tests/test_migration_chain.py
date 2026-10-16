from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_github_intake_extends_accepted_contract_and_ownership_migrations() -> None:
    root = Path(__file__).parents[1]
    config = Config(str(root / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["0013"]
    assert scripts.get_revision("0013").down_revision == "0012"
    assert scripts.get_revision("0012").down_revision == "0011"
    assert scripts.get_revision("0011").down_revision == "0010"
    assert scripts.get_revision("0009").down_revision == "0008"
    assert scripts.get_revision("0008").down_revision == "0007"
    assert scripts.get_revision("0007").down_revision == "0006"
    assert scripts.get_revision("0006").down_revision == "0005"
    assert scripts.get_revision("0005").down_revision == "0004"
    assert scripts.get_revision("0004").down_revision == "0003"
    assert scripts.get_revision("0003").down_revision == "0002"
