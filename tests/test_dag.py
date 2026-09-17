"""Tests for DAG Graph Builder & Cycle Detection (Story 1.2)."""

import pytest

from agency.yaml_engine.dag import CycleDetectedError, DAGBuilder
from agency.yaml_engine.schema import AgentNode, Edge, Phase, Workflow


def _make_workflow(
    name: str = "test",
    nodes: dict | None = None,
    edges: list[Edge] | None = None,
    entry_point: str = "a",
) -> Workflow:
    """Helper to create a minimal Workflow for testing."""
    if nodes is None:
        nodes = {}
    if edges is None:
        edges = []
    return Workflow(
        name=name,
        nodes=nodes,
        edges=edges,
        entry_point=entry_point,
    )


def _make_workflow_raw(
    name: str = "test",
    nodes: dict | None = None,
    edges: list[Edge] | None = None,
    entry_point: str = "a",
) -> Workflow:
    """Helper to create a Workflow bypassing validation (for cycle tests)."""
    if nodes is None:
        nodes = {}
    if edges is None:
        edges = []
    return Workflow.model_construct(
        name=name,
        nodes=nodes,
        edges=edges,
        entry_point=entry_point,
    )


def _agent(id_: str) -> AgentNode:
    return AgentNode(id=id_, type="agent", model="llama3", prompt_template="test")


# --- Valid DAG tests ---


def test_linear_chain():
    """Valid linear chain A→B→C produces correct topological order."""
    wf = _make_workflow(
        nodes={"a": _agent("a"), "b": _agent("b"), "c": _agent("c")},
        edges=[
            Edge(from_id="a", to_id="b"),
            Edge(from_id="b", to_id="c"),
        ],
        entry_point="a",
    )
    builder = DAGBuilder(wf)
    adj = builder.build_dag()
    assert adj == {"a": ["b"], "b": ["c"], "c": []}

    cycle = builder.detect_cycle()
    assert cycle is None

    topo = builder.topological_sort()
    assert topo == ["a", "b", "c"]


def test_diamond_graph():
    """Diamond graph A→B, A→C, B→D, C→D respects all dependency constraints."""
    wf = _make_workflow(
        nodes={
            "a": _agent("a"),
            "b": _agent("b"),
            "c": _agent("c"),
            "d": _agent("d"),
        },
        edges=[
            Edge(from_id="a", to_id="b"),
            Edge(from_id="a", to_id="c"),
            Edge(from_id="b", to_id="d"),
            Edge(from_id="c", to_id="d"),
        ],
        entry_point="a",
    )
    builder = DAGBuilder(wf)
    builder.build_dag()

    cycle = builder.detect_cycle()
    assert cycle is None

    topo = builder.topological_sort()
    assert topo[0] == "a"
    assert topo[-1] == "d"
    # b and c must come before d, after a
    assert topo.index("b") < topo.index("d")
    assert topo.index("c") < topo.index("d")
    assert topo.index("a") < topo.index("b")
    assert topo.index("a") < topo.index("c")


def test_independent_branches():
    """Independent branches A→B and X→Y interleave validly in topological sort."""
    wf = _make_workflow(
        nodes={
            "a": _agent("a"),
            "b": _agent("b"),
            "x": _agent("x"),
            "y": _agent("y"),
        },
        edges=[
            Edge(from_id="a", to_id="b"),
            Edge(from_id="x", to_id="y"),
        ],
        entry_point="a",
    )
    builder = DAGBuilder(wf)
    builder.build_dag()

    cycle = builder.detect_cycle()
    assert cycle is None

    topo = builder.topological_sort()
    assert len(topo) == 4
    assert set(topo) == {"a", "b", "x", "y"}
    assert topo.index("a") < topo.index("b")
    assert topo.index("x") < topo.index("y")


def test_single_node_no_edges():
    """Single node with no edges produces a valid topological sort."""
    wf = _make_workflow(
        nodes={"solo": _agent("solo")},
        edges=[],
        entry_point="solo",
    )
    builder = DAGBuilder(wf)
    topo = builder.topological_sort()
    assert topo == ["solo"]


def test_entry_point_isolation():
    """Entry point with no outgoing edges still appears first in topological sort."""
    wf = _make_workflow(
        nodes={
            "entry": _agent("entry"),
            "other": _agent("other"),
        },
        edges=[],
        entry_point="entry",
    )
    builder = DAGBuilder(wf)
    topo = builder.topological_sort()
    assert topo[0] == "entry"


# --- Cycle detection tests ---


