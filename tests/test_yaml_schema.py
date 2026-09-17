"""Tests for YAML schema definition & validation (Story 1.1)."""

import pytest

from agency.yaml_engine.parser import (
    SchemaValidationError,
    YAMLParseError,
    load_workflow,
)
from agency.yaml_engine.schema import ModelSpec, Workflow

VALID_WORKFLOW_YAML = """
name: test-workflow
entry_point: start
nodes:
  start:
    id: start
    type: agent
    model: llama3
    prompt_template: "Hello"
  decision:
    id: decision
    type: conditional
    condition: "result > 0"
    branches:
      "yes": end
      "no": fallback
  end:
    id: end
    type: agent
    model: llama3
    prompt_template: "Done"
  fallback:
    id: fallback
    type: tool_call
    tool_name: retry
    arguments_template: "{}"
edges:
  - from_id: start
    to_id: decision
  - from_id: decision
    to_id: end
    condition_label: "yes"
  - from_id: decision
    to_id: fallback
    condition_label: "no"
"""


def test_valid_workflow_parses():
    """A valid multi-node workflow parses and validates successfully."""
    wf = load_workflow(VALID_WORKFLOW_YAML)
    assert isinstance(wf, Workflow)
    assert wf.name == "test-workflow"
    assert len(wf.nodes) == 4
    assert wf.entry_point == "start"
    assert len(wf.edges) == 3


def test_duplicate_node_ids_rejected():
    """Duplicate node IDs raise a validation error."""
    bad_yaml = """
name: bad
entry_point: a
nodes:
  a:
    id: dup
    type: agent
    model: x
    prompt_template: y
  b:
    id: dup
    type: agent
    model: x
    prompt_template: y
"""
    with pytest.raises(SchemaValidationError):
        load_workflow(bad_yaml)


def test_missing_required_field_rejected():
    """Missing required fields (e.g., prompt_template on agent) raise an error."""
    bad_yaml = """
name: bad
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: x
"""
    with pytest.raises(SchemaValidationError):
        load_workflow(bad_yaml)


def test_invalid_edge_reference_rejected():
    """Edges pointing to non-existent nodes raise an error."""
    bad_yaml = """
name: bad
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: x
    prompt_template: y
edges:
  - from_id: a
    to_id: nonexistent
"""
    with pytest.raises(SchemaValidationError):
        load_workflow(bad_yaml)


def test_malformed_yaml_syntax_error():
    """Malformed YAML raises YAMLParseError."""
    bad_yaml = "nodes:\n  - [unbalanced"
    with pytest.raises(YAMLParseError):
        load_workflow(bad_yaml)


def test_entry_point_not_in_nodes_rejected():
    """Entry point referencing non-existent node raises an error."""
    bad_yaml = """
name: bad
entry_point: ghost
nodes:
  a:
    id: a
    type: agent
    model: x
    prompt_template: y
"""
    with pytest.raises(SchemaValidationError):
        load_workflow(bad_yaml)


def test_all_node_types_parse():
    """All 6 node types can be parsed and validated."""
    all_types_yaml = """
name: all-types
entry_point: agent1
nodes:
  agent1:
    id: agent1
    type: agent
    model: llama3
    prompt_template: "test"
  cond1:
    id: cond1
    type: conditional
    condition: "x > 0"
    branches:
      "yes": merge1
      "no": end
  merge1:
    id: merge1
    type: merge
    inputs:
      - agent1
      - cond1
    strategy: all
  broadcast1:
    id: broadcast1
    type: broadcast
    targets:
      - hil1
      - tool1
  hil1:
    id: hil1
    type: human_in_loop
    prompt: "Review this"
  tool1:
    id: tool1
    type: tool_call
    tool_name: save
    arguments_template: "{}"
  end:
    id: end
    type: agent
    model: llama3
    prompt_template: "done"
"""
    wf = load_workflow(all_types_yaml)
    assert len(wf.nodes) == 7
    types = {node.type for node in wf.nodes.values()}
    assert types == {"agent", "conditional", "merge", "broadcast", "human_in_loop", "tool_call"}


def test_key_mismatch_with_node_id_rejected():
    """Node dict key must match the node's id field."""
    bad_yaml = """
name: bad
entry_point: actual
nodes:
  wrong_key:
    id: actual
    type: agent
    model: x
    prompt_template: y
"""
    with pytest.raises(SchemaValidationError):
        load_workflow(bad_yaml)


