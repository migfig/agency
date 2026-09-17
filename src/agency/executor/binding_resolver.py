from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_BINDING_PATTERN = re.compile(r"\{\{\s*nodes\.([a-zA-Z0-9_-]+)\.output\s*\}\}")
_MALFORMED_BRACE_PATTERN = re.compile(r"\{\{([^}]+)\}\}")


def _check_malformed(template: str) -> None:
    """Log warnings for {{ ... }} expressions that don't match valid binding syntax."""
    valid_spans: set[tuple[int, int]] = {
        (m.start(), m.end()) for m in _BINDING_PATTERN.finditer(template)
    }
    for m in _MALFORMED_BRACE_PATTERN.finditer(template):
        if (m.start(), m.end()) not in valid_spans:
            logger.warning("Malformed binding expression: '%s'", m.group(0))


class BindingResolutionError(Exception):
    """Raised when binding resolution cannot proceed due to invalid context."""

    def __init__(self, bindings: list[str], context_keys: set[str]) -> None:
        self.bindings = bindings
        self.context_keys = context_keys
        missing = [b for b in bindings if b not in context_keys]
        msg = f"Cannot resolve bindings {missing} — available keys: {sorted(context_keys)}"
        super().__init__(msg)


class VariableResolver:
    """Resolves `{{ nodes.<id>.output }}` placeholders in template strings.

    Unresolved bindings are replaced with an empty string and a warning is logged.
    Malformed expressions that do not match the expected pattern are left untouched.
    """

    @staticmethod
    def resolve(template: str, context: dict[str, str]) -> str:
        """Interpolate all recognized bindings in *template* using *context*.

        Args:
            template: String potentially containing ``{{ nodes.X.output }}`` placeholders.
            context: Mapping of binding keys to their resolved string values.

        Returns:
            The template with all matched bindings replaced.

        Raises:
            BindingResolutionError: If *context* is not a dict.
        """
        if not isinstance(context, dict):
            found = list(VariableResolver.find_bindings(template))
            raise BindingResolutionError(found, set())

        # Detect malformed {{ ... }} expressions before substitution
        _check_malformed(template)

        def _replacer(match: re.Match[str]) -> str:
            node_id = match.group(1)
            key = f"nodes.{node_id}.output"
            if key in context:
                return context[key]
            logger.warning("Unresolved binding: '%s' (key '%s' not in context)", match.group(0), key)
            return ""

        return _BINDING_PATTERN.sub(_replacer, template)

    @staticmethod
    def find_bindings(template: str) -> list[str]:
        """Return deduplicated list of binding keys found in *template*.

        Args:
            template: String potentially containing binding expressions.

        Returns:
            List of unique keys like ``"nodes.a.output"``.
        """
        seen: set[str] = set()
        result: list[str] = []
        for match in _BINDING_PATTERN.finditer(template):
            node_id = match.group(1)
            key = f"nodes.{node_id}.output"
            if key not in seen:
                seen.add(key)
                result.append(key)
        return result
