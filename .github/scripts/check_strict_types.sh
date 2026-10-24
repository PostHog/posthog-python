#!/usr/bin/env bash
set -euo pipefail

PYTHON_VERSION="${PYTHON_VERSION:-3.11}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

python -m venv "$tmp/.venv"
"$tmp/.venv/bin/python" -m pip install --quiet --upgrade pip
"$tmp/.venv/bin/python" -m pip install --quiet . pyright

cat > "$tmp/strict_posthog_types.py" <<'PY'
# pyright: strict
import atexit

import posthog
from posthog import FeatureFlagEvaluations, FlagValue, Posthog
from posthog.emails import AsyncEmails, Emails, SendEmailResult
from posthog.ai.evaluations import (
    AsyncOfflineEvaluations,
    BooleanScorerConfig,
    EvaluationItem,
    NumericPassingRule,
    NumericScorerConfig,
    OfflineEvaluations,
)

client = Posthog("phc_test")
atexit.register(client.shutdown)

groups: dict[str, str | int] = {"company": 123}

flag_value: FlagValue | None = client.get_feature_flag("flag", 123, groups=groups)
all_flags: dict[str, FlagValue] | None = posthog.get_all_flags("user", groups=groups)
enabled: bool | None = posthog.feature_enabled("flag", "user", groups=groups)
payload: object | None = client.get_feature_flag_payload("flag", "user", groups=groups)
evaluations: FeatureFlagEvaluations = posthog.evaluate_flags(123, groups=groups)
span: posthog.Span = client.start_span("job")
active: posthog.Span | None = posthog.get_active_span()
span.end()

_ = (flag_value, all_flags, enabled, payload, evaluations, active)

emails = Emails("phs_example", migrated_from="resend")
template_send: SendEmailResult = emails.send(to="user@example.com", template_id="0190f6d2-7c1e-7b3a-9f5e-1c2d3e4f5a6b", template_data={"name": "Ada"}, capture_engagement=False, max_retries=0)
rendered_send: SendEmailResult = emails.send(to="user@example.com", template_key="password-reset", subject="Reset", html="<p>Reset</p>", from_="Acme <security@example.com>", reply_to=["help@example.com"])
local_template_send: SendEmailResult = emails.send(to="user@example.com", template_key="welcome", subject="Hi {{ variables.name }}", text="Hi", template_data={"name": "Ada"})
invite_send: SendEmailResult = emails.send(to="user@example.com", template_key="invite", subject="Invite", html="<p>Invite</p>", cc="team@example.com", bcc=["audit@example.com"], headers={"X-Entity-Ref-ID": "order-123"}, tags=[{"name": "category", "value": "invite"}], attachments=[{"filename": "invite.ics", "content": b"BEGIN:VCALENDAR", "content_type": "text/calendar"}, {"filename": "logo.png", "content": "iVBORw0KGgo=", "content_id": "logo"}])
_ = (template_send, rendered_send, local_template_send, invite_send)


async def async_email_types() -> None:
    sent: SendEmailResult = await AsyncEmails("phs_example").send(to="user@example.com", template_key="welcome", subject="Hi", html="<p>Hi</p>")
    _ = sent


async def offline_evaluation_types(
    sync_client: OfflineEvaluations, async_client: AsyncOfflineEvaluations
) -> None:
    numeric = sync_client.scorers.create(
        name="Relevance",
        kind="numeric",
        config=NumericScorerConfig(
            min=0, max=1,
            passing_rule=NumericPassingRule(operator="gte", threshold=0.8),
        ),
    )
    config: NumericScorerConfig = numeric.config
    experiment = sync_client.create_experiment(name="baseline", run_source="ci")
    item = EvaluationItem(input="question", output="answer")
    experiment.upload_result(item=item, scorer_version_id=numeric.current_version_id, value=0.9)
    boolean = await async_client.scorers.create(
        name="Toxicity", kind="boolean", config=BooleanScorerConfig(true_is_failure=True)
    )
    async_experiment = await async_client.create_experiment(name="async run")
    await async_experiment.upload_result(item=item, scorer_version_id=boolean.current_version_id, value=False)
    _ = config