def test_self_referential_edge_rejected():
    """Edges from a node to itself are rejected."""
    bad_yaml = """
name: bad
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: x
    prompt_template: y
edges:
  - from_id: a
    to_id: a
"""
    with pytest.raises(SchemaValidationError, match="cannot reference itself"):
        load_workflow(bad_yaml)


def test_cyclic_workflow_rejected():
    """Cycles in the DAG are detected and rejected with involved node IDs."""
    bad_yaml = """
name: cyclic
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: x
    prompt_template: y
  b:
    id: b
    type: agent
    model: x
    prompt_template: y
  c:
    id: c
    type: agent
    model: x
    prompt_template: y
edges:
  - from_id: a
    to_id: b
  - from_id: b
    to_id: c
  - from_id: c
    to_id: a
"""
    with pytest.raises(SchemaValidationError) as exc_info:
        load_workflow(bad_yaml)
    # The underlying error should be CycleDetectedError mentioning all involved nodes
    err_str = str(exc_info.value)
    assert "a" in err_str and "b" in err_str and "c" in err_str


def test_conditional_branch_target_validation():
    """Conditional branch targets pointing to non-existent nodes are rejected."""
    bad_yaml = """
name: bad
entry_point: cond
nodes:
  cond:
    id: cond
    type: conditional
    condition: "x > 0"
    branches:
      "yes": nonexistent
edges: []
"""
    with pytest.raises(SchemaValidationError, match="references non-existent node"):
        load_workflow(bad_yaml)


def test_empty_merge_inputs_rejected():
    """Merge nodes with empty inputs list are rejected."""
    bad_yaml = """
name: bad
entry_point: m
nodes:
  m:
    id: m
    type: merge
    inputs: []
    strategy: all
"""
    with pytest.raises(SchemaValidationError):
        load_workflow(bad_yaml)


def test_empty_broadcast_targets_rejected():
    """Broadcast nodes with empty targets list are rejected."""
    bad_yaml = """
name: bad
entry_point: b
nodes:
  b:
    id: b
    type: broadcast
    targets: []
"""
    with pytest.raises(SchemaValidationError):
        load_workflow(bad_yaml)


def test_error_message_contains_node_id():
    """Error messages include the node ID that caused the failure."""
    bad_yaml = """
name: bad
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: x
"""
    with pytest.raises(SchemaValidationError) as exc_info:
        load_workflow(bad_yaml)
    assert "a" in str(exc_info.value) or "a" in " | ".join(exc_info.value.errors)


def test_yaml_parse_error_has_line_info():
    """YAMLParseError captures line and column information."""
    bad_yaml = "nodes:\n  - [unbalanced"
    with pytest.raises(YAMLParseError) as exc_info:
        load_workflow(bad_yaml)
    assert exc_info.value.line is not None
    assert exc_info.value.column is not None


def test_empty_yaml_content_rejected():
    """Empty YAML content raises YAMLParseError."""
    with pytest.raises(YAMLParseError, match="empty"):
        load_workflow("")


def test_yaml_list_rejected():
    """YAML that parses to a list (not mapping) raises YAMLParseError."""
    bad_yaml = "- item1\n- item2"
    with pytest.raises(YAMLParseError, match="mapping"):
        load_workflow(bad_yaml)


# --- Phase tests ---------------------------------------------------------

def test_workflow_with_phases_parses():
    """Workflow with phases field parses successfully."""
    yaml_content = """
name: phased-workflow
entry_point: start
nodes:
  start:
    id: start
    type: agent
    model: llama3
    prompt_template: "Hello"
  end:
    id: end
    type: agent
    model: llama3
    prompt_template: "Done"
phases:
  - id: p1
    name: Generation
    node_ids:
      - start
  - id: p2
    name: Review
    node_ids:
      - end
"""
    wf = load_workflow(yaml_content)
    assert len(wf.phases) == 2
    assert wf.phases[0].id == "p1"
    assert wf.phases[0].name == "Generation"
    assert wf.phases[0].node_ids == ["start"]


def test_phase_with_nonexistent_node_rejected():
    """Phase referencing a non-existent node ID raises validation error."""
    bad_yaml = """
name: bad
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: x
    prompt_template: y
phases:
  - id: p1
    name: Phase One
    node_ids:
      - a
      - ghost
"""
    with pytest.raises(SchemaValidationError, match="non-existent node"):
        load_workflow(bad_yaml)


