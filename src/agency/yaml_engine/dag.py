from __future__ import annotations

from collections import deque

from agency.yaml_engine.schema import Workflow


class CycleDetectedError(ValueError):
    """Raised when a circular dependency is detected in the workflow DAG."""

    def __init__(self, cycle_nodes: list[str]) -> None:
        self.cycle_nodes = cycle_nodes
        cycle_str = " -> ".join(cycle_nodes + [cycle_nodes[0]])
        super().__init__(f"Cycle detected involving nodes [{', '.join(cycle_nodes)}]: {cycle_str}")


class DAGBuilder:
    """Builds a directed acyclic graph from a validated Workflow and provides
    cycle detection and topological ordering for execution sequencing."""

    def __init__(self, workflow: Workflow) -> None:
        self.workflow = workflow
        self._adj: dict[str, list[str]] = {}
        self._reverse_adj: dict[str, list[str]] = {}

    def build_dag(self) -> dict[str, list[str]]:
        """Construct adjacency list from Workflow edges. Returns the adjacency dict."""
        node_ids = set(self.workflow.nodes.keys())
        self._adj = {nid: [] for nid in node_ids}
        self._reverse_adj = {nid: [] for nid in node_ids}

        for edge in self.workflow.edges:
            if edge.from_id in self._adj and edge.to_id in self._adj:
                self._adj[edge.from_id].append(edge.to_id)
                self._reverse_adj[edge.to_id].append(edge.from_id)

        return self._adj

    def detect_cycle(self) -> list[str] | None:
        """DFS-based cycle detection. Returns the cycle path if found, None otherwise."""
        if not self._adj:
            self.build_dag()

        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {nid: WHITE for nid in self._adj}
        parent: dict[str, str | None] = {nid: None for nid in self._adj}

        def dfs(node: str) -> list[str] | None:
            color[node] = GRAY
            for neighbor in self._adj.get(node, []):
                if color[neighbor] == GRAY:
                    # Found cycle — trace back the path
                    cycle_path = [neighbor, node]
                    current = node
                    while parent.get(current) is not None and parent[current] != neighbor:
                        current = parent[current]  # type: ignore[misc]
                        cycle_path.append(current)
                    cycle_path.reverse()
                    return cycle_path
                if color[neighbor] == WHITE:
                    parent[neighbor] = node
                    result = dfs(neighbor)
                    if result is not None:
                        return result
            color[node] = BLACK
            return None

        for node in self._adj:
            if color[node] == WHITE:
                cycle = dfs(node)
                if cycle is not None:
                    return cycle
        return None

    def topological_sort(self) -> list[str]:
        """Kahn's algorithm for topological ordering. Raises CycleDetectedError if a cycle exists."""
        if not self._adj:
            self.build_dag()

        cycle = self.detect_cycle()
        if cycle is not None:
            raise CycleDetectedError(cycle)

        in_degree: dict[str, int] = {nid: 0 for nid in self._adj}
        for node in self._adj:
            for neighbor in self._adj[node]:
                in_degree[neighbor] += 1

        queue: deque[str] = deque()
        for node, degree in sorted(in_degree.items()):
            if degree == 0:
                queue.append(node)

        # Ensure entry point comes first if it has no dependencies
        if self.workflow.entry_point in in_degree and in_degree[self.workflow.entry_point] == 0:
            queue.clear()
            queue.append(self.workflow.entry_point)
            for node in sorted(in_degree.keys()):
                if node != self.workflow.entry_point and in_degree[node] == 0:
                    queue.append(node)

        result: list[str] = []
        while queue:
            node = queue.popleft()
            result.append(node)
            for neighbor in sorted(self._adj.get(node, [])):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        return result

    def get_phase_nodes(self, phase_id: str) -> list[str]:
        """Return the node IDs belonging to *phase_id* in topological order."""
        if not self._adj:
            self.build_dag()
        topo = self.topological_sort()
        phase_node_ids = set()
        for phase in self.workflow.phases:
            if phase.id == phase_id:
                phase_node_ids = set(phase.node_ids)
                break
        return [nid for nid in topo if nid in phase_node_ids]

    def get_phase_order(self) -> list[str]:
        """Return the list of phase IDs in declaration order."""
        return [phase.id for phase in self.workflow.phases]