PY

cat > "$tmp/invalid_types.py" <<'PY'
# pyright: strict
from posthog.ai.evaluations import BooleanScorerConfig, CategoricalScorerConfig, NumericPassingRule, NumericScorerConfig, OfflineEvaluations
from posthog.emails import AsyncEmails, Emails

client = OfflineEvaluations(project_id=123, secret_key="phx_example")
NumericPassingRule(operator="gt", threshold=0.8)  # expected-type-error
NumericScorerConfig(unknown_setting=True)  # expected-type-error
CategoricalScorerConfig(selection_mode="single")  # expected-type-error
client.scorers.create(name="Bad config", kind="numeric", config=BooleanScorerConfig(true_is_failure=True))  # expected-type-error
Emails("phs_example").send(to="user@example.com", template_id="0190f6d2-7c1e-7b3a-9f5e-1c2d3e4f5a6b", template_key="welcome", subject="Hi", html="<p>Hi</p>")  # expected-type-error
Emails("phs_example").send(to="user@example.com", template_key="welcome", html="<p>Hi</p>")  # expected-type-error
Emails("phs_example").send(to="user@example.com", template_id="0190f6d2-7c1e-7b3a-9f5e-1c2d3e4f5a6b", cc="team@example.com")  # expected-type-error
Emails("phs_example").send(to="user@example.com", template_key="welcome", subject="Hi", html="<p>Hi</p>", attachments=[{"filename": "a.txt", "content": [104, 105]}])  # expected-type-error


async def invalid_async_email() -> None:
    await AsyncEmails("phs_example").send(to="user@example.com", template_id="0190f6d2-7c1e-7b3a-9f5e-1c2d3e4f5a6b", subject="Hi")  # expected-type-error
PY

"$tmp/.venv/bin/python" - <<'PY' > "$tmp/public_api_access.py"
import inspect

import posthog
from posthog import Posthog

print("# pyright: strict")
print("import posthog")
print("from posthog import Posthog")
print('client = Posthog("phc_test")')

for name, obj in inspect.getmembers(Posthog):
    if name.startswith("_"):
        continue
    if inspect.isfunction(obj) or inspect.ismethoddescriptor(obj):
        print(f"client_{name} = client.{name}")

for name, obj in inspect.getmembers(posthog):
    if name.startswith("_") or name.startswith("inner_"):
        continue
    if inspect.isfunction(obj):
        print(f"module_{name} = posthog.{name}")
PY

cat > "$tmp/pyrightconfig.json" <<JSON
{
  "typeCheckingMode": "strict",
  "pythonVersion": "$PYTHON_VERSION",
  "venvPath": "$tmp",
  "venv": ".venv",
  "reportMissingTypeStubs": "error",
  "reportPrivateImportUsage": "error",
  "reportUnknownArgumentType": "error",
  "reportUnknownMemberType": "error",
  "reportUnknownVariableType": "error"
}
JSON

cd "$tmp"
"$tmp/.venv/bin/python" -m pyright strict_posthog_types.py public_api_access.py

if "$tmp/.venv/bin/python" -m pyright --outputjson invalid_types.py > invalid_types.json; then
    echo "Invalid offline evaluation configurations and email sends unexpectedly passed type checking."
    exit 1
fi
"$tmp/.venv/bin/python" - <<'PY'
import json
from pathlib import Path

expected = {
    index for index, line in enumerate(Path("invalid_types.py").read_text().splitlines())
    if "expected-type-error" in line
}
diagnostics = json.loads(Path("invalid_types.json").read_text())["generalDiagnostics"]
actual = {item["range"]["start"]["line"] for item in diagnostics if item["severity"] == "error"}
assert actual == expected, diagnostics
print("Offline evaluation configuration and email send types reject invalid inputs.")
PY