def test_workflow_without_phases_has_empty_list():
    """Workflow without phases field defaults to empty list."""
    wf = load_workflow(VALID_WORKFLOW_YAML)
    assert wf.phases == []


# --- Summarization config tests (Story 3.2) -------------------------

MINIMAL_WORKFLOW_YAML = """
name: minimal
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: x
    prompt_template: y
"""


def test_summarization_fields_default_to_none():
    """Summarization fields are unset (None) when absent from YAML."""
    wf = load_workflow(VALID_WORKFLOW_YAML)
    assert wf.summarize_threshold is None
    assert wf.context_window is None
    assert wf.summarizer_model is None


def test_summarization_config_parses():
    """Explicit summarization config is exposed on the Workflow."""
    yaml_content = (
        MINIMAL_WORKFLOW_YAML
        + "summarize_threshold: 60\n"
        + "context_window: 4096\n"
        + "summarizer_model: phi-2\n"
    )
    wf = load_workflow(yaml_content)
    assert wf.summarize_threshold == 60
    assert wf.context_window == 4096
    assert wf.summarizer_model == "phi-2"


@pytest.mark.parametrize("bad_threshold", [0, 101])
def test_summarize_threshold_out_of_range_rejected(bad_threshold: int):
    """summarize_threshold outside 1..100 is rejected."""
    yaml_content = MINIMAL_WORKFLOW_YAML + f"summarize_threshold: {bad_threshold}\n"
    with pytest.raises(SchemaValidationError):
        load_workflow(yaml_content)


@pytest.mark.parametrize("bad_window", [0, -1])
def test_context_window_non_positive_rejected(bad_window: int):
    """Non-positive context_window is rejected."""
    yaml_content = MINIMAL_WORKFLOW_YAML + f"context_window: {bad_window}\n"
    with pytest.raises(SchemaValidationError):
        load_workflow(yaml_content)


@pytest.mark.parametrize("blank_model", ["", "   "])
def test_summarizer_model_blank_rejected(blank_model: str):
    """A blank summarizer_model is not a model name; reject it at the schema boundary."""
    yaml_content = MINIMAL_WORKFLOW_YAML + f'summarizer_model: "{blank_model}"\n'
    with pytest.raises(SchemaValidationError, match="non-blank model name"):
        load_workflow(yaml_content)


# --- Model registry tests (Story 5.1) --------------------------------------

def test_models_section_parses_to_model_specs():
    """A models: section is exposed as ModelSpec entries on the Workflow."""
    yaml_content = MINIMAL_WORKFLOW_YAML + (
        "models:\n"
        "  m-llama:\n"
        "    endpoint: http://127.0.0.1:8080\n"
        "    path: /tmp/llama.bin\n"
        "    vram_size: 8GB\n"
        "  m-phi:\n"
        "    endpoint: http://127.0.0.1:8081\n"
    )
    wf = load_workflow(yaml_content)
    assert isinstance(wf.models["m-llama"], ModelSpec)
    assert wf.models["m-llama"].endpoint == "http://127.0.0.1:8080"
    assert wf.models["m-llama"].path == "/tmp/llama.bin"
    assert wf.models["m-llama"].vram_size == "8GB"


def test_model_spec_fields_default_to_none():
    """Unset ModelSpec fields are None when absent from the models: entry."""
    wf = load_workflow(MINIMAL_WORKFLOW_YAML + "models:\n  m-phi:\n    endpoint: http://127.0.0.1:8081\n")
    assert wf.models["m-phi"].endpoint == "http://127.0.0.1:8081"
    assert wf.models["m-phi"].path is None
    assert wf.models["m-phi"].vram_size is None


def test_models_section_defaults_to_empty():
    """A workflow without a models: section defaults to an empty registry."""
    assert load_workflow(MINIMAL_WORKFLOW_YAML).models == {}


# --- Executable tools: tools: section (spec 005) ---------------------------

MINIMAL_TOOL_YAML = """
name: tools-test
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: x
    prompt_template: y
"""


