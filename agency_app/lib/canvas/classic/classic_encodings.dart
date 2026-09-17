import 'package:flutter/material.dart';

import '../../api/models.dart';
import '../../catalog/workflow_def.dart';

/// Non-color visual channel of a node state (glyph / motion / dimming).
/// Markers alone are pairwise distinct across the 7 states so the encoding
/// survives even with color removed (FR-009).
enum ClassicMarker {
  none, // pending
  pulse, // running (pulsing border alpha)
  countdown, // awaiting_retry (ticking "retry in Ns" text)
  check, // completed (check glyph)
  checkFb, // completed_fallback (check glyph + persistent fb chip)
  cross, // failed (cross glyph)
  dim, // skipped (dimmed card, no motion)
}

/// A theme-independent label+marker pairing for one node state.
///
/// The [label] for `awaiting_retry` is a template: the painter computes the
/// live `retry in Ns` text from `SNodeState.nextRetryAt` and the ticker clock.
class ClassicStateEncoding {
  const ClassicStateEncoding({
    required this.label,
    required this.marker,
    required this.animated,
  });

  final String label;
  final ClassicMarker marker;
  final bool animated;

  @override
  String toString() => 'ClassicStateEncoding($label, $marker, $animated)';
}

/// The 7-state encoding table (contracts/classic-canvas.md). Label+marker
/// alone distinguish all seven states; border colors are supplied separately
/// by `ClassicPalette.stateColors` (same seven roles as the Abyss palette).
const Map<NodeStatus, ClassicStateEncoding> kClassicStateEncodings = {
  NodeStatus.pending:
      ClassicStateEncoding(label: 'pending', marker: ClassicMarker.none, animated: false),
  NodeStatus.running:
      ClassicStateEncoding(label: 'running', marker: ClassicMarker.pulse, animated: true),
  NodeStatus.awaitingRetry:
      ClassicStateEncoding(label: 'retry in Ns', marker: ClassicMarker.countdown, animated: true),
  NodeStatus.completed:
      ClassicStateEncoding(label: 'completed', marker: ClassicMarker.check, animated: false),
  NodeStatus.completedFallback:
      ClassicStateEncoding(label: 'fallback', marker: ClassicMarker.checkFb, animated: false),
  NodeStatus.failed:
      ClassicStateEncoding(label: 'failed', marker: ClassicMarker.cross, animated: false),
  NodeStatus.skipped:
      ClassicStateEncoding(label: 'skipped', marker: ClassicMarker.dim, animated: false),
};

ClassicStateEncoding classicStateEncodingFor(NodeStatus status) =>
    kClassicStateEncodings[status]!;

/// A theme-independent type marker: icon (primary non-color channel, FR-006)
/// plus the type name as text (matches the `NodeType.wire` vocabulary).
class TypeMarker {
  const TypeMarker({required this.icon, required this.name});

  final IconData icon;
  final String name;

  @override
  String toString() => 'TypeMarker($icon, $name)';
}

/// The 6-type marker table, total over `NodeType`.
const Map<NodeType, TypeMarker> kTypeMarkers = {
  NodeType.agent: TypeMarker(icon: Icons.smart_toy, name: 'agent'),
  NodeType.conditional: TypeMarker(icon: Icons.rule, name: 'conditional'),
  NodeType.merge: TypeMarker(icon: Icons.call_split, name: 'merge'),
  NodeType.broadcast: TypeMarker(icon: Icons.cast, name: 'broadcast'),
  NodeType.humanInLoop: TypeMarker(icon: Icons.person, name: 'human_in_loop'),
  NodeType.toolCall: TypeMarker(icon: Icons.build, name: 'tool_call'),
};

TypeMarker typeMarkerFor(NodeType type) => kTypeMarkers[type]!;
