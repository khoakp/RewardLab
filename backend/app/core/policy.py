import ipaddress
from urllib.parse import urlparse

from app.core.config import settings
from app.core.errors import POLICY_DENIED
from app.domain.policy import AuditEvent, PolicyDecision, ProviderPolicy
from app.observability.logging import get_logger, redact_data, redact_url_credentials

logger = get_logger("adbap.policy")

BLOCKED_METADATA_HOSTS = {
    "169.254.169.254",
    "169.254.169.123",
    "169.254.170.2",
    "fd00:ec2::254",
    "metadata.google.internal",
    "metadata",
    "instance-data",
}

DEFAULT_POLICIES: dict[str, ProviderPolicy] = {
    "rewardlab": ProviderPolicy(
        provider="rewardlab",
        automation_allowed=True,
        domain_allowlist=settings.automation_allowed_domains,
        allowed_actions=[
            "navigate",
            "wait_visible",
            "extract_text",
            "click",
            "fill",
            "assert_visible",
            "verify_state",
            "save_result",
        ],
        required_capabilities=["read_tasks", "mark_reviewed"],
        allowed_capabilities=["read_tasks", "mark_reviewed"],
        allowed_ports=[],
        allow_popups=False,
        block_service_workers=True,
        sensitive_action_requires_approval=True,
        allow_auth_storage=True,
        ai_allowed=False,
    ),
    "microsoft_rewards": ProviderPolicy(
        provider="microsoft_rewards",
        automation_allowed=False,
        domain_allowlist=[],
        allowed_actions=[],
        required_capabilities=[],
        allowed_capabilities=[],
        allowed_ports=[],
        allow_popups=False,
        block_service_workers=True,
        sensitive_action_requires_approval=True,
        allow_auth_storage=False,
        ai_allowed=False,
    ),
}


