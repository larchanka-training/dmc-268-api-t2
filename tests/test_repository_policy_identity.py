"""Public identity contract; old literals captured independently at7e1b350."""

from uuid import UUID

from app.context.snapshot import capture_settings, snapshot_id
from app.db.models import RepositorySettings
from app.db.review_starts import settings_identity
from app.domain.repository_policy import StoredReviewPolicy
from app.domain.settings import compute_repository_settings_digest


def legacy_settings() -> RepositorySettings:
    return RepositorySettings(
        id=UUID("10000000-0000-0000-0000-000000000020"),
        repository_id=UUID("20000000-0000-0000-0000-000000000020"),
        rules={"instructions": ["Сохраняйте проверку нулевого знаменателя."]},
        ignores={"globs": ["vendor/**"]},
        max_starts_per_hour=10,
        max_active_jobs=4,
        output_language="ru",
        surrounding_lines=5,
        rules_digest="e992da2fdc38df1022bcbb8cafb0b7ef23a4d4a66b9f79b8569dc582a6620bc5",
        review_policy=None,
    )


def legacy_values() -> dict[str, object]:
    settings = legacy_settings()
    return {
        **capture_settings(settings),
        "rules_source": {
            "source_settings_id": str(settings.id),
            "source_settings_identity": (
                "671ea4c57bb35efd1de499b0b3545595817c9be2392704ef7c745a82583ccde2"
            ),
            "file": {
                "state": "absent",
                "path": ".review/rules.md",
                "base_sha": "a" * 40,
            },
        },
    }


def test_effective_snapshot_identity_binds_nonnull_review_policy() -> None:
    values = legacy_values()
    first = snapshot_id(
        {
            **values,
            "review_policy": StoredReviewPolicy(minimum_severity="medium").model_dump(
                mode="json"
            ),
        }
    )
    changed = snapshot_id(
        {
            **values,
            "review_policy": StoredReviewPolicy(minimum_severity="critical").model_dump(
                mode="json"
            ),
        }
    )
    assert changed != first


# Existing NULL behavior characterization; literals came from unchanged7e1b350.
def test_null_policy_keeps_old_three_public_identity_literals() -> None:
    source = legacy_settings()
    assert (
        settings_identity(source)
        == "671ea4c57bb35efd1de499b0b3545595817c9be2392704ef7c745a82583ccde2"
    )
    assert (
        compute_repository_settings_digest(
            id=source.id,
            repository_id=source.repository_id,
            rules=source.rules,
            ignores=source.ignores,
            output_language=source.output_language,
            surrounding_lines=source.surrounding_lines,
            review_policy=None,
        )
        == "5f9ec414cc0979122120c07e84fbe048c7b2ef272f9b3452db90ed06c90ca2f3"
    )
    assert snapshot_id({**legacy_values(), "review_policy": None}) == UUID(
        "5be5ef7b-7d06-5f31-bea2-d5fc55c14670"
    )
