from typing import Any

from playwright.async_api import Locator, Page

from app.workflows.errors import WorkflowValidationError
from app.workflows.schema import (
    CssLocator,
    LabelLocator,
    LocatorTarget,
    RoleLocator,
    TestIdLocator,
    TextLocator,
    XPathLocator,
)


def resolve_locator(page_or_locator: Page | Locator, target: LocatorTarget) -> Locator:
    """Resolves a typed LocatorTarget DSL model into an active Playwright Locator."""
    match target:
        case RoleLocator():
            kwargs: dict[str, Any] = {"exact": target.exact}
            if target.name is not None:
                kwargs["name"] = target.name
            # role string in Playwright (e.g. "button", "link", "heading")
            return page_or_locator.get_by_role(target.role, **kwargs)

        case TestIdLocator():
            # Playwright get_by_test_id looks up data-testid attribute
            return page_or_locator.get_by_test_id(target.value)

        case TextLocator():
            return page_or_locator.get_by_text(target.value, exact=target.exact)

        case LabelLocator():
            return page_or_locator.get_by_label(target.value, exact=target.exact)

        case CssLocator():
            return page_or_locator.locator(target.value)

        case XPathLocator():
            return page_or_locator.locator(target.value)

        case _:
            raise WorkflowValidationError(
                message=f"Unsupported locator strategy: {type(target).__name__}",
                code="INVALID_LOCATOR",
                details={"target": str(target)},
            )
