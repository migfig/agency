import 'dart:math' as math;

import 'package:agencyapp/api/models.dart';
import 'package:agencyapp/canvas/classic/classic_encodings.dart';
import 'package:agencyapp/canvas/classic/classic_legend.dart';
import 'package:agencyapp/canvas/classic/classic_palette.dart';
import 'package:agencyapp/catalog/workflow_def.dart';
import 'package:agencyapp/theme/theme_data.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// T011 — the classic legend panel (FR-007): it lists all six type entries
/// (icon + name) and all seven state entries (border color sample + label +
/// glyph/motion description), is collapsible, and remains legible in both
/// themes.
void main() {
  Future<void> pumpLegend(WidgetTester tester, ThemeMode mode) =>
      tester.pumpWidget(
        MaterialApp(
          theme: themeDataFor(mode),
          home: Scaffold(body: ClassicLegend(palette: paletteFor(mode))),
        ),
      );

  group('T011 legend (US1)', () {
    testWidgets('lists all six type entries (icon + name)', (tester) async {
      await pumpLegend(tester, ThemeMode.dark);
      for (final t in NodeType.values) {
        expect(find.text(typeMarkerFor(t).name), findsOneWidget,
            reason: 'type name ${t.wire} missing');
        expect(find.byIcon(typeMarkerFor(t).icon), findsWidgets,
            reason: 'type icon for ${t.wire} missing');
      }
    });

    testWidgets('lists all seven state entries (sample + label + description)',
        (tester) async {
      await pumpLegend(tester, ThemeMode.dark);
      for (final s in NodeStatus.values) {
        final encoding = kClassicStateEncodings[s]!;
        expect(
            find.byKey(ValueKey('state-sample-${s.wire}')), findsOneWidget,
            reason: 'border color sample for ${s.wire} missing');
        expect(find.text(encoding.label), findsOneWidget,
            reason: 'state label for ${s.wire} missing');
        expect(find.text(kMarkerDescriptions[encoding.marker]!), findsOneWidget,
            reason: 'marker description for ${s.wire} missing');
      }
    });

    testWidgets('is collapsible (default open, toggles both ways)',
        (tester) async {
      await pumpLegend(tester, ThemeMode.dark);
      expect(find.text('agent'), findsOneWidget,
          reason: 'open by default');
      expect(find.byTooltip('Collapse legend'), findsOneWidget);

      await tester.tap(find.byTooltip('Collapse legend'));
      await tester.pump();
      expect(find.text('agent'), findsNothing,
          reason: 'collapsed hides the type rows');
      expect(find.text('pending'), findsNothing,
          reason: 'collapsed hides the state rows');
      expect(find.text('Legend'), findsOneWidget,
          reason: 'the header stays');
      expect(find.byTooltip('Expand legend'), findsOneWidget);

      await tester.tap(find.byTooltip('Expand legend'));
      await tester.pump();
      expect(find.text('agent'), findsOneWidget, reason: 're-expands');
      expect(find.text('pending'), findsOneWidget, reason: 're-expands');
    });

    test('remains legible in both themes (text vs panel contrast ≥ 2.0)', () {
      double channel(double v) =>
          v <= 0.03928 ? v / 12.92 : math.pow((v + 0.055) / 1.055, 2.4).toDouble();
      double luminance(Color c) =>
          0.2126 * channel(c.r) + 0.7152 * channel(c.g) + 0.0722 * channel(c.b);
      double contrast(Color a, Color b) {
        final la = luminance(a);
        final lb = luminance(b);
        final hi = la > lb ? la : lb;
        final lo = la > lb ? lb : la;
        return (hi + 0.05) / (lo + 0.05);
      }

      for (final p in [darkClassic(), lightClassic()]) {
        // The legend paints its text (palette.text) on the panel
        // (palette.surface).
        expect(contrast(p.text, p.surface), greaterThanOrEqualTo(2.0),
            reason: '${p.mode.name}: legend text must be legible on the panel');
        // State sample borders stay distinguishable on the panel.
        for (final s in NodeStatus.values) {
          expect(contrast(p.colorFor(s), p.surface), greaterThanOrEqualTo(1.4),
              reason:
                  '${p.mode.name}: ${s.wire} sample must be visible on the panel');
        }
      }
    });
  });
}