def test_tools_section_parses_all_kinds():
    """shell/program/file_write definitions parse with their kind-specific fields."""
    yaml_content = MINIMAL_TOOL_YAML + (
        "tools:\n"
        "  lint_python:\n"
        "    kind: shell\n"
        '    command: "ruff check {file}"\n'
        "    working_dir: .\n"
        "    timeout_seconds: 30\n"
        '    description: "Lint a Python file"\n'
        "  run_python:\n"
        "    kind: program\n"
        '    program: "{file}"\n'
        '    interpreter: python\n'
        "  save_program:\n"
        '    kind: file_write\n'
        '    path: "programs/{name}.py"\n'
    )
    wf = load_workflow(yaml_content)
    assert set(wf.tools) == {"lint_python", "run_python", "save_program"}
    lint = wf.tools["lint_python"]
    assert lint.kind == "shell"
    assert lint.command == "ruff check {file}"
    assert lint.working_dir == "."
    assert lint.timeout_seconds == 30
    run = wf.tools["run_python"]
    assert run.kind == "program"
    assert run.program == "{file}"
    assert run.interpreter == "python"
    save = wf.tools["save_program"]
    assert save.kind == "file_write"
    assert save.path == "programs/{name}.py"


def test_tools_section_defaults_empty():
    """A workflow without a tools: section defaults to an empty mapping."""
    wf = load_workflow(MINIMAL_TOOL_YAML)
    assert wf.tools == {}


@pytest.mark.parametrize(
    ("kind", "required_field"),
    [
        ("shell", "command"),
        ("program", "program"),
        ("file_write", "path"),
    ],
)
def test_tool_kind_missing_required_field_rejected(kind: str, required_field: str):
    """Each kind requires its own template field (command/program/path)."""
    yaml_content = MINIMAL_TOOL_YAML + f"tools:\n  bad:\n    kind: {kind}\n"
    with pytest.raises(SchemaValidationError, match=required_field):
        load_workflow(yaml_content)


@pytest.mark.parametrize("command", ["echo }", "echo {unterminated", "echo {}"])
def test_malformed_template_rejected_at_load(command: str):
    """Malformed {name} templates (stray brace, unterminated, auto-numbered) fail at load."""
    yaml_content = MINIMAL_TOOL_YAML + (
        "tools:\n"
        "  bad:\n"
        "    kind: shell\n"
        f'    command: "{command}"\n'
    )
    with pytest.raises(SchemaValidationError):
        load_workflow(yaml_content)


def test_agent_node_tools_absent_is_none():
    """Agent nodes without a tools: block keep the opt-in default (today's behavior)."""
    wf = load_workflow(MINIMAL_TOOL_YAML)
    assert wf.nodes["a"].tools is None


def test_agent_tools_defaults_and_allow_list():
    """AgentTools defaults: max_rounds=4; allow is explicit."""
    yaml_content = """
name: tools-test
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: x
    prompt_template: y
  b:
    id: b
    type: agent
    model: x
    prompt_template: y
    tools:
      allow: [upper, lint_python]
tools:
  lint_python:
    kind: shell
    command: "ruff check {file}"
"""
    wf = load_workflow(yaml_content)
    assert wf.nodes["a"].tools is None
    tools = wf.nodes["b"].tools
    assert tools is not None
    assert tools.allow == ["upper", "lint_python"]
    assert tools.max_rounds == 4


@pytest.mark.parametrize("max_rounds", [0, -1])
def test_agent_tools_max_rounds_ge_1_rejected(max_rounds: int):
    yaml_content = f"""
name: tools-test
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: x
    prompt_template: y
    tools:
      max_rounds: {max_rounds}
"""
    with pytest.raises(SchemaValidationError):
        load_workflow(yaml_content)


def test_duplicate_tool_name_builtin_collision_rejected():
    """A tools: entry colliding with a built-in name is a duplicate (FR-017)."""
    yaml_content = MINIMAL_TOOL_YAML + (
        "tools:\n"
        "  upper:\n"
        '    kind: shell\n'
        '    command: "echo x"\n'
    )
    with pytest.raises(SchemaValidationError, match="duplicate tool name 'upper'"):
        load_workflow(yaml_content)


def test_allow_list_unknown_tool_rejected():
    """An allow-list entry that names no tool is rejected at load time."""
    yaml_content = """
name: tools-test
entry_point: a
nodes:
  a:
    id: a
    type: agent
    model: x
    prompt_template: y
    tools:
      allow: [ghost_tool]
"""
    with pytest.raises(SchemaValidationError, match="allow-list references unknown tool 'ghost_tool'"):
        load_workflow(yaml_content)


# -- spec 006 (T005): execution_environment / sandbox: / per-tool caps -------


def test_execution_environment_absent_is_none():
    wf = load_workflow(MINIMAL_TOOL_YAML)
    assert wf.execution_environment is None