def test_simple_cycle():
    """Simple cycle A→B→A raises CycleDetectedError with correct nodes."""
    wf = _make_workflow_raw(
        nodes={"a": _agent("a"), "b": _agent("b")},
        edges=[
            Edge(from_id="a", to_id="b"),
            Edge(from_id="b", to_id="a"),
        ],
        entry_point="a",
    )

    builder = DAGBuilder(wf)
    builder.build_dag()

    cycle = builder.detect_cycle()
    assert cycle is not None
    assert set(cycle) == {"a", "b"}

    with pytest.raises(CycleDetectedError) as exc_info:
        builder.topological_sort()
    assert set(exc_info.value.cycle_nodes) == {"a", "b"}


def test_complex_cycle():
    """Complex cycle A→B→C→A raises CycleDetectedError with all involved nodes."""
    wf = _make_workflow_raw(
        nodes={
            "a": _agent("a"),
            "b": _agent("b"),
            "c": _agent("c"),
        },
        edges=[
            Edge(from_id="a", to_id="b"),
            Edge(from_id="b", to_id="c"),
            Edge(from_id="c", to_id="a"),
        ],
        entry_point="a",
    )

    builder = DAGBuilder(wf)
    builder.build_dag()

    cycle = builder.detect_cycle()
    assert cycle is not None
    assert set(cycle) == {"a", "b", "c"}

    with pytest.raises(CycleDetectedError) as exc_info:
        builder.topological_sort()
    assert set(exc_info.value.cycle_nodes) == {"a", "b", "c"}


def test_cycle_error_message_format():
    """CycleDetectedError message includes cycle path notation."""
    error = CycleDetectedError(["a", "b", "c"])
    assert "a" in str(error)
    assert "b" in str(error)
    assert "c" in str(error)
    assert "->" in str(error) or "→" in str(error)


# --- Integration: Workflow validation triggers CycleDetectedError ---


def test_workflow_validation_raises_cycle_error():
    """Workflow model validator detects cycles and raises error with node IDs."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        _make_workflow(
            nodes={"a": _agent("a"), "b": _agent("b")},
            edges=[
                Edge(from_id="a", to_id="b"),
                Edge(from_id="b", to_id="a"),
            ],
            entry_point="a",
        )
    # The underlying error should mention both nodes in the cycle
    err_str = str(exc_info.value)
    assert "a" in err_str and "b" in err_str


# --- Topological sort constraint verification ---


def test_topo_sort_satisfies_all_constraints():
    """Every edge in the DAG is respected by the topological ordering."""
    wf = _make_workflow(
        nodes={
            "a": _agent("a"),
            "b": _agent("b"),
            "c": _agent("c"),
            "d": _agent("d"),
            "e": _agent("e"),
        },
        edges=[
            Edge(from_id="a", to_id="b"),
            Edge(from_id="a", to_id="c"),
            Edge(from_id="b", to_id="d"),
            Edge(from_id="c", to_id="d"),
            Edge(from_id="d", to_id="e"),
        ],
        entry_point="a",
    )
    builder = DAGBuilder(wf)
    topo = builder.topological_sort()

    for edge in wf.edges:
        assert topo.index(edge.from_id) < topo.index(edge.to_id), (
            f"Edge {edge.from_id} -> {edge.to_id} violated by ordering {topo}"
        )


# --- Phase-aware DAG tests ---


def test_get_phase_nodes_returns_correct_subset():
    """get_phase_nodes returns phase members in topological order."""
    wf = _make_workflow(
        nodes={
            "a": _agent("a"),
            "b": _agent("b"),
            "c": _agent("c"),
            "d": _agent("d"),
        },
        edges=[
            Edge(from_id="a", to_id="b"),
            Edge(from_id="a", to_id="c"),
            Edge(from_id="b", to_id="d"),
            Edge(from_id="c", to_id="d"),
        ],
        entry_point="a",
    )
    wf.phases = [
        Phase(id="p1", name="Gen", node_ids=["a", "b"]),
        Phase(id="p2", name="Review", node_ids=["c", "d"]),
    ]
    builder = DAGBuilder(wf)
    nodes = builder.get_phase_nodes("p1")
    assert set(nodes) == {"a", "b"}
    assert nodes.index("a") < nodes.index("b")


def test_get_phase_order_returns_declaration_order():
    """get_phase_order returns phase IDs in YAML declaration order."""
    wf = _make_workflow(
        nodes={"a": _agent("a"), "b": _agent("b")},
        edges=[],
        entry_point="a",
    )
    wf.phases = [
        Phase(id="first", name="First", node_ids=["a"]),
        Phase(id="second", name="Second", node_ids=["b"]),
        Phase(id="third", name="Third", node_ids=[]),
    ]
    builder = DAGBuilder(wf)
    assert builder.get_phase_order() == ["first", "second", "third"]


def test_get_phase_nodes_unknown_phase_returns_empty():
    """get_phase_nodes for a non-existent phase returns empty list."""
    wf = _make_workflow(
        nodes={"a": _agent("a")},
        edges=[],
        entry_point="a",
    )
    builder = DAGBuilder(wf)
    assert builder.get_phase_nodes("unknown") == []
