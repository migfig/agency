import 'dart:math' as math;

import 'package:agencyapp/api/models.dart';
import 'package:agencyapp/canvas/classic/classic_painter.dart';
import 'package:agencyapp/canvas/classic/classic_topology.dart';
import 'package:agencyapp/canvas/classic/classic_view.dart';
import 'package:agencyapp/catalog/workflow_def.dart';
import 'package:agencyapp/run/run_store.dart';
import 'package:agencyapp/theme/theme_data.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// The orbiting comet on running cards (FR-008): the comet head phase, the
/// rounded-rect border walk it orbits, and a widget smoke that a running
/// scene keeps painting as the ticker advances.
void main() {
  // Card metrics from the contract: 200×72 with kCardRadius 8.
  const double w = 200, h = 72, r = 8;
  const double top = 184; // w - 2r
  const double side = 56; // h - 2r
  const double corner = math.pi * 8 / 2;

  group('comet head phase', () {
    test('is in [0,1), zero at t=0, and periodic with kCometPeriod', () {
      expect(cometHeadPhase(0.0), 0.0);
      for (var i = 1; i < 64; i++) {
        expect(cometHeadPhase(i * 0.137), inInclusiveRange(0.0, 1.0));
      }
      for (final t in [0.0, 0.31, 0.999, 12.7]) {
        expect(cometHeadPhase(t + kCometPeriod),
            closeTo(cometHeadPhase(t), 1e-9));
        expect(cometHeadPhase(t + 3 * kCometPeriod),
            closeTo(cometHeadPhase(t), 1e-9));
      }
    });

    test('advances strictly monotonically within one lap', () {
      var prev = cometHeadPhase(0.0);
      for (var i = 1; i < 100; i++) {
        final p = cometHeadPhase(i * 0.01);
        expect(p, greaterThan(prev), reason: 'monotone within a lap');
        prev = p;
      }
    });
  });

  group('rounded-rect border walk', () {
    test('perimeter matches the closed form and clamps the radius', () {
      expect(rrectPerimeter(w, h, r),
          closeTo(2 * (w + h) - 8 * r + 2 * math.pi * r, 1e-9));
      // A requested radius larger than half the smaller side clamps.
      expect(rrectPerimeter(10, 10, 8),
          closeTo(2 * 20 - 8 * 5 + 2 * math.pi * 5, 1e-9));
    });

    test('starts at the top edge and walks clockwise through every corner',
        () {
      Offset at(double s) => rrectPointAt(s, w, h, r);
      // Corner-boundary points come out of trig with ~ulp error, so compare
      // with a tolerance.
      void expectAt(double s, double x, double y, [String? why]) {
        final pt = at(s);
        expect(pt.dx, closeTo(x, 1e-9), reason: 's=$s${why == null ? '' : ' — $why'}');
        expect(pt.dy, closeTo(y, 1e-9), reason: 's=$s${why == null ? '' : ' — $why'}');
      }
      expectAt(0, 8, 0);
      expectAt(top, 192, 0);
      expectAt(top + corner, 200, 8);
      expectAt(top + corner + side, 200, 64);
      expectAt(top + 2 * corner + side, 192, 72);
      expectAt(top + 2 * corner + side + top, 8, 72);
      expectAt(top + 3 * corner + side + top, 0, 64);
      expectAt(top + 3 * corner + 2 * side + top, 0, 8);
      expectAt(rrectPerimeter(w, h, r), 8, 0,
          'the walk is closed: one full perimeter returns to the start');
    });

    test('the walk is continuous and stays on the border', () {
      final p = rrectPerimeter(w, h, r);
      const n = 720;
      var prev = rrectPointAt(0, w, h, r);
      for (var i = 1; i <= n; i++) {
        final pt = rrectPointAt(p * i / n, w, h, r);
        expect((pt - prev).distance, lessThan(0.8),
            reason: 'no jumps between consecutive arc-length steps');
        expect(onBorder(pt, w, h, r), isTrue,
            reason: 's=${(p * i / n).toStringAsFixed(2)} is on the border');
        prev = pt;
      }
    });

    test('negative arc length wraps around the perimeter', () {
      final p = rrectPerimeter(w, h, r);
      expect(rrectPointAt(-1, w, h, r), rrectPointAt(p - 1, w, h, r));
    });
  });

  group('comet smoke', () {
    testWidgets('a running scene keeps painting as the ticker advances',
        (tester) async {
      final def = parseWorkflowDefinition('''
name: comet
nodes:
  a: {id: a, type: agent, model: draft}
  b: {id: b, type: agent, model: draft}
edges:
  - {from_id: a, to_id: b}
''');
      final store = RunStore(workflowDefinition: def)
        ..seed(RunStatusView.fromJson({
          'run_id': 'run-comet',
          'workflow_name': 'comet',
          'state': 'running',
          'started_at': DateTime.now().toUtc().toIso8601String(),
          'nodes': <String, Object>{
            'a': {'status': 'running', 'attempt': 1, 'model': 'draft'},
          },
          'pending_inputs': <Object>[],
        }));

      await tester.pumpWidget(MaterialApp(
        theme: themeDataFor(ThemeMode.dark),
        home: Scaffold(
            body: ClassicCanvasView(
                store: store,
                definition: def,
                topology: CanvasTopology.leftRight)),
      ));
      await tester.pump(); // post-frame zoom-to-fit

      // Advance past several comet laps; paint must not throw.
      for (var i = 0; i < 4; i++) {
        await tester.pump(const Duration(milliseconds: 500));
      }
      final painter = tester
              .widget<CustomPaint>(find.descendant(
                      of: find.byType(ClassicCanvasView),
                      matching: find.byType(CustomPaint)).first)
              .painter as ClassicPainter;
      expect(painter.clock, greaterThan(0),
          reason: 'the ticker feeds the painter a live clock');
    });
  });
}

/// True when [p] sits on the border of a [w]×[h] rounded rect of radius
/// [r]: on one of the four straight edges, or exactly [r] from a corner
/// center.
bool onBorder(Offset p, double w, double h, double r) {
  const eps = 1e-3;
  if (p.dx < -eps || p.dy < -eps || p.dx > w + eps || p.dy > h + eps) {
    return false;
  }
  if (p.dy < eps || p.dy > h - eps || p.dx < eps || p.dx > w - eps) {
    return true;
  }
  for (final c in [
    Offset(r, r),
    Offset(w - r, r),
    Offset(w - r, h - r),
    Offset(r, h - r),
  ]) {
    if (((p - c).distance - r).abs() < 1e-2) return true;
  }
  return false;
}
