from __future__ import annotations

from pathlib import Path

import yaml

from .schema import Workflow


class YAMLParseError(Exception):
    """Raised when YAML syntax is invalid."""

    def __init__(self, message: str, line: int | None = None, column: int | None = None) -> None:
        self.line = line
        self.column = column
        super().__init__(message)


class SchemaValidationError(Exception):
    """Raised when YAML content fails schema validation."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(" | ".join(errors))


def load_workflow(path_or_content: str | Path) -> Workflow:
    """Load and validate a workflow from a YAML file path or raw YAML string.

    Args:
        path_or_content: File path (str/Path) or raw YAML content string.

    Returns:
        Validated Workflow instance.

    Raises:
        YAMLParseError: If YAML syntax is invalid.
        SchemaValidationError: If content fails schema validation.
    """
    if isinstance(path_or_content, Path):
        path_or_content = str(path_or_content)

    raw_dict = _parse_yaml(path_or_content)
    return _validate_and_build(raw_dict)


def _parse_yaml(input_str: str) -> dict:
    """Parse YAML content, distinguishing between file paths and raw YAML."""
    if _looks_like_yaml_content(input_str):
        content = input_str
    else:
        path = Path(input_str)
        if path.exists() and path.is_file():
            content = path.read_text(encoding="utf-8")
        else:
            content = input_str

    try:
        result = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        line = None
        column = None
        if hasattr(exc, "problem_mark") and exc.problem_mark:
            line = exc.problem_mark.line + 1
            column = exc.problem_mark.column + 1
        raise YAMLParseError(
            f"Invalid YAML syntax: {exc.problem}",
            line=line,
            column=column,
        ) from exc

    if result is None:
        raise YAMLParseError("YAML content is empty")
    if not isinstance(result, dict):
        raise YAMLParseError(
            f"Workflow YAML must be a mapping (key-value pairs), got {type(result).__name__}"
        )
    return result


def _looks_like_yaml_content(s: str) -> bool:
    """Heuristic to decide if a string is raw YAML or a file path."""
    stripped = s.strip()
    return (
        "\n" in s
        or stripped.startswith(("{", "[", "-", "..."))
        or (":" in stripped and not stripped.startswith(("/", "\\")))
    )


def _validate_and_build(raw: dict) -> Workflow:
    """Validate raw dict against Workflow schema and return model."""
    from pydantic import ValidationError

    try:
        return Workflow.model_validate(raw)
    except ValidationError as exc:
        errors = _format_validation_errors(exc)
        raise SchemaValidationError(errors) from exc


def _format_validation_errors(exc: Exception) -> list[str]:
    """Format Pydantic validation errors into readable messages."""
    from pydantic import ValidationError

    if isinstance(exc, ValidationError):
        messages = []
        for err in exc.errors():
            loc = err.get("loc", ())
            node_id = ""
            if len(loc) >= 3 and loc[0] == "nodes":
                node_id = f" in node '{loc[1]}'"
            field = loc[-1] if loc else "unknown"
            msg = err.get("msg", "")
            typ = err.get("type", "")
            if typ == "missing":
                messages.append(f"Missing required field '{field}'{node_id}")
            elif typ == "value_error":
                messages.append(f"Validation error for '{field}'{node_id}: {msg}")
            else:
                messages.append(f"{msg} (field: '{field}'{node_id})")
        return messages
    return [str(exc)]
