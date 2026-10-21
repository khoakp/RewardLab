import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.workflows.errors import UnsupportedSchemaVersionError

CURRENT_SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = {1}

MAX_WORKFLOW_STEPS = 50
MAX_TIMEOUT_SECONDS = 300
MAX_STEP_TIMEOUT_SECONDS = 60
MAX_RETRIES = 5

ID_PATTERN = r"^[a-zA-Z0-9_-]{1,64}$"
ID_REGEX = re.compile(ID_PATTERN)


# =============================================================================
# Locator Target Schemas (Discriminated Union by 'by')
# =============================================================================


class RoleLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by: Literal["role"] = "role"
    role: str = Field(..., min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=256)
    exact: bool = True

    @field_validator("role", "name")
    @classmethod
    def strip_and_check_non_empty(cls, v: str | None) -> str | None:
        if v is not None:
            v_stripped = v.strip()
            if not v_stripped:
                raise ValueError("Locator value must not be empty or whitespace-only")
            return v_stripped
        return v


class TestIdLocator(BaseModel):
    __test__ = False
    model_config = ConfigDict(extra="forbid")
    by: Literal["test_id"] = "test_id"
    value: str = Field(..., min_length=1, max_length=128)

    @field_validator("value")
    @classmethod
    def validate_value(cls, v: str) -> str:
        v_stripped = v.strip()
        if not v_stripped:
            raise ValueError("test_id locator value must not be empty or whitespace-only")
        return v_stripped


class TextLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by: Literal["text"] = "text"
    value: str = Field(..., min_length=1, max_length=256)
    exact: bool = False

    @field_validator("value")
    @classmethod
    def validate_value(cls, v: str) -> str:
        v_stripped = v.strip()
        if not v_stripped:
            raise ValueError("text locator value must not be empty or whitespace-only")
        return v_stripped


class LabelLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by: Literal["label"] = "label"
    value: str = Field(..., min_length=1, max_length=256)
    exact: bool = False

    @field_validator("value")
    @classmethod
    def validate_value(cls, v: str) -> str:
        v_stripped = v.strip()
        if not v_stripped:
            raise ValueError("label locator value must not be empty or whitespace-only")
        return v_stripped


class CssLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by: Literal["css"] = "css"
    value: str = Field(..., min_length=1, max_length=512)

    @field_validator("value")
    @classmethod
    def validate_value(cls, v: str) -> str:
        v_stripped = v.strip()
        if not v_stripped:
            raise ValueError("CSS locator value must not be empty or whitespace-only")
        # Prohibit dangerous javascript expressions
        if "javascript:" in v_stripped.lower():
            raise ValueError("CSS locator must not contain 'javascript:'")
        return v_stripped


class XPathLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    by: Literal["xpath"] = "xpath"
    value: str = Field(..., min_length=1, max_length=512)

    @field_validator("value")
    @classmethod
    def validate_value(cls, v: str) -> str:
        v_stripped = v.strip()
        if not v_stripped:
            raise ValueError("XPath locator value must not be empty or whitespace-only")
        return v_stripped


LocatorTarget = Annotated[
    RoleLocator | TestIdLocator | TextLocator | LabelLocator | CssLocator | XPathLocator,
    Field(discriminator="by"),
]


# =============================================================================
# Action Step Schemas (Discriminated Union by 'action')
# =============================================================================


class StepBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=64, pattern=ID_PATTERN)
    description: str | None = Field(default=None, max_length=512)
    timeout_seconds: int | None = Field(default=None, ge=1, le=MAX_STEP_TIMEOUT_SECONDS)


class NavigateStep(StepBase):
    action: Literal["navigate"] = "navigate"
    path: str | None = Field(default=None, max_length=512)
    url: str | None = Field(default=None, max_length=1024)
    target_url: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def validate_navigation_target(self) -> "NavigateStep":
        effective_url = self.url or self.target_url
        if not self.path and not effective_url:
            raise ValueError("Navigate step must specify either 'path' or 'url'")
        if self.path and not self.path.startswith("/"):
            raise ValueError("Navigate step 'path' must start with '/' (e.g. '/tasks')")
        if effective_url:
            raw_url = effective_url.lower().strip()
            if not (raw_url.startswith("http://") or raw_url.startswith("https://")):
                raise ValueError("Navigate step 'url' must use 'http' or 'https' scheme")
        return self


class ClickStep(StepBase):
    action: Literal["click"] = "click"
    target: LocatorTarget
    side_effect: bool = False
    button: Literal["left", "right", "middle"] = "left"
    click_count: int = Field(default=1, ge=1, le=3)


class FillStep(StepBase):
    action: Literal["fill"] = "fill"
    target: LocatorTarget
    value: str = Field(..., max_length=2048)
    clear_before: bool = True