class PolicyEvaluator:
    """Evaluates requested actions and draft workflows against provider policies.

    Note: Static URL validation performed here validates declared schemes, origins,
    userinfo and host allowlists at workflow submission time. Runtime network/redirect
    sandboxing belongs to Milestone M2.
    """

    def __init__(
        self,
        policies: dict[str, ProviderPolicy] | None = None,
        record_audit: bool = True,
    ) -> None:
        self.policies = policies or DEFAULT_POLICIES.copy()
        self.record_audit = record_audit
        self.audit_log: list[AuditEvent] = []

    def get_policy(self, provider: str) -> ProviderPolicy | None:
        """Retrieves ProviderPolicy for the specified provider."""
        return self.policies.get(provider)

    def _record_audit_event(
        self,
        actor: str,
        action: str,
        resource: str,
        outcome: str,
        details: dict,
    ) -> None:
        sanitized = redact_data(details)
        event = AuditEvent(
            actor=actor,
            action=action,
            resource=resource,
            outcome=outcome,
            sanitized_details=sanitized,
        )
        if self.record_audit:
            self.audit_log.append(event)
        logger.info(
            "Audit event: actor=%s action=%s resource=%s outcome=%s details=%s",
            actor,
            action,
            resource,
            outcome,
            sanitized,
        )

    def validate_static_url(
        self,
        url: str,
        domain_allowlist: list[str],
        allowed_ports: list[int] | None = None,
    ) -> tuple[bool, str, str | None]:
        """Statically inspects a URL for scheme, embedded credentials, metadata IPs,
        port boundaries, and exact host allowlist matching.
        Returns: (is_valid, reason, sanitized_url)
        """
        if not url or not isinstance(url, str):
            return False, "Target URL must be a non-empty string", None

        trimmed = url.strip()
        lower_raw = trimmed.lower()

        # Reject dangerous/unsupported schemes early
        for prohibited in ("javascript:", "data:", "file:", "vbscript:", "blob:", "about:"):
            if lower_raw.startswith(prohibited):
                return (
                    False,
                    (
                        f"Prohibited URL scheme '{prohibited.rstrip(':')}'. "
                        "Only 'http' and 'https' are allowed."
                    ),
                    redact_url_credentials(trimmed),
                )

        # Check for embedded credentials in raw URL before parsing
        if "://" in trimmed:
            scheme_part, rest = trimmed.split("://", 1)
            if "@" in rest.split("/", 1)[0]:
                return (
                    False,
                    "URL contains embedded credentials which is prohibited",
                    redact_url_credentials(trimmed),
                )

        try:
            parsed = urlparse(trimmed)
        except Exception as e:
            return False, f"Malformed URL syntax: {e}", redact_url_credentials(trimmed)

        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            return (
                False,
                f"Prohibited URL scheme '{parsed.scheme}'. Only 'http' and 'https' are allowed.",
                redact_url_credentials(trimmed),
            )

        hostname = (parsed.hostname or "").lower().strip()
        if not hostname:
            return (
                False,
                "URL missing hostname",
                redact_url_credentials(trimmed),
            )

        # Check for link-local / cloud metadata hostnames or IPs
        if hostname in BLOCKED_METADATA_HOSTS:
            return (
                False,
                f"Target host '{hostname}' is a prohibited metadata address",
                redact_url_credentials(trimmed),
            )

        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_link_local or ip.is_multicast or ip.is_reserved:
                return (
                    False,
                    f"Target IP address '{hostname}' is a prohibited address range",
                    redact_url_credentials(trimmed),
                )
        except ValueError:
            # Hostname is not a raw IP address; standard name matching proceeds
            pass

        # Insecure HTTP allowed only on localhost / loopback
        if scheme == "http" and hostname not in ("localhost", "127.0.0.1", "::1"):
            return (
                False,
                f"Insecure HTTP scheme prohibited for non-loopback host '{hostname}'",
                redact_url_credentials(trimmed),
            )

        # Exact allowlist match (prevents domain spoofing like rewardlab.local.attacker.com)
        normalized_allowlist = {d.lower().strip() for d in domain_allowlist}
        if hostname not in normalized_allowlist:
            allowed_sorted = sorted(normalized_allowlist)
            return (
                False,
                f"Host '{hostname}' is not in provider domain allowlist {allowed_sorted}",
                redact_url_credentials(trimmed),
            )

        # Port validation if allowed_ports is specified and non-empty
        if allowed_ports:
            port = parsed.port or (443 if scheme == "https" else 80)
            if port not in allowed_ports:
                return (
                    False,
                    f"Port {port} is not in provider allowed ports {allowed_ports}",
                    redact_url_credentials(trimmed),
                )

        sanitized_url = redact_url_credentials(trimmed)
        return True, "URL allowed", sanitized_url

    def evaluate_action(
        self,
        provider: str,
        action: str,
        target_url: str | None = None,
        actor: str = "workflow_validator",
    ) -> PolicyDecision:
        policy = self.policies.get(provider)

        # 1. Unknown provider or automation not allowed
        if not policy or not policy.automation_allowed:
            reason = (
                f"Automation is prohibited for provider '{provider}'. "
                "Microsoft Rewards is companion-only and unknown providers are denied by default."
            )
            self._record_audit_event(
                actor=actor,
                action=action,
                resource=f"provider:{provider}",
                outcome="DENIED",
                details={"reason": reason, "target_url": target_url},
            )
            return PolicyDecision(
                allowed=False,
                error_code=POLICY_DENIED,
                reason=reason,
                sanitized_target=redact_url_credentials(target_url) if target_url else None,
            )

        # 2. Action allowed check
        if action not in policy.allowed_actions:
            reason = (
                f"Action '{action}' is not permitted for provider '{provider}'. "
                f"Allowed actions: {policy.allowed_actions}"
            )
            self._record_audit_event(
                actor=actor,
                action=action,
                resource=f"provider:{provider}",
                outcome="DENIED",
                details={"reason": reason, "target_url": target_url},
            )
            return PolicyDecision(
                allowed=False,
                error_code=POLICY_DENIED,
                reason=reason,
                sanitized_target=redact_url_credentials(target_url) if target_url else None,
            )

        # 3. Static target URL check if provided
        sanitized_url = None
        if target_url:
            valid, url_reason, sanitized_url = self.validate_static_url(
                target_url,
                policy.domain_allowlist,
                allowed_ports=policy.allowed_ports,
            )
            if not valid:
                self._record_audit_event(
                    actor=actor,
                    action=action,
                    resource=f"provider:{provider}",
                    outcome="DENIED",
                    details={"reason": url_reason, "target_url": target_url},
                )
                return PolicyDecision(
                    allowed=False,
                    error_code=POLICY_DENIED,
                    reason=url_reason,
                    sanitized_target=sanitized_url,
                )

        # 4. Action and destination allowed
        self._record_audit_event(
            actor=actor,
            action=action,
            resource=f"provider:{provider}",
            outcome="ALLOWED",
            details={"action": action, "target_url": target_url},
        )
        return PolicyDecision(
            allowed=True,
            reason="Action and destination permitted by provider policy",
            sanitized_target=sanitized_url,
        )

    def evaluate_capabilities(
        self,
        provider: str,
        requested_capabilities: list[str],
        actor: str = "workflow_validator",
    ) -> PolicyDecision:
        """Evaluates whether requested workflow capabilities are granted for the provider."""
        policy = self.policies.get(provider)
        if not policy or not policy.automation_allowed:
            reason = (
                f"Automation is prohibited for provider '{provider}'. "
                "Microsoft Rewards is companion-only and unknown providers are denied by default."
            )
            return PolicyDecision(allowed=False, error_code=POLICY_DENIED, reason=reason)

        granted = set(policy.allowed_capabilities) | set(policy.required_capabilities)
        unauthorized = [c for c in requested_capabilities if c not in granted]
        if unauthorized:
            reason = (
                f"Requested capabilities {unauthorized} are not granted for provider '{provider}'. "
                f"Granted capabilities: {sorted(granted)}"
            )
            self._record_audit_event(
                actor=actor,
                action="request_capabilities",
                resource=f"provider:{provider}",
                outcome="DENIED",
                details={"reason": reason, "unauthorized": unauthorized},
            )
            return PolicyDecision(
                allowed=False,
                error_code="CAPABILITY_NOT_GRANTED",
                reason=reason,
            )

        return PolicyDecision(allowed=True, reason="Capabilities granted")

    def validate_api_endpoint(
        self,
        base_url: str,
        endpoint: str,
        provider: str,
    ) -> tuple[bool, str, str | None]:
        """Validates that an API endpoint does not perform SSRF or escape base_url."""
        policy = self.policies.get(provider)
        if not policy:
            return False, f"Unknown provider '{provider}'", None

        cleaned_ep = endpoint.strip()
        # Reject absolute URLs or protocol-relative trickery in endpoint
        if "://" in cleaned_ep or cleaned_ep.startswith("//"):
            return (
                False,
                "API endpoint must be a relative path, not an absolute or foreign URL",
                None,
            )

        base = base_url.rstrip("/")
        path = cleaned_ep.lstrip("/")
        full_url = f"{base}/{path}"

        return self.validate_static_url(
            full_url,
            policy.domain_allowlist,
            allowed_ports=policy.allowed_ports,
        )


default_evaluator = PolicyEvaluator()
