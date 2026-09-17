"""Tests for Variable Binding Resolution (Story 1.5)."""

import logging

import pytest

from agency.executor.binding_resolver import BindingResolutionError, VariableResolver

# --- resolve() tests ---


def test_resolve_all_bindings():
    """All valid bindings in template are replaced with context values."""
    template = "Hello {{ nodes.a.output }}"
    context = {"nodes.a.output": "world"}
    result = VariableResolver.resolve(template, context)
    assert result == "Hello world"


def test_resolve_multiple_bindings():
    """Multiple distinct bindings are resolved independently."""
    template = "{{ nodes.a.output }} and {{ nodes.b.output }}"
    context = {
        "nodes.a.output": "foo",
        "nodes.b.output": "bar",
    }
    result = VariableResolver.resolve(template, context)
    assert result == "foo and bar"


def test_resolve_repeated_binding():
    """The same binding appearing multiple times is resolved consistently."""
    template = "{{ nodes.a.output }} {{ nodes.a.output }}"
    context = {"nodes.a.output": "x"}
    result = VariableResolver.resolve(template, context)
    assert result == "x x"


def test_resolve_no_bindings():
    """Template without bindings is returned unchanged."""
    template = "No placeholders here"
    context: dict[str, str] = {}
    result = VariableResolver.resolve(template, context)
    assert result == "No placeholders here"


def test_resolve_empty_template():
    """Empty template returns empty string."""
    result = VariableResolver.resolve("", {"nodes.a.output": "world"})
    assert result == ""


def test_resolve_binding_with_spaces_in_braces():
    """Whitespace inside braces is tolerated: {{  nodes.a.output  }}."""
    template = "{{  nodes.a.output  }}"
    context = {"nodes.a.output": "hello"}
    result = VariableResolver.resolve(template, context)
    assert result == "hello"


def test_resolve_partial_bindings_fills_empty():
    """Missing context keys resolve to empty string."""
    template = "{{ nodes.a.output }} {{ nodes.b.output }}"
    context = {"nodes.a.output": "present"}
    result = VariableResolver.resolve(template, context)
    assert result == "present "


def test_resolve_partial_emits_logging_warning(caplog):
    """A logging warning is emitted for each unresolved binding."""
    template = "{{ nodes.missing.output }}"
    context: dict[str, str] = {}

    with caplog.at_level(logging.WARNING, logger="agency.executor.binding_resolver"):
        VariableResolver.resolve(template, context)

    assert any("Unresolved binding" in record.message for record in caplog.records)
    assert any("nodes.missing.output" in record.message for record in caplog.records)


def test_resolve_malformed_binding_left_as_is_warns(caplog):
    """Malformed expressions are left untouched and emit a logging warning."""
    template = "{{ nodes.a }} and {{ invalid_syntax }}"
    context: dict[str, str] = {}

    with caplog.at_level(logging.WARNING, logger="agency.executor.binding_resolver"):
        result = VariableResolver.resolve(template, context)

    assert result == template
    assert len(caplog.records) == 2
    assert any("Malformed binding" in r.message for r in caplog.records)


def test_resolve_invalid_context_raises():
    """Passing a non-dict context raises BindingResolutionError."""
    with pytest.raises(BindingResolutionError) as exc_info:
        VariableResolver.resolve("{{ nodes.a.output }}", "not-a-dict")  # type: ignore[arg-type]

    assert "nodes.a.output" in exc_info.value.bindings
    assert exc_info.value.context_keys == set()


# --- find_bindings() tests ---


def test_find_single_binding():
    """Single binding is detected."""
    bindings = VariableResolver.find_bindings("{{ nodes.x.output }}")
    assert bindings == ["nodes.x.output"]


def test_find_multiple_unique_bindings():
    """Multiple distinct bindings are all found."""
    template = "{{ nodes.a.output }} {{ nodes.b.output }}"
    bindings = VariableResolver.find_bindings(template)
    assert set(bindings) == {"nodes.a.output", "nodes.b.output"}


def test_find_duplicate_bindings_deduplicates():
    """Repeated bindings in the same template are deduplicated."""
    template = "{{ nodes.a.output }} {{ nodes.a.output }}"
    bindings = VariableResolver.find_bindings(template)
    assert bindings == ["nodes.a.output"]


def test_find_no_bindings():
    """Template without bindings returns empty list."""
    bindings = VariableResolver.find_bindings("plain text")
    assert bindings == []


def test_find_malformed_ignored():
    """Malformed expressions are not reported as bindings."""
    template = "{{ nodes.a }} {{ broken }}"
    bindings = VariableResolver.find_bindings(template)
    assert bindings == []


# --- BindingResolutionError tests ---


def test_error_message_format():
    """BindingResolutionError message lists missing bindings and available keys."""
    error = BindingResolutionError(
        bindings=["nodes.a.output", "nodes.b.output"],
        context_keys={"nodes.c.output"},
    )
    assert "nodes.a.output" in str(error)
    assert "nodes.b.output" in str(error)
    assert "nodes.c.output" in str(error)
