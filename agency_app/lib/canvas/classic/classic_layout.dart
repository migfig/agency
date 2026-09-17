import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../../catalog/workflow_def.dart';
import '../scene_layout.dart';
import 'classic_topology.dart';

/// World-px metrics for the classic card grid (data-model.md §2): card
/// 200×72, column gap 90, row gap 36, world padding 60.
const double kClassicCardWidth = 200;
const double kClassicCardHeight = 72;
const double kClassicColGap = 90;
const double kClassicRowGap = 36;
const double kClassicWorldPadding = 60;

/// One placed node card in world pixels.
class CardRect {
  const CardRect({
    required this.nodeId,
    required this.col,
    required this.row,
    required this.rect,
    required this.isEntry,
  });

  final String nodeId;

  /// Kahn depth from `computeLayout` (column = longest path from entries).
  final int col;

  /// id-sorted order within the column.
  final int row;

  /// The card rectangle in world pixels (already offset by the world
  /// padding and the column/row pitches).
  final Rect rect;

  /// True for the workflow's entry cards: in-degree 0 in the definition
  /// edges. Orphan and cycle nodes are never entries.
  final bool isEntry;

  @override
  bool operator ==(Object other) =>
      other is CardRect &&
      other.nodeId == nodeId &&
      other.col == col &&
      other.row == row &&
      other.rect == rect &&
      other.isEntry == isEntry;

  @override
  int get hashCode => Object.hash(nodeId, col, row, rect, isEntry);

  @override
  String toString() =>
      'CardRect($nodeId, col: $col, row: $row, rect: $rect, isEntry: $isEntry)';
}

/// One directed edge between two placed cards, with its paint anchors and
/// Bézier control point. The source anchor sits on the source card's
/// flow-side midpoint, the target anchor on the target card's flow-side
/// midpoint; [control] keeps the quadratic Bézier on the flow axis (the
/// midpoint of the two anchors along the flow axis, other axis from the
/// source).
class ClassicEdge {
  const ClassicEdge({
    required this.fromId,
    required this.toId,
    required this.sourceAnchor,
    required this.targetAnchor,
    required this.control,
  });

  final String fromId;
  final String toId;
  final Offset sourceAnchor;
  final Offset targetAnchor;

  /// Quadratic Bézier control point (on the flow axis).
  final Offset control;

  @override
  bool operator ==(Object other) =>
      other is ClassicEdge &&
      other.fromId == fromId &&
      other.toId == toId &&
      other.sourceAnchor == sourceAnchor &&
      other.targetAnchor == targetAnchor &&
      other.control == control;

  @override
  int get hashCode =>
      Object.hash(fromId, toId, sourceAnchor, targetAnchor, control);

  @override
  String toString() => 'ClassicEdge($fromId → $toId)';
}

/// The deterministic card placement for one scene: a card for every run node
/// id (orphans/cycles inherit `computeLayout`'s trailing columns) plus the
/// directed edges between placed cards.
class ClassicLayout {
  const ClassicLayout({required this.cards, required this.edges});

  /// Total over every run node id.
  final Map<String, CardRect> cards;

  /// Only edges whose both endpoints are placed.
  final List<ClassicEdge> edges;

  CardRect? cardOf(String nodeId) => cards[nodeId];

  /// The union of all card rects (zero rect when there are no cards).
  Rect get bounds {
    if (cards.isEmpty) return Rect.zero;
    var minX = double.infinity;
    var minY = double.infinity;
    var maxX = double.negativeInfinity;
    var maxY = double.negativeInfinity;
    for (final c in cards.values) {
      if (c.rect.left < minX) minX = c.rect.left;
      if (c.rect.top < minY) minY = c.rect.top;
      if (c.rect.right > maxX) maxX = c.rect.right;
      if (c.rect.bottom > maxY) maxY = c.rect.bottom;
    }
    return Rect.fromLTRB(minX, minY, maxX, maxY);
  }

  @override
  bool operator ==(Object other) =>
      other is ClassicLayout &&
      mapEquals(other.cards, cards) &&
      listEquals(other.edges, edges);

  @override
  int get hashCode => Object.hash(
      Object.hashAll(cards.keys), Object.hashAll(edges));

  @override
  String toString() =>
      'ClassicLayout(${cards.length} cards, ${edges.length} edges)';
}

