"""Selected automatic intent is captured at the durable inbox public seam."""

from http import HTTPStatus
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker
from test_repository_settings_api import SettingsScope, replace_settings

from app.application.github_ports import GithubRequest
from app.db.models import Repository
from app.db.webhook_inbox import SqlAlchemyWebhookInbox

pytest_plugins = ("test_repositories_api", "test_repository_settings_api")


async def test_new_receipt_captures_enabled_policy_and_authorizing_actor(
    settings_scope: SettingsScope,
) -> None:
    scope = settings_scope
    saved = await replace_settings(
        scope,
        auto_review_enabled=True,
        base_branches=["main"],
        minimum_severity="medium",
    )
    assert saved.status_code == HTTPStatus.OK
    async with scope.catalog.postgres.engine.connect() as connection:
        external_id = await connection.scalar(
            Repository.__table__.select()
            .with_only_columns(Repository.external_id)
            .where(Repository.id == scope.repository)
        )
    assert external_id is not None
    factory = async_sessionmaker(scope.catalog.postgres.engine, expire_on_commit=False)
    inbox = SqlAlchemyWebhookInbox(factory)
    identifier = await inbox.accept(
        str(uuid4()),
        GithubRequest("team", "settings", external_id, 7, "a" * 40, "b" * 40),
    )
    assert identifier is not None
    scope.catalog.postgres.track("webhook_receipt", identifier)
    receipt = await inbox.get(identifier)
    assert receipt is not None
    assert receipt.automatic is not None
    intent = receipt.automatic
    assert intent.actor_user_id == scope.catalog.user
    assert intent.source_settings_id.hex == saved.json()["version"].replace("-", "")
    assert intent.repository_id == scope.repository
    assert intent.policy.base_branches == ["main"]
    assert intent.policy.minimum_severity == "medium"


async def test_old_off_receipt_is_not_selected_by_later_enable_or_duplicate(
    settings_scope: SettingsScope,
) -> None:
    scope = settings_scope
    async with scope.catalog.postgres.engine.connect() as connection:
        external_id = await connection.scalar(
            Repository.__table__.select()
            .with_only_columns(Repository.external_id)
            .where(Repository.id == scope.repository)
        )
    assert external_id is not None
    inbox = SqlAlchemyWebhookInbox(async_sessionmaker(scope.catalog.postgres.engine))
    delivery = str(uuid4())
    request = GithubRequest("team", "settings", external_id, 7, "a" * 40, "b" * 40)
    identifier = await inbox.accept(delivery, request)
    assert identifier is not None
    scope.catalog.postgres.track("webhook_receipt", identifier)
    before = await inbox.get(identifier)
    assert before is not None and before.automatic is None
    saved = await replace_settings(scope, auto_review_enabled=True)
    assert saved.status_code == HTTPStatus.OK
    assert await inbox.accept(delivery, request) is None
    after = await inbox.get(identifier)
    assert after is not None and after.automatic is None
