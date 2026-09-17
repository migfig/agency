import 'package:flutter/material.dart';

import '../api/models.dart';

/// The shared color definition for the Abyss UI. One source of truth for the
/// 7-state node colors plus the general surface/text colors, from which both
/// theme modes are derived (T012).
class AbyssPalette {
  const AbyssPalette({
    required this.mode,
    required this.background,
    required this.surface,
    required this.text,
    required this.accent,
    required this.stateColors,
  });

  final ThemeMode mode;
  final Color background;
  final Color surface;
  final Color text;
  final Color accent;
  final Map<NodeStatus, Color> stateColors;

  Color colorFor(NodeStatus status) => stateColors[status]!;

  /// A desaturated copy (stale display, US5): every color is lerped toward
  /// its own luminance gray by [amount], so the scene reads as frozen while
  /// keeping its layout and state distinctions.
  AbyssPalette desaturated({double amount = 0.7}) {
    Color desat(Color c) {
      final lum = 0.299 * c.r + 0.587 * c.g + 0.114 * c.b;
      int mix(double channel) =>
          (lum + (channel - lum) * (1 - amount)).round().clamp(0, 255);
      return Color.fromARGB(mix(c.a), mix(c.r), mix(c.g), mix(c.b));
    }

    return AbyssPalette(
      mode: mode,
      background: desat(background),
      surface: desat(surface),
      text: desat(text),
      accent: desat(accent),
      stateColors: {
        for (final e in stateColors.entries) e.key: desat(e.value),
      },
    );
  }
}

/// Dark mode: pale/bright cyan family, amber, warm green, red, dim gray.
const AbyssPalette _dark = AbyssPalette(
  mode: ThemeMode.dark,
  background: Color(0xFF0A1014),
  surface: Color(0xFF10181E),
  text: Color(0xFFD7E4EA),
  accent: Color(0xFF00E5FF),
  stateColors: {
    NodeStatus.pending: Color(0xFF9FEAF2),
    NodeStatus.running: Color(0xFF00E5FF),
    NodeStatus.awaitingRetry: Color(0xFFFFC400),
    NodeStatus.completed: Color(0xFF7DD87D),
    // Shares the base completed color; distinguished by the halo shape.
    NodeStatus.completedFallback: Color(0xFF7DD87D),
    NodeStatus.failed: Color(0xFFFF5252),
    NodeStatus.skipped: Color(0xFF6B7280),
  },
);

/// Light mode: steel/deep blue family, burnt orange, emerald, dark red, gray.
const AbyssPalette _light = AbyssPalette(
  mode: ThemeMode.light,
  background: Color(0xFFF2F6F8),
  surface: Color(0xFFFFFFFF),
  text: Color(0xFF102028),
  accent: Color(0xFF0D47A1),
  stateColors: {
    NodeStatus.pending: Color(0xFF4682B4),
    NodeStatus.running: Color(0xFF0D47A1),
    NodeStatus.awaitingRetry: Color(0xFFCC5500),
    NodeStatus.completed: Color(0xFF2E7D32),
    // Shares the base completed color; distinguished by the halo shape.
    NodeStatus.completedFallback: Color(0xFF2E7D32),
    NodeStatus.failed: Color(0xFFB71C1C),
    // Darkened from Blue Grey 200 so the thin static ring survives the pale sea.
    NodeStatus.skipped: Color(0xFF90A4AE),
  },
);

AbyssPalette darkAbyss() => _dark;

AbyssPalette lightAbyss() => _light;

AbyssPalette paletteFor(ThemeMode mode) => mode == ThemeMode.light ? _light : _dark;
