from typing import Any

from pydantic import BaseModel, Field, model_validator


class ProviderPolicy(BaseModel):
    provider: str
    automation_allowed: bool
    domain_allowlist: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    allowed_capabilities: list[str] = Field(default_factory=list)
    allowed_ports: list[int] = Field(default_factory=list)
    allow_popups: bool = False
    block_service_workers: bool = True
    sensitive_action_requires_approval: bool = True
    allow_auth_storage: bool = False
    ai_allowed: bool = False

    @model_validator(mode="after")
    def _default_rewardlab_capabilities(self) -> "ProviderPolicy":
        if self.provider == "rewardlab":
            no_allowed = "allowed_capabilities" not in self.model_fields_set
            if no_allowed and not self.allowed_capabilities:
                self.allowed_capabilities = ["read_tasks", "mark_reviewed"]
            no_required = "required_capabilities" not in self.model_fields_set
            if no_required and not self.required_capabilities:
                self.required_capabilities = ["read_tasks", "mark_reviewed"]
        return self


class PolicyDecision(BaseModel):
    allowed: bool
    error_code: str | None = None
    reason: str
    sanitized_target: str | None = None


class AuditEvent(BaseModel):
    actor: str = "system"
    action: str
    resource: str
    outcome: str  # "ALLOWED" | "DENIED"
    sanitized_details: dict[str, Any] = Field(default_factory=dict)
