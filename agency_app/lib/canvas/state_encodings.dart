import '../api/models.dart';

/// Visual shape of a node light. Shapes alone are pairwise distinct across the
/// 7 states so the encoding survives even with color removed.
enum LightShape {
  hollowRing, // pending
  filledDisc, // running
  dashedDisc, // awaiting_retry
  filledDiscSmall, // completed
  haloDisc, // completed_fallback
  crossDisc, // failed
  thinHollowRing, // skipped
}

/// Motion of a node light. Motions alone are pairwise distinct across the 7
/// states.
enum PulseMotion {
  breathing, // pending
  pulsingGlow, // running
  jitterFlicker, // awaiting_retry
  steadyGlow, // completed
  haloShimmer, // completed_fallback
  hardFlicker, // failed
  still, // skipped
}

/// A theme-independent shape+motion pairing for one node state.
class StateEncoding {
  const StateEncoding(this.shape, this.motion);
  final LightShape shape;
  final PulseMotion motion;

  @override
  String toString() => 'StateEncoding($shape, $motion)';
}

/// The 7-state encoding table (ui.md). Shape+motion alone distinguish all
/// seven states; colors are supplied separately by the theme palette.
const Map<NodeStatus, StateEncoding> kStateEncodings = {
  NodeStatus.pending: StateEncoding(LightShape.hollowRing, PulseMotion.breathing),
  NodeStatus.running: StateEncoding(LightShape.filledDisc, PulseMotion.pulsingGlow),
  NodeStatus.awaitingRetry: StateEncoding(LightShape.dashedDisc, PulseMotion.jitterFlicker),
  NodeStatus.completed: StateEncoding(LightShape.filledDiscSmall, PulseMotion.steadyGlow),
  NodeStatus.completedFallback: StateEncoding(LightShape.haloDisc, PulseMotion.haloShimmer),
  NodeStatus.failed: StateEncoding(LightShape.crossDisc, PulseMotion.hardFlicker),
  NodeStatus.skipped: StateEncoding(LightShape.thinHollowRing, PulseMotion.still),
};

StateEncoding encodingFor(NodeStatus status) => kStateEncodings[status]!;

/// A transient pulse sample for a running/animated light.
class PulseEffect {
  const PulseEffect({required this.phase, required this.intensity});
  final double phase; // 0..1 cycle position
  final double intensity; // 0..1
}
