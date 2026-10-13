"""The production image carries everything the app loads from disk at runtime."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
_IMAGE = "dmc268-api-test:image-contents"


@pytest.mark.skipif(
    shutil.which("docker") is None or os.environ.get("DMC268_RUN_DOCKER_TESTS") != "1",
    reason="set DMC268_RUN_DOCKER_TESTS=1 with Docker running",
)
def test_image_loads_reviewer_prompts(payload_data: dict[str, Any]) -> None:
    subprocess.run(
        ["docker", "build", "-q", "-t", _IMAGE, str(ROOT)],
        capture_output=True,
        check=True,
    )
    fixtures = ROOT / "tests/review/fixtures"
    descriptor = json.loads((fixtures / "v1-descriptor.json").read_text("utf-8"))
    expected_digest = hashlib.sha256(
        json.dumps(
            descriptor,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    load = """
import json, sys
from pathlib import Path
from app.review.prompt import PromptBuilder
from app.review.schemas.context import ContextPayload
data = json.load(sys.stdin)
builder = PromptBuilder.load(
    Path('.agents/reviewer'), 'v1', expected_digest=data['digest']
)
payload = ContextPayload.model_validate(data['payload'])
prompt = builder.build(payload, max_input_tokens=10000)
assert [m.model_dump() for m in prompt.messages] == data['messages']['initial']
repair = builder.build_repair(
    payload, raw_text='{"summary": "x"}',
    details=['findings: Field required', 'limitations: Field required'],
    max_input_tokens=10000,
)
assert [m.model_dump() for m in repair.messages] == data['messages']['repair']
assert json.loads(builder.output_schema_json) == data['schema']
print(builder.prompt_version)
"""

    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            _IMAGE,
            "python",
            "-c",
            load,
        ],
        input=json.dumps(
            {
                "digest": expected_digest,
                "payload": payload_data,
                "messages": json.loads(
                    (fixtures / "v1-messages.json").read_text("utf-8")
                ),
                "schema": json.loads(
                    (fixtures / "v1-provider-schema.json").read_text("utf-8")
                ),
            }
        ),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "v1"
