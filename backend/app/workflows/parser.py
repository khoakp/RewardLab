import json
from typing import Any, Literal

import yaml

from app.workflows.errors import WorkflowSizeExceededError, WorkflowSyntaxError

MAX_WORKFLOW_SIZE_BYTES = 256 * 1024  # 256 KB


class UniqueKeySafeLoader(yaml.SafeLoader):
    """YAML SafeLoader that strictly forbids duplicate keys within mappings."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, yaml.MappingNode):
            raise yaml.constructor.ConstructorError(
                None, None, f"Expected a mapping node, but found {node.id}", node.start_mark
            )

        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                line = key_node.start_mark.line + 1
                col = key_node.start_mark.column + 1
                raise WorkflowSyntaxError(
                    f"Duplicate YAML key '{key}' detected at line {line}, column {col}",
                    details={"key": str(key), "line": line, "column": col},
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _json_duplicate_key_detector(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """JSON object_pairs_hook that strictly forbids duplicate keys in objects."""
    mapping: dict[str, Any] = {}
    for key, value in pairs:
        if key in mapping:
            raise WorkflowSyntaxError(
                f"Duplicate JSON key '{key}' detected",
                details={"key": str(key)},
            )
        mapping[key] = value
    return mapping


def parse_workflow_content(
    content: str | bytes,
    format_hint: Literal["auto", "yaml", "json"] = "auto",
) -> dict[str, Any]:
    """Safely parse workflow YAML or JSON content into a Python dictionary.

    Enforces:
    1. Maximum input size (256 KB) to prevent DoS / memory exhaustion.
    2. UTF-8 decoding.
    3. Safe deserialization (blocks arbitrary Python objects, custom tags).
    4. Strict rejection of duplicate dictionary keys in YAML and JSON.
    """
    raw_bytes = content.encode("utf-8") if isinstance(content, str) else content

    if len(raw_bytes) > MAX_WORKFLOW_SIZE_BYTES:
        raise WorkflowSizeExceededError(
            f"Workflow content size ({len(raw_bytes)} bytes) exceeds maximum limit "
            f"of {MAX_WORKFLOW_SIZE_BYTES} bytes ({MAX_WORKFLOW_SIZE_BYTES // 1024} KB)",
            details={"size_bytes": len(raw_bytes), "max_bytes": MAX_WORKFLOW_SIZE_BYTES},
        )

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise WorkflowSyntaxError(f"Workflow content must be valid UTF-8: {e}") from e

    stripped = text.strip()
    if not stripped:
        raise WorkflowSyntaxError("Workflow content is empty")

    # Determine format
    is_json = False
    if format_hint == "json":
        is_json = True
    elif format_hint == "yaml":
        is_json = False
    elif stripped.startswith("{"):
        is_json = True

    if is_json:
        try:
            parsed = json.loads(text, object_pairs_hook=_json_duplicate_key_detector)
        except json.JSONDecodeError as e:
            raise WorkflowSyntaxError(
                f"Invalid JSON syntax at line {e.lineno}, column {e.colno}: {e.msg}",
                details={"line": e.lineno, "column": e.colno},
            ) from e
    else:
        try:
            parsed = yaml.load(text, Loader=UniqueKeySafeLoader)
        except WorkflowSyntaxError:
            raise
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            line = mark.line + 1 if mark else None
            col = mark.column + 1 if mark else None
            msg = str(e)
            raise WorkflowSyntaxError(
                f"Invalid YAML syntax: {msg}",
                details={"line": line, "column": col},
            ) from e

    if not isinstance(parsed, dict):
        type_name = type(parsed).__name__
        raise WorkflowSyntaxError(
            f"Expected top-level workflow definition to be a mapping/object, got {type_name}"
        )

    return parsed
