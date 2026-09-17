import 'package:flutter/foundation.dart';

import '../../api/models.dart';
import '../../catalog/workflow_def.dart';
import '../../run/run_state.dart';
import 'classic_encodings.dart';
import 'classic_layout.dart';
import 'classic_topology.dart';

/// A pure, deterministic snapshot of what the classic canvas shows for one
/// run.
///
/// Contains the card layout (world-px rects + directed edges), the per-node
/// state encodings, and the source state/definition. No viewport transform,
/// no time-varying animation — those are applied at paint time by the
/// `ClassicPainter` (the view owns the ticker, mirroring `CanvasView`).
class ClassicScene {
  const ClassicScene({
    required this.runId,
    required this.state,
    required this.definition,
    required this.layout,
    required this.encodings,
    required this.topology,
  });

  final String runId;
  final RunState state;
  final WorkflowDefinition definition;
  final ClassicLayout layout;

  /// Per-node state encoding, total over [RunState.nodes].
  final Map<String, ClassicStateEncoding> encodings;

  /// The flow topology the [layout] was built under.
  final CanvasTopology topology;

  /// Convenience: the [NodeStatus] for a node, or null if absent.
  NodeStatus? statusOf(String nodeId) => state.nodes[nodeId]?.status;

  @override
  bool operator ==(Object other) =>
      other is ClassicScene &&
      other.runId == runId &&
      other.state == state &&
      other.layout == layout &&
      other.topology == topology &&
      mapEquals(other.encodings, encodings);

  @override
  int get hashCode => Object.hash(
      runId, state, layout, topology, Object.hashAll(encodings.keys));

  @override
  String toString() =>
      'ClassicScene($runId, ${layout.cards.length} cards, ${layout.edges.length} edges)';
}

/// Pure: scene = f(run state, workflow definition, topology).
/// Deterministic (FR-020) — the same inputs always produce the same scene;
/// no wall clock, no randomness.
ClassicScene buildClassicScene(
  RunState state, {
  required WorkflowDefinition definition,
  required CanvasTopology topology,
}) {
  return ClassicScene(
    runId: state.runId,
    state: state,
    definition: definition,
    topology: topology,
    layout: buildClassicLayout(
      definition,
      runNodeIds: state.nodes.keys,
      topology: topology,
    ),
    encodings: {
      for (final e in state.nodes.entries)
        e.key: classicStateEncodingFor(e.value.status),
    },
  );
}
