import '../api/models.dart';
import '../catalog/workflow_def.dart';
import '../run/run_state.dart';
import 'scene_layout.dart';
import 'state_encodings.dart';

/// A pure, deterministic snapshot of what the canvas shows for one run.
///
/// Contains node positions (from the layout) and per-node light encodings
/// (from the state machine). No viewport transform, no time-varying
/// animation — those are applied at paint time by [ScenePainter].
class CanvasScene {
  const CanvasScene({
    required this.state,
    required this.layout,
    required this.encodingsByNode,
  });

  final RunState state;
  final SceneLayout layout;
  final Map<String, StateEncoding> encodingsByNode;

  /// Convenience: the [NodeStatus] for a node, or null if absent.
  NodeStatus? statusOf(String nodeId) => state.nodes[nodeId]?.status;
}

/// Pure: scene = f(run state, workflow definition).
/// Deterministic (FR-016) — the same inputs always produce the same scene.
CanvasScene buildScene(RunState state, {required WorkflowDefinition definition}) {
  return CanvasScene(
    state: state,
    layout: computeLayout(definition, runNodeIds: state.nodes.keys),
    encodingsByNode: {
      for (final e in state.nodes.entries)
        e.key: kStateEncodings[e.value.status]!,
    },
  );
}