class HoverStep(StepBase):
    action: Literal["hover"] = "hover"
    target: LocatorTarget
    force: bool = False


class ScrollStep(StepBase):
    action: Literal["scroll"] = "scroll"
    target: LocatorTarget | None = None
    direction: Literal["up", "down", "left", "right"] | None = "down"
    amount: int = Field(default=300, ge=1, le=10000)


class PressKeyStep(StepBase):
    action: Literal["press_key"] = "press_key"
    key: str = Field(..., min_length=1, max_length=64)
    target: LocatorTarget | None = None


class WaitForStateStep(StepBase):
    action: Literal["wait_for_state"] = "wait_for_state"
    target: LocatorTarget
    state: Literal["visible", "hidden", "attached", "detached"] = "visible"


class WaitVisibleStep(StepBase):
    action: Literal["wait_visible"] = "wait_visible"
    target: LocatorTarget


class AssertVisibleStep(StepBase):
    action: Literal["assert_visible"] = "assert_visible"
    target: LocatorTarget


class ExtractTextStep(StepBase):
    action: Literal["extract_text"] = "extract_text"
    target: LocatorTarget
    output: str = Field(..., min_length=1, max_length=64, pattern=ID_PATTERN)


class VerifyStateStep(StepBase):
    action: Literal["verify_state"] = "verify_state"
    source: Literal["rewardlab_api", "dom", "response"] = "rewardlab_api"
    field: str = Field(..., min_length=1, max_length=128)
    equals: Any = Field(...)
    target: LocatorTarget | None = None
    task_id: str | None = Field(default=None, max_length=64)


class SaveResultStep(StepBase):
    action: Literal["save_result"] = "save_result"
    key: str = Field(..., min_length=1, max_length=64, pattern=ID_PATTERN)
    value_from: str | None = Field(default=None, max_length=64, pattern=ID_PATTERN)
    value: Any | None = None

    @model_validator(mode="after")
    def validate_value_source(self) -> "SaveResultStep":
        if self.value_from is None and self.value is None:
            raise ValueError("SaveResult step must specify either 'value_from' or 'value'")
        if self.value_from is not None and self.value is not None:
            raise ValueError("SaveResult step cannot specify both 'value_from' and 'value'")
        return self


class NotifyUserStep(StepBase):
    action: Literal["notify_user"] = "notify_user"
    message: str = Field(..., min_length=1, max_length=1024)
    level: Literal["info", "warning", "error"] = "info"


class RequestHumanActionStep(StepBase):
    action: Literal["request_human_action"] = "request_human_action"
    prompt: str = Field(..., min_length=1, max_length=1024)
    timeout_seconds: int = Field(default=300, ge=10, le=1800)


WorkflowStep = Annotated[
    NavigateStep
    | ClickStep
    | FillStep
    | HoverStep
    | ScrollStep
    | PressKeyStep
    | WaitForStateStep
    | WaitVisibleStep
    | AssertVisibleStep
    | ExtractTextStep
    | VerifyStateStep
    | SaveResultStep
    | NotifyUserStep
    | RequestHumanActionStep,
    Field(discriminator="action"),
]


# =============================================================================
# Execution Limits & Root Workflow Definition
# =============================================================================


class ExecutionLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: int = Field(default=30, ge=1, le=MAX_TIMEOUT_SECONDS)
    max_retries: int = Field(default=1, ge=0, le=MAX_RETRIES)
    step_timeout_seconds: int = Field(default=15, ge=1, le=MAX_STEP_TIMEOUT_SECONDS)


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=CURRENT_SCHEMA_VERSION)
    id: str = Field(default="draft_workflow", min_length=1, max_length=64, pattern=ID_PATTERN)
    name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=1024)
    provider: str = Field(..., min_length=1, max_length=64)
    requested_capabilities: list[str] = Field(default_factory=list, max_length=32)
    execution: ExecutionLimits = Field(default_factory=ExecutionLimits)
    steps: list[WorkflowStep] = Field(..., min_length=1, max_length=MAX_WORKFLOW_STEPS)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, v: int) -> int:
        if v not in SUPPORTED_SCHEMA_VERSIONS:
            raise UnsupportedSchemaVersionError(v, SUPPORTED_SCHEMA_VERSIONS)
        return v

    @model_validator(mode="after")
    def validate_unique_step_ids(self) -> "WorkflowDefinition":
        seen_ids: set[str] = set()
        duplicates: list[str] = []
        for step in self.steps:
            if step.id in seen_ids:
                duplicates.append(step.id)
            seen_ids.add(step.id)
        if duplicates:
            raise ValueError(f"Duplicate step IDs found in workflow: {sorted(set(duplicates))}")
        return self