/// Map the existing `computeLayout` (col, row) grid to world-px card
/// rectangles (FR-003, research R1). Pure: a function of (definition, run
/// node ids, topology) only. The topology selects how the grid's flow axis
/// (Kahn depth, `col`) and within-level axis (`row`) are oriented in world
/// px; the 200×72 card size is unchanged across topologies.
ClassicLayout buildClassicLayout(
  WorkflowDefinition def, {
  Iterable<String>? runNodeIds,
  required CanvasTopology topology,
}) {
  final grid = computeLayout(def, runNodeIds: runNodeIds);
  final defIds = def.nodes.keys.toSet();

  // In-degree over the definition edges (the edges computeLayout itself
  // uses, i.e. both endpoints in the definition).
  final inDeg = <String, int>{};
  for (final e in def.edges) {
    if (defIds.contains(e.fromId) && defIds.contains(e.toId)) {
      inDeg[e.toId] = (inDeg[e.toId] ?? 0) + 1;
    }
  }

  const pitchX = kClassicCardWidth + kClassicColGap;
  const pitchY = kClassicCardHeight + kClassicRowGap;

  var colMax = 0;
  for (final p in grid.positions.values) {
    if (p.col > colMax) colMax = p.col;
  }

  final cards = <String, CardRect>{};
  for (final e in grid.positions.entries) {
    final isEntry = defIds.contains(e.key) && (inDeg[e.key] ?? 0) == 0;
    final o = _cellOrigin(topology, e.value.col, e.value.row,
        pitchX: pitchX, pitchY: pitchY, colMax: colMax);
    cards[e.key] = CardRect(
      nodeId: e.key,
      col: e.value.col,
      row: e.value.row,
      rect: Rect.fromLTWH(
        o.dx,
        o.dy,
        kClassicCardWidth,
        kClassicCardHeight,
      ),
      isEntry: isEntry,
    );
  }

  final edges = <ClassicEdge>[];
  for (final e in def.edges) {
    final a = cards[e.fromId];
    final b = cards[e.toId];
    if (a == null || b == null) continue;
    final (sa, ta, control) = _edgeAnchors(topology, a.rect, b.rect);
    edges.add(ClassicEdge(
      fromId: e.fromId,
      toId: e.toId,
      sourceAnchor: sa,
      targetAnchor: ta,
      control: control,
    ));
  }

  return ClassicLayout(cards: cards, edges: edges);
}

/// World-px top-left origin of a `(col, row)` grid cell under [topology]
/// (the grid is offset by [kClassicWorldPadding]; the mirrored topologies
/// reflect the flow axis about [colMax]).
Offset _cellOrigin(
  CanvasTopology topology,
  int col,
  int row, {
  required double pitchX,
  required double pitchY,
  required int colMax,
}) {
  final x = switch (topology) {
    CanvasTopology.leftRight => kClassicWorldPadding + col * pitchX,
    CanvasTopology.topDown => kClassicWorldPadding + row * pitchX,
    CanvasTopology.rightLeft =>
      kClassicWorldPadding + (colMax - col) * pitchX,
    CanvasTopology.bottomUp => kClassicWorldPadding + row * pitchX,
  };
  final y = switch (topology) {
    CanvasTopology.leftRight => kClassicWorldPadding + row * pitchY,
    CanvasTopology.topDown => kClassicWorldPadding + col * pitchY,
    CanvasTopology.rightLeft => kClassicWorldPadding + row * pitchY,
    CanvasTopology.bottomUp => kClassicWorldPadding + (colMax - col) * pitchY,
  };
  return Offset(x, y);
}

/// The paint anchors + Bézier control for an edge leaving source rect [a]
/// and entering target rect [b] under [topology]: the source anchor is on
/// the source card's flow-side midpoint, the target anchor on the target
/// card's flow-side midpoint, and the control keeps the curve on the flow
/// axis (midpoint of the two anchors along the flow axis, other axis from
/// the source).
(Offset, Offset, Offset) _edgeAnchors(
  CanvasTopology topology,
  Rect a,
  Rect b,
) {
  switch (topology) {
    case CanvasTopology.leftRight:
      final s = Offset(a.right, a.center.dy);
      final g = Offset(b.left, b.center.dy);
      return (s, g, Offset((s.dx + g.dx) / 2, s.dy));
    case CanvasTopology.rightLeft:
      final s = Offset(a.left, a.center.dy);
      final g = Offset(b.right, b.center.dy);
      return (s, g, Offset((s.dx + g.dx) / 2, s.dy));
    case CanvasTopology.topDown:
      final s = Offset(a.center.dx, a.bottom);
      final g = Offset(b.center.dx, b.top);
      return (s, g, Offset(s.dx, (s.dy + g.dy) / 2));
    case CanvasTopology.bottomUp:
      final s = Offset(a.center.dx, a.top);
      final g = Offset(b.center.dx, b.bottom);
      return (s, g, Offset(s.dx, (s.dy + g.dy) / 2));
  }
}
