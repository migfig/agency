import '../catalog/workflow_def.dart';

/// A grid coordinate (column = depth, row = order within the column).
class NodePosition {
  const NodePosition({required this.col, required this.row});
  final int col;
  final int row;

  @override
  bool operator ==(Object other) => other is NodePosition && other.col == col && other.row == row;

  @override
  int get hashCode => Object.hash(col, row);

  @override
  String toString() => 'NodePosition(col: $col, row: $row)';
}

class SceneLayout {
  const SceneLayout({required this.positions, required this.columnCount, required this.rowCount});
  final Map<String, NodePosition> positions;
  final int columnCount;
  final int rowCount;

  /// The position of [id], or a canonical unplaced sentinel `(-1, -1)` if the
  /// id is not part of this layout.
  NodePosition positionOf(String id) => positions[id] ?? const NodePosition(col: -1, row: -1);
}

/// Compute a deterministic scene layout for a workflow definition.
///
/// Column = longest-path depth from entry nodes (Kahn's algorithm). Within a
/// column, rows are assigned by sorted node id. Run node ids absent from the
/// definition (orphans, FR-012) are appended to a trailing column. Cycles are
/// handled safely: nodes that never reach in-degree 0 are placed in a trailing
/// column after the processed prefix.
///
/// This is a pure function of (definition identity, runNodeIds); the catalog
/// layer may memoize by (sourcePath, lastModified).
SceneLayout computeLayout(WorkflowDefinition def, {Iterable<String>? runNodeIds}) {
  final defIds = def.nodes.keys.toSet();
  final inDeg = <String, int>{for (final id in defIds) id: 0};
  final succs = <String, List<String>>{for (final id in defIds) id: <String>[]};

  for (final e in def.edges) {
    if (defIds.contains(e.fromId) && defIds.contains(e.toId)) {
      inDeg[e.toId] = inDeg[e.toId]! + 1;
      succs[e.fromId]!.add(e.toId);
    }
  }

  final col = <String, int>{};
  final queue = <String>[for (final id in defIds) if (inDeg[id]! == 0) id];
  final processed = <String>{};
  while (queue.isNotEmpty) {
    final u = queue.removeAt(0);
    if (processed.contains(u)) continue;
    processed.add(u);
    col[u] = col[u] ?? 0;
    for (final v in succs[u]!) {
      final candidate = col[u]! + 1;
      if (candidate > (col[v] ?? -1)) col[v] = candidate;
      inDeg[v] = inDeg[v]! - 1;
      if (inDeg[v]! == 0) queue.add(v);
    }
  }

  int maxProcessed = -1;
  for (final id in processed) {
    if (col[id]! > maxProcessed) maxProcessed = col[id]!;
  }
  final cycleCol = maxProcessed + 1;
  for (final id in defIds) {
    if (!col.containsKey(id)) col[id] = cycleCol;
  }

  // Orphan run node ids → trailing column after every definition node.
  int defMax = -1;
  for (final c in col.values) {
    if (c > defMax) defMax = c;
  }
  if (runNodeIds != null) {
    final orphanCol = defMax + 1;
    final orphans = {
      for (final id in runNodeIds)
        if (!defIds.contains(id)) id,
    }.toList()
      ..sort();
    for (final id in orphans) {
      col[id] = orphanCol;
    }
  }

  final positions = <String, NodePosition>{};
  final byCol = <int, List<String>>{};
  for (final e in col.entries) {
    (byCol[e.value] ??= <String>[]).add(e.key);
  }

  int rowCount = 0;
  for (final e in byCol.entries) {
    final ids = e.value..sort();
    for (var i = 0; i < ids.length; i++) {
      positions[ids[i]] = NodePosition(col: e.key, row: i);
    }
    if (ids.length > rowCount) rowCount = ids.length;
  }

  int columnCount = 0;
  for (final c in byCol.keys) {
    if (c + 1 > columnCount) columnCount = c + 1;
  }

  return SceneLayout(positions: positions, columnCount: columnCount, rowCount: rowCount);
}
