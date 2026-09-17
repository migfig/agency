import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;

import 'package:agencyapp/api/agency_client.dart';
import 'package:agencyapp/api/models.dart';
import 'package:agencyapp/app/app_state.dart';
import 'package:agencyapp/app/home_page.dart';
import 'package:agencyapp/canvas/canvas_scene.dart';
import 'package:agencyapp/canvas/canvas_view.dart';
import 'package:agencyapp/canvas/scene_painter.dart';
import 'package:agencyapp/canvas/state_encodings.dart';
import 'package:agencyapp/catalog/catalog_entry.dart';
import 'package:agencyapp/catalog/workflow_def.dart';
import 'package:agencyapp/theme/abyss_palette.dart';
import 'package:agencyapp/theme/theme_data.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// T036 — theme tests (US3, SC-005, FR-018/FR-019).
///
/// Mirrors `main.dart`'s wiring (ListenableBuilder -> MaterialApp with
/// `theme: themeDataFor(app.themeMode)`) against faked prefs and a scripted
/// fake [AgencyClient], then proves:
///   (a) first launch with no stored `theme_mode` is dark;
///   (b) the top-bar toggle restyles the whole app (MaterialApp theme AND the
///       abyss scene palette) while the same RunState keeps driving the scene,
///       and the choice is persisted;
///   (c) a restart with the stored pref restores the last chosen theme.
///
/// T038's legibility gate (state colors vs the sea) lives here too so the
/// light palette is verified, not just eyeballed.
void main() {
  const ts = '2026-08-29T10:00:00+00:00';
  const runId = 'run-1';

  WorkflowDefinition demoDef() => parseWorkflowDefinition('''
name: demo
nodes:
  a: {id: a, type: agent, model: draft, retry: {max_attempts: 3, backoff: exponential, base_delay_seconds: 1}}
  b: {id: b, type: agent, model: draft, fallback: fb}
  c: {id: c, type: tool_call, tool: echo}
  d: {id: d, type: conditional}
''');

  EventFrame frame(String type, int seq, Map<String, Object> payload) =>
      decodeEventFrame({
        'event_type': type,
        'seq': seq,
        'timestamp': ts,
        'run_id': runId,
        'payload': payload,
      })!;

  Map<String, Object> runningSnapshot() => {
        'run_id': runId,
        'workflow_name': 'demo',
        'state': 'running',
        'started_at': ts,
        'nodes': <String, Object>{
          'a': {'status': 'running', 'attempt': 1, 'model': 'draft'},
        },
        'pending_inputs': <Object>[],
      };

  MockClient fakeClient() => MockClient((request) async {
        final path = request.url.path;
        final headers = <String, String>{'content-type': 'application/json'};
        if (request.method == 'GET' && path == '/runs') {
          return http.Response(
            jsonEncode([
              {
                'run_id': runId,
                'workflow_name': 'demo',
                'started_at': ts,
                'state': 'running',
              }
            ]),
            200,
            headers: headers,
          );
        }
        if (request.method == 'GET' && path == '/runs/$runId') {
          return http.Response(jsonEncode(runningSnapshot()), 200,
              headers: headers);
        }
        if (request.method == 'GET' && path == '/health') {
          return http.Response(jsonEncode({'status': 'ok'}), 200,
              headers: headers);
        }
        return http.Response(
          jsonEncode({'error': {'code': 'unknown_run', 'message': 'nope'}}),
          404,
          headers: headers,
        );
      });

  /// Pumps frames with elapsed time so the default 200ms MaterialApp theme
  /// crossfade completes (and fake-client microtasks flush).
  Future<void> settle(WidgetTester tester) async {
    for (var i = 0; i < 3; i++) {
      await tester.pump(const Duration(milliseconds: 250));
    }
  }

  /// The real app wiring from `main.dart`, minus the filesystem catalog and
  /// network event source (both faked, as in the other widget tests).
  Future<void> pumpAgency(WidgetTester tester,
      {required AppState app,
      required AgencyClient client,
      required CatalogEntry entry,
      required StreamController<EventFrame> events}) async {
    await tester.pumpWidget(
      ListenableBuilder(
        listenable: app,
        builder: (context, _) => MaterialApp(
          title: 'Agency',
          debugShowCheckedModeBanner: false,
          theme: themeDataFor(app.themeMode),
          home: HomePage(
            app: app,
            client: client,
            entries: [entry],
            eventStream: events.stream,
          ),
        ),
      ),
    );
  }

  Brightness appBrightness(WidgetTester tester) =>
      Theme.of(tester.element(find.byType(HomePage))).brightness;

  CanvasScene sceneOf(WidgetTester tester) =>
      tester.state<CanvasViewState>(find.byType(CanvasView)).scene!;

  ScenePainter painterOf(WidgetTester tester) {
    final paint = tester.widget<CustomPaint>(
        find.descendant(
            of: find.byType(CanvasView),
            matching: find.byType(CustomPaint)).first);
    return paint.painter as ScenePainter;
  }

  group('T036 theme (US3)', () {
    testWidgets('(a) first launch with no stored theme_mode is dark',
        (tester) async {
      SharedPreferences.setMockInitialValues(const {});
      final app = AppState();
      await app.init();
      expect(app.themeMode, ThemeMode.dark,
          reason: 'no stored theme_mode defaults to dark');

      final events = StreamController<EventFrame>.broadcast();
      final client = AgencyClient('http://fake', httpClient: fakeClient());
      final entry = CatalogEntry(
          path: 'demo.yaml',
          name: 'demo',
          definition: demoDef());

      await pumpAgency(
          tester, app: app, client: client, entry: entry, events: events);
      await settle(tester);

      expect(appBrightness(tester), Brightness.dark);
      expect(painterOf(tester).palette, same(darkAbyss()),
          reason: 'the scene paints with the dark palette on first launch');
    });

    testWidgets('(b) the toggle restyles the app and the scene; the same '
        'RunState keeps driving the scene', (tester) async {
      SharedPreferences.setMockInitialValues(const {});
      final app = AppState();
      await app.init();

      final events = StreamController<EventFrame>.broadcast();
      final client = AgencyClient('http://fake', httpClient: fakeClient());
      final entry = CatalogEntry(
          path: 'demo.yaml',
          name: 'demo',
          definition: demoDef());

      await pumpAgency(
          tester, app: app, client: client, entry: entry, events: events);
      await settle(tester);

      expect(app.themeMode, ThemeMode.dark);
      expect(appBrightness(tester), Brightness.dark);
      expect(painterOf(tester).palette, same(darkAbyss()));
      expect(sceneOf(tester).encodingsByNode['a'],
          kStateEncodings[NodeStatus.running],
          reason: 'the run is live before the toggle');

      // Toggle the theme.
      await tester.tap(find.byTooltip('Toggle theme'));
      await settle(tester);

      expect(app.themeMode, ThemeMode.light,
          reason: 'the toggle flips the stored theme mode');
      expect(appBrightness(tester), Brightness.light,
          reason: 'the MaterialApp re-themes immediately');
      expect(painterOf(tester).palette, same(lightAbyss()),
          reason: 'the abyss repaints with the daylight palette');
      expect(sceneOf(tester).encodingsByNode['a'],
          kStateEncodings[NodeStatus.running],
          reason: 'the same RunState keeps driving the scene (no restart)');

      // The in-flight run keeps updating after the theme change.
      events.add(frame('node_completed', 1,
          {'node_id': 'a', 'attempt': 1, 'tokens_used': 5, 'duration_seconds': 1.0}));
      await settle(tester);
      expect(sceneOf(tester).encodingsByNode['a'],
          kStateEncodings[NodeStatus.completed],
          reason: 'frames still move the scene in the new theme');
      expect(painterOf(tester).palette, same(lightAbyss()));

      // The choice is persisted for the next launch.
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString('theme_mode'), 'light',
          reason: 'theme_mode is written to shared_preferences');
    });

    testWidgets('(c) restart with stored theme_mode=light restores light',
        (tester) async {
      SharedPreferences.setMockInitialValues(const {'theme_mode': 'light'});
      final app = AppState();
      await app.init();
      expect(app.themeMode, ThemeMode.light,
          reason: 'the stored preference wins over the dark default');

      final events = StreamController<EventFrame>.broadcast();
      final client = AgencyClient('http://fake', httpClient: fakeClient());
      final entry = CatalogEntry(
          path: 'demo.yaml',
          name: 'demo',
          definition: demoDef());

      await pumpAgency(
          tester, app: app, client: client, entry: entry, events: events);
      await settle(tester);

      expect(appBrightness(tester), Brightness.light);
      expect(painterOf(tester).palette, same(lightAbyss()));
    });

    testWidgets('(c) restart with stored theme_mode=dark restores dark',
        (tester) async {
      SharedPreferences.setMockInitialValues(const {'theme_mode': 'dark'});
      final app = AppState();
      await app.init();
      expect(app.themeMode, ThemeMode.dark);

      final events = StreamController<EventFrame>.broadcast();
      final client = AgencyClient('http://fake', httpClient: fakeClient());
      final entry = CatalogEntry(
          path: 'demo.yaml',
          name: 'demo',
          definition: demoDef());

      await pumpAgency(
          tester, app: app, client: client, entry: entry, events: events);
      await settle(tester);

      expect(appBrightness(tester), Brightness.dark);
      expect(painterOf(tester).palette, same(darkAbyss()));
    });
  });

  group('T038 light-theme legibility', () {
    double channel(double v) {
      final lin = v <= 0.03928 ? v / 12.92 : math.pow((v + 0.055) / 1.055, 2.4).toDouble();
      return lin;
    }

    double luminance(Color c) =>
        0.2126 * channel(c.r) + 0.7152 * channel(c.g) + 0.0722 * channel(c.b);

    double contrast(Color a, Color b) {
      final la = luminance(a);
      final lb = luminance(b);
      final hi = la > lb ? la : lb;
      final lo = la > lb ? lb : la;
      return (hi + 0.05) / (lo + 0.05);
    }

    test('every state color and the strands stay legible against the sea '
        'in both themes', () {
      // Threshold: a 1px stroke must survive on a busy animated background;
      // WCAG's 4.5 body-text ratio is not the right bar for scene chrome,
      // but 2.0 is the floor below which a state hue starts to vanish.
      const minContrast = 2.0;
      for (final p in [darkAbyss(), lightAbyss()]) {
        for (final s in NodeStatus.values) {
          expect(contrast(p.colorFor(s), p.background), greaterThanOrEqualTo(minContrast),
              reason: '${p.mode.name}: ${s.wire} legible against the sea');
        }
        expect(contrast(p.accent, p.background), greaterThanOrEqualTo(minContrast),
            reason: '${p.mode.name}: strands legible against the sea');
      }
    });
  });
}
