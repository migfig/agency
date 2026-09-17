import 'package:flutter/material.dart';

import 'abyss_palette.dart';

/// Build a full [ThemeData] for one mode from the shared [AbyssPalette].
ThemeData themeDataFor(ThemeMode mode) {
  final p = paletteFor(mode);
  final scheme = mode == ThemeMode.light
      ? ColorScheme.fromSeed(
          seedColor: p.accent,
          brightness: Brightness.light,
          primary: p.accent,
          onSurface: p.text,
        )
      : ColorScheme.fromSeed(
          seedColor: p.accent,
          brightness: Brightness.dark,
          primary: p.accent,
          onSurface: p.text,
        );
  return ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    brightness: scheme.brightness,
    scaffoldBackgroundColor: p.background,
    cardTheme: CardThemeData(
      color: p.surface,
    ),
  );
}
