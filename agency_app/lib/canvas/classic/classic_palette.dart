import 'package:flutter/material.dart';

import '../../api/models.dart';

/// The shared color definition for the Classic UI. One source of truth for the
/// 7-state node colors plus the general surface/text/edge colors, from which
/// both theme modes are derived. Structure mirrors [AbyssPalette] (FR-022):
/// identical roles, palette only.
class ClassicPalette {
  const ClassicPalette({
    required this.mode,
    required this.background,
    required this.surface,
    required this.text,
    required this.accent,
    required this.cardFill,
    required this.edge,
    required this.stateColors,
  });

  final ThemeMode mode;
  final Color background;
  final Color surface;
  final Color text;
  final Color accent;
  final Color cardFill;
  final Color edge;
  final Map<NodeStatus, Color> stateColors;

  Color colorFor(NodeStatus status) => stateColors[status]!;

  /// A desaturated copy (stale display, US5): every color is lerped toward
  /// its own luminance gray by [amount], so the scene reads as frozen while
  /// keeping its layout and state distinctions.
  ClassicPalette desaturated({double amount = 0.7}) {
    Color desat(Color c) {
      final lum = 0.299 * c.r + 0.587 * c.g + 0.114 * c.b;
      int mix(double channel) =>
          (lum + (channel - lum) * (1 - amount)).round().clamp(0, 255);
      return Color.fromARGB(mix(c.a), mix(c.r), mix(c.g), mix(c.b));
    }

    return ClassicPalette(
      mode: mode,
      background: desat(background),
      surface: desat(surface),
      text: desat(text),
      accent: desat(accent),
      cardFill: desat(cardFill),
      edge: desat(edge),
      stateColors: {
        for (final e in stateColors.entries) e.key: desat(e.value),
      },
    );
  }
}

/// Dark mode: near-black navy ground, light cards, classic blue accent,
/// amber, green, red, dim gray state roles.
const ClassicPalette _dark = ClassicPalette(
  mode: ThemeMode.dark,
  background: Color(0xFF11151C),
  surface: Color(0xFF1A212B),
  text: Color(0xFFE3EAF2),
  accent: Color(0xFF4FA3FF),
  cardFill: Color(0xFF232B36),
  edge: Color(0xFF8B98A9),
  stateColors: {
    NodeStatus.pending: Color(0xFF9AA7B8),
    NodeStatus.running: Color(0xFF4FA3FF),
    NodeStatus.awaitingRetry: Color(0xFFF5A524),
    NodeStatus.completed: Color(0xFF57C785),
    // Shares the base completed color; distinguished by the check+fb marker.
    NodeStatus.completedFallback: Color(0xFF57C785),
    NodeStatus.failed: Color(0xFFE05252),
    NodeStatus.skipped: Color(0xFF6B7280),
  },
);

/// Light mode: pale ground, white cards, deep blue accent, burnt orange,
/// emerald, dark red, gray state roles.
const ClassicPalette _light = ClassicPalette(
  mode: ThemeMode.light,
  background: Color(0xFFF4F6F9),
  surface: Color(0xFFFFFFFF),
  text: Color(0xFF1A2430),
  accent: Color(0xFF1565C0),
  cardFill: Color(0xFFFFFFFF),
  edge: Color(0xFF5B6B7C),
  stateColors: {
    NodeStatus.pending: Color(0xFF5B6B7C),
    NodeStatus.running: Color(0xFF1565C0),
    NodeStatus.awaitingRetry: Color(0xFFB26A00),
    NodeStatus.completed: Color(0xFF2E7D32),
    // Shares the base completed color; distinguished by the check+fb marker.
    NodeStatus.completedFallback: Color(0xFF2E7D32),
    NodeStatus.failed: Color(0xFFC62828),
    NodeStatus.skipped: Color(0xFF90A4AE),
  },
);

ClassicPalette darkClassic() => _dark;

ClassicPalette lightClassic() => _light;

ClassicPalette paletteFor(ThemeMode mode) => mode == ThemeMode.light ? _light : _dark;