@pytest.mark.parametrize("value", ["sandbox", "local"])
def test_execution_environment_accepts_valid_values(value):
    wf = load_workflow(MINIMAL_TOOL_YAML + f"execution_environment: {value}\n")
    assert wf.execution_environment == value


@pytest.mark.parametrize("bad", ["Sandbox", "cloud", "Sandbox "])
def test_execution_environment_invalid_rejected(bad):
    with pytest.raises(SchemaValidationError):
        load_workflow(MINIMAL_TOOL_YAML + f'execution_environment: "{bad}"\n')


def test_sandbox_mapping_parses_all_fields():
    yaml_content = MINIMAL_TOOL_YAML + (
        "sandbox:\n"
        "  cpu: 4\n"
        '  memory: "1GB"\n'
        "  pids_limit: 512\n"
        "  timeout_seconds: 60\n"
        "  output_limit_bytes: 2000000\n"
        '  scratch_size: "128MB"\n'
    )
    wf = load_workflow(yaml_content)
    assert wf.sandbox is not None
    assert wf.sandbox.cpu == 4
    assert wf.sandbox.memory == "1GB"
    assert wf.sandbox.pids_limit == 512
    assert wf.sandbox.timeout_seconds == 60
    assert wf.sandbox.output_limit_bytes == 2000000
    assert wf.sandbox.scratch_size == "128MB"


def test_sandbox_mapping_all_keys_optional():
    wf = load_workflow(MINIMAL_TOOL_YAML + "sandbox: {}\n")
    assert wf.sandbox is not None
    assert wf.sandbox.cpu is None
    assert wf.sandbox.memory is None
    assert wf.sandbox.pids_limit is None
    assert wf.sandbox.timeout_seconds is None
    assert wf.sandbox.output_limit_bytes is None
    assert wf.sandbox.scratch_size is None


def test_sandbox_absent_is_none():
    wf = load_workflow(MINIMAL_TOOL_YAML)
    assert wf.sandbox is None


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("cpu", 0),
        ("cpu", -1.5),
        ("pids_limit", 0),
        ("timeout_seconds", 0),
        ("output_limit_bytes", -5),
        ("memory", "512XB"),
        ("memory", "abc"),
        ("memory", "1TB"),
        ("scratch_size", "64 PB"),
    ],
)
def test_sandbox_mapping_invalid_values_rejected(field, bad):
    with pytest.raises(SchemaValidationError):
        load_workflow(MINIMAL_TOOL_YAML + f"sandbox:\n  {field}: {bad}\n")


@pytest.mark.parametrize(
    "definition",
    [
        {"kind": "shell", "command": "echo {x}"},
        {"kind": "program", "program": "scripts/{x}.py"},
        {"kind": "file_write", "path": "out/{x}.txt"},
    ],
)
def test_tool_definitions_accept_per_tool_caps(definition):
    definition = {**definition, "cpu": 8, "memory": "2GB", "pids_limit": 1024}
    yaml_content = MINIMAL_TOOL_YAML + (
        "tools:\n"
        "  heavy:\n"
        f"    kind: {definition['kind']}\n"
    )
    if "command" in definition:
        yaml_content += f"    command: {definition['command']!r}\n"
    if "program" in definition:
        yaml_content += f"    program: {definition['program']!r}\n"
    if "path" in definition:
        yaml_content += f"    path: {definition['path']!r}\n"
    yaml_content += "    cpu: 8\n    memory: '2GB'\n    pids_limit: 1024\n"
    wf = load_workflow(yaml_content)
    tool = wf.tools["heavy"]
    assert tool.cpu == 8
    assert tool.memory == "2GB"
    assert tool.pids_limit == 1024


def test_tool_definitions_caps_absent_are_none():
    yaml_content = MINIMAL_TOOL_YAML + (
        "tools:\n"
        "  plain:\n"
        '    kind: shell\n'
        '    command: "echo {x}"\n'
    )
    wf = load_workflow(yaml_content)
    tool = wf.tools["plain"]
    assert tool.cpu is None
    assert tool.memory is None
    assert tool.pids_limit is None


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("cpu", 0),
        ("pids_limit", 0),
        ("memory", "1TB"),
    ],
)
def test_tool_definitions_invalid_caps_rejected(field, bad):
    with pytest.raises(SchemaValidationError):
        load_workflow(
            MINIMAL_TOOL_YAML + f"tools:\n  bad:\n    kind: shell\n    command: 'echo x'\n    {field}: {bad}\n"
        )
