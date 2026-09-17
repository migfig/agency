import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;

import 'package:agencyapp/api/agency_client.dart';
import 'package:agencyapp/api/models.dart';
import 'package:agencyapp/app/app_state.dart';
import 'package:agencyapp/app/home_page.dart';
import 'package:agencyapp/canvas/canvas_view.dart';
import 'package:agencyapp/canvas/classic/classic_encodings.dart';
import 'package:agencyapp/canvas/hil_prompt.dart';
import 'package:agencyapp/canvas/inspector.dart';
import 'package:agencyapp/canvas/classic/classic_layout.dart';
import 'package:agencyapp/canvas/classic/classic_legend.dart';
import 'package:agencyapp/canvas/classic/classic_palette.dart';
import 'package:agencyapp/canvas/classic/classic_painter.dart';
import 'package:agencyapp/canvas/classic/classic_scene.dart';
import 'package:agencyapp/canvas/classic/classic_topology.dart';
import 'package:agencyapp/canvas/classic/classic_view.dart';
import 'package:agencyapp/canvas/summary_bar.dart';
import 'package:agencyapp/canvas/zoom_controls.dart';
import 'package:agencyapp/catalog/catalog_entry.dart';
import 'package:agencyapp/catalog/workflow_def.dart';
import 'package:agencyapp/run/run_state.dart';
import 'package:agencyapp/run/run_store.dart';
import 'package:agencyapp/theme/theme_data.dart';
import 'package:flutter/gestures.dart' show PointerDeviceKind, PointerScrollEvent;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// T012 — initial classic view widget tests (US1 acceptance scenarios 1–4):
/// pumping `ClassicCanvasView` with a settled run's `RunState` renders one
/// card per step, directed edges with arrowheads for every placed dependency,
/// entry markers on the in-degree-0 cards, and the legend panel.
void main() {
  const runId = 'run-1';

  // A recent-past start (10 min before the real wall clock): keeps
  // SummaryBar's wall-clock elapsed short (no flex overflow under the test
  // font) while staying in the past for the countdown assertions.
  final tsDate = DateTime.now().toUtc().subtract(const Duration(minutes: 10));
  final ts = tsDate.toIso8601String();

  WorkflowDefinition def() => parseWorkflowDefinition('''
name: settled
nodes:
  a: {id: a, type: agent, model: draft}
  b: {id: b, type: agent, model: draft, fallback: fb}
  c: {id: c, type: tool_call, tool: echo}
  d: {id: d, type: conditional}
  e: {id: e, type: agent, model: draft}
edges:
  - {from_id: a, to_id: b}
  - {from_id: b, to_id: c}
  - {from_id: a, to_id: d}
  - {from_id: d, to_id: e}
''');

  Map<String, Object> settledSnapshot() => {
        'run_id': runId,
        'workflow_name': 'settled',
        'state': 'completed',
        'started_at': ts,
        'nodes': <String, Object>{
          'a': {'status': 'completed', 'attempt': 1, 'tokens': 12, 'duration_seconds': 1.5},
          'b': {'status': 'completed', 'attempt': 1, 'fallback': 'fb', 'tokens': 30, 'duration_seconds': 2.0},
          'c': {'status': 'skipped', 'attempt': 1, 'reason': 'unresolved_binding'},
          'd': {'status': 'skipped', 'attempt': 1, 'reason': 'condition false'},
          'e': {'status': 'skipped', 'attempt': 1, 'reason': 'unresolved_binding'},
        },
        'pending_inputs': <Object>[],
      };

  Future<void> pumpView(WidgetTester tester,
      {required ClassicCanvasView view, ThemeMode mode = ThemeMode.dark}) =>
      tester.pumpWidget(
        MaterialApp(
          theme: themeDataFor(mode),
          home: Scaffold(body: view),
        ),
      );

  ClassicCanvasViewState viewState(WidgetTester tester) =>
      tester.state<ClassicCanvasViewState>(find.byType(ClassicCanvasView));

  // ---- US2 helpers (T019–T023) ------------------------------------------

  /// A 3-node, 3-edge definition: a → b → c plus the skip edge a → c, so
  /// every edge touches a distinct pair of nodes.
  WorkflowDefinition triDef() => parseWorkflowDefinition('''
name: tri
nodes:
  a: {id: a, type: agent, model: draft}
  b: {id: b, type: agent, model: draft}
  c: {id: c, type: agent, model: draft}
edges:
  - {from_id: a, to_id: b}
  - {from_id: a, to_id: c}
  - {from_id: b, to_id: c}
''');

  EventFrame frame(String type, int seq, Map<String, Object> payload) =>
      EventFrame(
        eventType: type,
        seq: seq,
        timestamp: tsDate,
        runId: runId,
        payload: payload,
      );

  ClassicEdge edge(ClassicScene scene, String from, String to) =>
      scene.layout.edges.firstWhere((e) => e.fromId == from && e.toId == to);

  ClassicPainter painterOf(WidgetTester tester) =>
      tester
          .widget<CustomPaint>(find.descendant(
              of: find.byType(ClassicCanvasView),
              matching: find.byType(CustomPaint)).first)
          .painter as ClassicPainter;

  Future<void> settle(WidgetTester tester) async {
    for (var i = 0; i < 3; i++) {
      await tester.pump();
    }
  }

  Future<void> pumpHome(WidgetTester tester,
      {required AppState app,
      required AgencyClient client,
      required CatalogEntry entry,
      required StreamController<EventFrame> events}) async {
    await tester.pumpWidget(MaterialApp(
      theme: themeDataFor(ThemeMode.dark),
      home: HomePage(
        app: app,
        client: client,
        entries: [entry],
        eventStream: events.stream,
      ),
    ));
  }

  Map<String, Object> completedSnapshot() => {
        'run_id': runId,
        'workflow_name': 'tri',
        'state': 'completed',
        'started_at': ts,
        'nodes': <String, Object>{
          'a': {'status': 'completed', 'attempt': 1, 'tokens': 100, 'duration_seconds': 1.0},
          'b': {'status': 'completed_fallback', 'attempt': 1, 'fallback': 'fb', 'model': 'fb', 'tokens': 20, 'duration_seconds': 0.5},
        },
        'pending_inputs': <Object>[],
      };

  /// A server history listing [runId] as a finished run (US3): the app
  /// auto-selects it and seeds the store from `GET /runs/{id}` +
  /// `GET /runs/{id}/result` alone.
  MockClient historyClassicClient() => MockClient((request) async {
        final path = request.url.path;
        final headers = <String, String>{'content-type': 'application/json'};
        if (request.method == 'GET' && path == '/runs') {
          return http.Response(
            jsonEncode([
              {
                'run_id': runId,
                'workflow_name': 'tri',
                'started_at': ts,
                'state': 'completed',
              },
            ]),
            200,
            headers: headers,
          );
        }
        if (request.method == 'GET' && path == '/runs/$runId') {
          return http.Response(jsonEncode(completedSnapshot()), 200,
              headers: headers);
        }
        if (request.method == 'GET' && path == '/runs/$runId/result') {
          return http.Response(
            jsonEncode({
              'run_id': runId,
              'status': 'completed',
              'total_tokens': 120,
              'duration_seconds': 2.0,
              'nodes': <Object>[
                {
                  'node_id': 'a',
                  'status': 'completed',
                  'attempt': 1,
                  'tokens': 100,
                  'duration_seconds': 1.0,
                  'output': 'alpha',
                },
                {
                  'node_id': 'b',
                  'status': 'completed_fallback',
                  'attempt': 1,
                  'tokens': 20,
                  'duration_seconds': 0.5,
                  'fallback': 'fb',
                  'output': 'beta',
                },
              ],
            }),
            200,
            headers: headers,
          );
        }
        return http.Response(
          jsonEncode({'error': {'code': 'unknown_run', 'message': 'nope'}}),
          404,
          headers: headers,
        );
      });

  /// A compact, comparable snapshot of the run state a canvas renders:
  /// run id, run status, token total, and the per-node status map.
  Map<String, Object?> stateSnapshot(RunState s) => {
        'runId': s.runId,
        'runStatus': s.runStatus,
        'totalTokens': s.totalTokens,
        'nodes': {
          for (final e in s.nodes.entries) e.key: e.value.status
        },
      };

  MockClient classicClient() => MockClient((request) async {
        final path = request.url.path;
        final headers = <String, String>{'content-type': 'application/json'};
        if (request.method == 'POST' && path == '/runs') {
          return http.Response(
            jsonEncode({'run_id': runId, 'state': 'running', 'workflow': 'tri'}),
            201,
            headers: headers,
          );
        }
        if (request.method == 'GET' && path == '/runs') {
          return http.Response('[]', 200, headers: headers);
        }
        if (request.method == 'GET' && path == '/runs/$runId') {
          return http.Response(jsonEncode(completedSnapshot()), 200,
              headers: headers);
        }
        if (request.method == 'GET' && path == '/runs/$runId/result') {
          return http.Response(
            jsonEncode({
              'run_id': runId,
              'status': 'completed',
              'total_tokens': 120,
              'duration_seconds': 2.0,
              'nodes': <Object>[],
            }),
            200,
            headers: headers,
          );
        }
        if (request.method == 'GET' && path == '/health') {
          return http.Response(
            jsonEncode({'status': 'ok', 'log_dir': 'runs'}),
            200,
            headers: headers,
          );
        }
        return http.Response(
          jsonEncode({'error': {'code': 'unknown_run', 'message': 'nope'}}),
          404,
          headers: headers,
        );
      });

  group('T012 classic view (US1)', () {
    testWidgets('renders one card per step, directed edges, entry markers, '
        'and the legend panel (US1 scenarios 1–4)', (tester) async {
      final d = def();
      final store = RunStore(workflowDefinition: d)
        ..seed(RunStatusView.fromJson(settledSnapshot()));

      await pumpView(tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump(); // post-frame zoom-to-fit

      final scene = viewState(tester).scene!;

      // Scenario 1: one card per step (every run node).
      expect(scene.layout.cards.keys.toSet(), {'a', 'b', 'c', 'd', 'e'});

      // Scenario 2: directed edges for every placed dependency, anchored
      // source right-midpoint → target left-midpoint (the arrowhead is
      // painted at each target anchor by the ClassicPainter).
      expect(
          scene.layout.edges.map((e) => '${e.fromId}>${e.toId}').toSet(),
          {'a>b', 'a>d', 'b>c', 'd>e'});
      for (final e in scene.layout.edges) {
        final s = scene.layout.cards[e.fromId]!.rect;
        final t = scene.layout.cards[e.toId]!.rect;
        expect(e.sourceAnchor, Offset(s.right, s.center.dy));
        expect(e.targetAnchor, Offset(t.left, t.center.dy));
      }

      // Scenario 3: entry markers on the in-degree-0 cards (only a).
      expect(scene.layout.cards['a']!.isEntry, isTrue);
      expect(
          scene.layout.cards.values.where((c) => c.isEntry).length, 1);

      // Scenario 4: the legend panel.
      expect(find.byType(ClassicLegend), findsOneWidget);

      // The scene is painted by the ClassicPainter.
      final paint = tester.widget<CustomPaint>(find.descendant(
          of: find.byType(ClassicCanvasView),
          matching: find.byType(CustomPaint)).first);
      expect(paint.painter, isA<ClassicPainter>());
    });

    testWidgets('zooms to fit on first scene build', (tester) async {
      final d = def();
      final store = RunStore(workflowDefinition: d)
        ..seed(RunStatusView.fromJson(settledSnapshot()));

      await pumpView(tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump(); // post-frame zoom-to-fit

      final state = viewState(tester);
      // World bounds of the settled scene: x 60..840 (w 780), y 60..240
      // (h 180) in an 800×600 viewport → scale ≈ 800/780, scene centered.
      expect(state.scale, closeTo(800 / 780, 1e-3));
      expect(state.pan.dy, closeTo(300 - (180 * (800 / 780)) / 2, 1.0));
    });

    testWidgets('rebuilds the scene on every store change', (tester) async {
      final d = def();
      final store = RunStore(workflowDefinition: d);

      await pumpView(tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump();
      expect(viewState(tester).scene, isNull,
          reason: 'no scene before the store is seeded');

      final running = RunStatusView.fromJson({
        'run_id': runId,
        'workflow_name': 'settled',
        'state': 'running',
        'started_at': ts,
        'nodes': <String, Object>{
          'a': {'status': 'running', 'attempt': 1, 'model': 'draft'},
        },
        'pending_inputs': <Object>[],
      });
      store.seed(running);
      await tester.pump();
      expect(viewState(tester).scene, isNotNull,
          reason: 'seeding the store builds a scene');
      expect(viewState(tester).scene!.state, same(store.state));

      final settled = RunStatusView.fromJson(settledSnapshot());
      store.seed(settled);
      await tester.pump();
      expect(viewState(tester).scene!.state, same(store.state),
          reason: 'a later store change rebuilds the scene');
    });
  });

  group('T019 live feedback (US2)', () {
    testWidgets('a node status change fires a ~600 ms card highlight and a '
        'marker traveling every edge that touches the node', (tester) async {
      final d = triDef();
      final events = StreamController<EventFrame>();
      final store = RunStore(workflowDefinition: d)
        ..connect(events.stream)
        ..seed(RunStatusView.fromJson({
          'run_id': runId,
          'workflow_name': 'tri',
          'state': 'running',
          'started_at': ts,
          'nodes': <String, Object>{
            'a': {'status': 'running', 'attempt': 1, 'model': 'draft'},
          },
          'pending_inputs': <Object>[],
        }));

      await pumpView(tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump(); // post-frame zoom-to-fit

      final st = viewState(tester);
      expect(st.pulses, isEmpty, reason: 'the seed scene registers no pulse');
      final c0 = st.clock;

      events.add(frame('node_completed', 1, {
        'node_id': 'a',
        'attempt': 1,
        'tokens_used': 12,
        'duration_seconds': 1.5,
      }));
      await tester.pump(); // stream microtask + scene rebuild

      expect(st.pulses.keys, contains('a'),
          reason: 'a completed transition registers a pulse for a');
      final start = st.pulses['a']!;
      expect(start, closeTo(c0, 1e-9),
          reason: 'the pulse starts at the current ticker clock');

      // Halfway through the 600 ms window: the card highlight channel is in
      // flight and the marker sits mid-curve on BOTH edges leaving a.
      await tester.pump(const Duration(milliseconds: 300));
      expect(pulseProgress(st.clock, start), closeTo(0.5, 1e-9),
          reason: 'the highlight is halfway through its ~600 ms window');
      final scene = st.scene!;
      expect(pulseForEdge(edge(scene, 'a', 'b'), st.pulses, st.clock),
          closeTo(0.5, 1e-9));
      expect(pulseForEdge(edge(scene, 'a', 'c'), st.pulses, st.clock),
          closeTo(0.5, 1e-9));
      expect(pulseForEdge(edge(scene, 'b', 'c'), st.pulses, st.clock), isNull,
          reason: 'edges that do not touch a stay quiet');

      // The marker travels source → target (rightward on a→b), the edge's
      // direction.
      final eAB = edge(scene, 'a', 'b');
      final m1 = classicEdgePointAt(eAB, pulseProgress(st.clock, start)!);
      expect(m1.dx, greaterThan(eAB.sourceAnchor.dx));
      expect(m1.dx, lessThan(eAB.targetAnchor.dx));

      // Past the window: the pulse is over, highlight lifted, marker gone.
      await tester.pump(const Duration(milliseconds: 400));
      expect(pulseProgress(st.clock, start), isNull,
          reason: 'the pulse expires after ~600 ms');
      expect(pulseForEdge(edge(st.scene!, 'a', 'b'), st.pulses, st.clock), isNull);
      expect(pulseForEdge(edge(st.scene!, 'a', 'c'), st.pulses, st.clock), isNull);
    });

    testWidgets('fallback_activated fires the same response without a '
        'status change', (tester) async {
      final d = triDef();
      final events = StreamController<EventFrame>();
      final store = RunStore(workflowDefinition: d)
        ..connect(events.stream)
        ..seed(RunStatusView.fromJson({
          'run_id': runId,
          'workflow_name': 'tri',
          'state': 'running',
          'started_at': ts,
          'nodes': <String, Object>{
            'b': {'status': 'running', 'attempt': 1, 'model': 'draft'},
          },
          'pending_inputs': <Object>[],
        }));

      await pumpView(tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump();

      events.add(frame('fallback_activated', 1, {'node_id': 'b', 'fallback': 'fb'}));
      await tester.pump();

      final node = store.state!.nodes['b']!;
      expect(node.status, NodeStatus.running,
          reason: 'fallback_activated keeps the current status');
      expect(node.model, 'fb');
      expect(node.fallbackModel, 'fb');
      expect(node.onFallbackPath, isTrue);

      final st = viewState(tester);
      expect(st.pulses.keys, contains('b'),
          reason: 'the model/fallback identity change registers a pulse');
      await tester.pump(const Duration(milliseconds: 300));
      final scene = st.scene!;
      // b's touching edges: a→b (incoming) and b→c (outgoing).
      expect(pulseForEdge(edge(scene, 'a', 'b'), st.pulses, st.clock), isNotNull);
      expect(pulseForEdge(edge(scene, 'b', 'c'), st.pulses, st.clock), isNotNull);
      expect(pulseForEdge(edge(scene, 'a', 'c'), st.pulses, st.clock), isNull);
    });

    testWidgets('frames without a step mapping never animate the canvas',
        (tester) async {
      final d = triDef();
      final events = StreamController<EventFrame>();
      final store = RunStore(workflowDefinition: d)
        ..connect(events.stream)
        ..seed(RunStatusView.fromJson({
          'run_id': runId,
          'workflow_name': 'tri',
          'state': 'running',
          'started_at': ts,
          'nodes': <String, Object>{
            'a': {'status': 'running', 'attempt': 1, 'model': 'draft'},
          },
          'pending_inputs': <Object>[],
        }));

      await pumpView(tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump();

      final st = viewState(tester);
      final unmapped = [
        'vram_threshold_exceeded',
        'vram_freed',
        'model_offloaded',
        'model_reloaded',
        'agent_queued',
        'agent_dequeued',
        'phase_started',
        'phase_completed',
        'summarization_triggered',
      ];
      for (var i = 0; i < unmapped.length; i++) {
        events.add(frame(unmapped[i], i + 1, const {}));
        await tester.pump();
        expect(st.pulses, isEmpty,
            reason: '${unmapped[i]} has no classic step mapping');
      }
      expect(store.state!.nodes['a']!.status, NodeStatus.running,
          reason: 'unmapped frames do not disturb node state');
    });
  });

  group('T020 state encodings (behavior) (US2)', () {
    test('retryCountdownLabel ticks down and degrades safely', () {
      final next = tsDate.add(const Duration(milliseconds: 2500));
      expect(retryCountdownLabel(next, tsDate), 'retry in 2s');
      expect(
          retryCountdownLabel(next, tsDate.add(const Duration(seconds: 1))),
          'retry in 1s',
          reason: 'the label ticks down second by second');
      expect(retryCountdownLabel(next, next), 'retrying…');
      expect(
          retryCountdownLabel(next, tsDate.add(const Duration(seconds: 3))),
          'retrying…',
          reason: 'a past retry instant never renders a negative countdown');
      expect(retryCountdownLabel(null, tsDate), 'retry…');
      expect(retryCountdownLabel(next, null), 'retry…');
    });

    testWidgets('running: pulsing border channel, live on the ticker clock',
        (tester) async {
      final d = triDef();
      final store = RunStore(workflowDefinition: d)
        ..seed(RunStatusView.fromJson({
          'run_id': runId,
          'workflow_name': 'tri',
          'state': 'running',
          'started_at': ts,
          'nodes': <String, Object>{
            'a': {'status': 'running', 'attempt': 1, 'model': 'draft'},
          },
          'pending_inputs': <Object>[],
        }));

      await pumpView(tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump();

      final enc = viewState(tester).scene!.encodings['a']!;
      expect(enc, kClassicStateEncodings[NodeStatus.running]!);
      expect(enc.label, 'running');
      expect(enc.marker, ClassicMarker.pulse);
      expect(enc.animated, isTrue);

      await tester.pump(const Duration(milliseconds: 100));
      await tester.pump(const Duration(milliseconds: 100));
      await tester.pump(); // let the ticker rebuild land

      final st = viewState(tester);
      final t0 = st.clock;
      // The pulsing border is a deterministic, non-static function of the
      // ticker clock: distinct a quarter period apart, always in (0, 1].
      expect(runningBorderAlpha(t0), isNot(runningBorderAlpha(t0 + 0.5)));
      final alphas =
          List<double>.generate(8, (i) => runningBorderAlpha(t0 + i * 0.25));
      expect(alphas.every((a) => a > 0 && a <= 1), isTrue);
      expect(alphas.toSet().length, greaterThan(1));

      // The painter is fed the live clock each frame.
      expect(painterOf(tester).clock, closeTo(t0, 1e-9));
      expect(painterOf(tester).now, isNotNull,
          reason: 'the countdown channel gets a live "now"');
    });

    testWidgets('awaiting_retry: nextRetryAt drives the ticking countdown '
        'channel', (tester) async {
      final d = triDef();
      final events = StreamController<EventFrame>();
      final store = RunStore(workflowDefinition: d)
        ..connect(events.stream)
        ..seed(RunStatusView.fromJson({
          'run_id': runId,
          'workflow_name': 'tri',
          'state': 'running',
          'started_at': ts,
          'nodes': <String, Object>{
            'a': {'status': 'running', 'attempt': 1, 'model': 'draft'},
          },
          'pending_inputs': <Object>[],
        }));

      await pumpView(tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump();

      // Unknown nextRetryAt → the safe static placeholder, not a broken
      // countdown.
      expect(
          retryCountdownLabel(
              store.state!.nodes['a']!.nextRetryAt, painterOf(tester).now),
          'retry…');

      events.add(frame('node_retrying', 1,
          {'node_id': 'a', 'attempt': 2, 'delay_seconds': 2.5}));
      await tester.pump();

      final node = store.state!.nodes['a']!;
      expect(node.status, NodeStatus.awaitingRetry);
      expect(node.nextRetryAt, tsDate.add(const Duration(milliseconds: 2500)),
          reason: 'node_retrying stamps nextRetryAt = frame ts + delay');

      await tester.pump(); // let the rebuild land
      final now0 = painterOf(tester).now!;
      await tester.pump(const Duration(milliseconds: 500));
      await tester.pump();
      final now1 = painterOf(tester).now!;
      expect(now1.difference(now0).inMilliseconds, 500,
          reason: 'the countdown clock advances with the ticker');
      expect(retryCountdownLabel(node.nextRetryAt, now1), 'retrying…',
          reason: 'the fixed frame ts is in the past → "retrying…", never '
              'a negative countdown');
    });

    testWidgets('completed_fallback: fb chip encoding names the fallback '
        'model', (tester) async {
      final d = triDef();
      final store = RunStore(workflowDefinition: d)
        ..seed(RunStatusView.fromJson({
          'run_id': runId,
          'workflow_name': 'tri',
          'state': 'completed',
          'started_at': ts,
          'nodes': <String, Object>{
            'a': {'status': 'completed', 'attempt': 1, 'tokens': 10, 'duration_seconds': 1.0},
            'b': {'status': 'completed_fallback', 'attempt': 1, 'fallback': 'fb', 'model': 'fb', 'tokens': 30, 'duration_seconds': 2.0},
          },
          'pending_inputs': <Object>[],
        }));

      await pumpView(tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump();

      final scene = viewState(tester).scene!;
      final node = scene.state.nodes['b']!;
      expect(node.status, NodeStatus.completedFallback);
      expect(node.fallbackModel, 'fb',
          reason: 'the inspector names the fallback model');
      expect(node.onFallbackPath, isTrue);
      expect(scene.encodings['b']!.marker, ClassicMarker.checkFb);
      expect(scene.encodings['b']!.label, 'fallback');
      expect(scene.encodings['a']!.marker, ClassicMarker.check,
          reason: 'distinguishable from plain completed');
    });

    testWidgets('failed: cross encoding carries the error reason',
        (tester) async {
      final d = triDef();
      final store = RunStore(workflowDefinition: d)
        ..seed(RunStatusView.fromJson({
          'run_id': runId,
          'workflow_name': 'tri',
          'state': 'failed',
          'started_at': ts,
          'nodes': <String, Object>{
            'c': {'status': 'failed', 'attempt': 1, 'reason': 'boom'},
          },
          'pending_inputs': <Object>[],
        }));

      await pumpView(tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump();

      final scene = viewState(tester).scene!;
      expect(scene.encodings['c']!.marker, ClassicMarker.cross);
      expect(scene.encodings['c']!.label, 'failed');
      expect(scene.state.nodes['c']!.error, 'boom');
    });

    testWidgets('skipped: dimmed, no motion, carries the skip reason',
        (tester) async {
      final d = triDef();
      final store = RunStore(workflowDefinition: d)
        ..seed(RunStatusView.fromJson({
          'run_id': runId,
          'workflow_name': 'tri',
          'state': 'completed',
          'started_at': ts,
          'nodes': <String, Object>{
            'c': {'status': 'skipped', 'attempt': 1, 'reason': 'unresolved_binding'},
          },
          'pending_inputs': <Object>[],
        }));

      await pumpView(tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump();

      final scene = viewState(tester).scene!;
      final enc = scene.encodings['c']!;
      expect(enc.marker, ClassicMarker.dim);
      expect(enc.label, 'skipped');
      expect(enc.animated, isFalse, reason: 'skipped cards never animate');
      expect(scene.state.nodes['c']!.skipReason, 'unresolved_binding');
    });
  });

  group('T023 run summary above the classic canvas (US2)', () {
    testWidgets('the shared SummaryBar stays composed above the canvas and '
        'reflects the store', (tester) async {
      // The bar's fixed metrics (status, badge, elapsed, tokens) need more
      // width than the default 800px test window leaves the run panel under
      // the wide test font, so give the panel production-like room.
      tester.view.physicalSize = const Size(1280, 800);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      final d = triDef();
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState();
      final client = AgencyClient('http://fake', httpClient: classicClient());
      final entry = CatalogEntry(path: 'tri.yaml', name: 'tri', definition: d);

      await pumpHome(tester, app: app, client: client, entry: entry, events: events);
      await tester.tap(find.text('Start').first);
      await settle(tester);

      app.setCanvasMode(CanvasMode.classic);
      await tester.pump();

      expect(find.byType(SummaryBar), findsOneWidget);
      expect(find.byType(ClassicCanvasView), findsOneWidget);
      final barTop = tester.getTopLeft(find.byType(SummaryBar)).dy;
      final canvasTop = tester.getTopLeft(find.byType(ClassicCanvasView)).dy;
      expect(barTop, lessThan(canvasTop),
          reason: 'the summary bar sits above the canvas in classic mode');

      final bar = tester.widget<SummaryBar>(find.byType(SummaryBar));
      expect(bar.state, same(app.selectedRunStore!.state),
          reason: 'the bar mirrors the same store state the canvas reads');

      // A completed run shows its terminal status and API-level token total.
      expect(
          find.descendant(of: find.byType(SummaryBar), matching: find.text('completed')),
          findsOneWidget);
      expect(
          find.descendant(of: find.byType(SummaryBar), matching: find.text('120 tokens')),
          findsOneWidget);

      // A run_cancel frame surfaces the cancelled badge in the bar.
      events.add(frame('run_cancel', 1, const {}));
      await tester.pump();
      expect(
          find.descendant(of: find.byType(SummaryBar), matching: find.text('cancelled')),
          findsOneWidget);
    });
  });

  group('T025 mode switch loses nothing (US3 scenario 3)', () {
    testWidgets(
        'a history run rendered in classic survives an Abyss round trip',
        (tester) async {
      // Give the run panel production-like room (as in the T023 tests).
      tester.view.physicalSize = const Size(1280, 800);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      final d = triDef();
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState()..setCanvasMode(CanvasMode.classic);
      final client =
          AgencyClient('http://fake', httpClient: historyClassicClient());
      final entry = CatalogEntry(path: 'tri.yaml', name: 'tri', definition: d);

      await pumpHome(tester,
          app: app, client: client, entry: entry, events: events);
      for (var i = 0; i < 6; i++) {
        await tester.pump();
      }

      expect(app.selectedRunId, runId,
          reason: 'the history run is auto-selected on startup');
      expect(find.byType(ClassicCanvasView), findsOneWidget);
      final store = app.storeFor(runId);
      final before = stateSnapshot(store.state!);
      expect(before['nodes'], isNotEmpty);

      // Switch to Abyss: the same run re-renders there, state intact.
      app.setCanvasMode(CanvasMode.abyss);
      await tester.pump();
      await tester.pump();
      expect(find.byType(ClassicCanvasView), findsNothing);
      expect(find.byType(CanvasView), findsOneWidget);
      final abyssScene =
          tester.state<CanvasViewState>(find.byType(CanvasView)).scene;
      expect(abyssScene, isNotNull,
          reason: 'the Abyss view renders the same run');
      expect(abyssScene!.state.runId, runId);
      expect(stateSnapshot(store.state!), before,
          reason: 'switching to Abyss loses nothing');

      // Switch back: classic re-renders the same run with its state intact.
      app.setCanvasMode(CanvasMode.classic);
      await tester.pump();
      await tester.pump(); // post-frame zoom-to-fit
      expect(find.byType(ClassicCanvasView), findsOneWidget);
      final scene = viewState(tester).scene!;
      expect(scene.runId, runId);
      expect(stateSnapshot(scene.state), before,
          reason: 'the classic scene re-renders from the intact store');
      expect(stateSnapshot(store.state!), before);
      expect(app.selectedRunId, runId, reason: 'the selection is intact');
    });
  });

  group('T026 terminal card content (US3)', () {
    test('the terminal metrics line carries the state glyph, metrics, and reason',
        () {
      final completed = SNodeState(
        nodeId: 'a',
        type: 'agent',
        model: 'draft',
        status: NodeStatus.completed,
        tokens: 342,
        durationSeconds: 12.4,
        maxAttempts: 2,
      );
      expect(classicMetricsLine(completed),
          '✓ 342 tok · 12.4 s · attempt 1/2');

      // A fallback completion keeps the check glyph (the fb chip is drawn
      // in the card header).
      final fallback = completed.copyWith(
        status: NodeStatus.completedFallback,
        model: 'fb-model',
      );
      expect(classicMetricsLine(fallback),
          '✓ 342 tok · 12.4 s · attempt 1/2');

      // A failed step: cross glyph, no token/duration, failure reason.
      final failed = SNodeState(
        nodeId: 'b',
        type: 'agent',
        status: NodeStatus.failed,
        attempt: 2,
        maxAttempts: 2,
        error: 'b exploded',
      );
      expect(classicMetricsLine(failed), '✕ attempt 2/2 · b exploded');

      // A skipped step: dimmed card — no glyph, but the reason stays on the
      // line.
      final skipped = SNodeState(
        nodeId: 'c',
        type: 'conditional',
        status: NodeStatus.skipped,
        skipReason: 'condition false',
      );
      expect(classicMetricsLine(skipped), 'attempt 1/1 · condition false');

      // Long reasons are truncated with an ellipsis.
      final longFail = failed.copyWith(error: 'x' * 50);
      expect(classicMetricsLine(longFail)!.endsWith('…'), isTrue);

      // Non-terminal states carry no metrics line.
      expect(
          classicMetricsLine(
              SNodeState(nodeId: 'd', type: 'agent', status: NodeStatus.running)),
          isNull);
      expect(
          classicMetricsLine(
              SNodeState(nodeId: 'e', type: 'agent', status: NodeStatus.pending)),
          isNull);
      expect(
          classicMetricsLine(SNodeState(
              nodeId: 'f',
              type: 'agent',
              status: NodeStatus.awaitingRetry)),
          isNull);
    });
  });

  group('T027 mode persistence (US4, FR-002)', () {
    test('(a) fresh preferences default to abyss (first launch)', () async {
      SharedPreferences.setMockInitialValues(const {});
      final app = AppState();
      await app.init();
      expect(app.canvasMode, CanvasMode.abyss,
          reason: 'no stored canvas_mode → the first-launch default is abyss');
    });

    test('(b) setCanvasMode persists; a re-created AppState restores it',
        () async {
      SharedPreferences.setMockInitialValues(const {});
      final app = AppState();
      await app.init();
      expect(app.canvasMode, CanvasMode.abyss);

      app.setCanvasMode(CanvasMode.classic);
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString('canvas_mode'), 'classic',
          reason: 'setCanvasMode writes the enum name to SharedPreferences');

      final restarted = AppState();
      await restarted.init();
      expect(restarted.canvasMode, CanvasMode.classic,
          reason: 'a re-created AppState restores the stored mode on restart');

      restarted.setCanvasMode(CanvasMode.abyss);
      final restartedAgain = AppState();
      await restartedAgain.init();
      expect(restartedAgain.canvasMode, CanvasMode.abyss,
          reason: 'the abyss choice persists through a restart the same way');
    });

    test('(c) unrecognized canvas_mode values fall back to abyss', () async {
      for (final junk in ['spatial', 'CLASSIC', 'abyss3d', '', 'classic ']) {
        SharedPreferences.setMockInitialValues({'canvas_mode': junk});
        final app = AppState();
        await app.init();
        expect(app.canvasMode, CanvasMode.abyss,
            reason: "canvas_mode='$junk' is not 'abyss'/'classic' → abyss "
                '(data-model.md Validation Rule 6)');
      }
      SharedPreferences.setMockInitialValues({'canvas_mode': 'abyss'});
      final explicit = AppState();
      await explicit.init();
      expect(explicit.canvasMode, CanvasMode.abyss,
          reason: 'the explicit abyss value stays abyss');
    });

    testWidgets('(d) the top-bar toggle writes the preference on every change '
        'and re-renders the same run with the selection intact',
        (tester) async {
      SharedPreferences.setMockInitialValues(const {});
      tester.view.physicalSize = const Size(1280, 800);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      final d = triDef();
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState();
      await app.init();
      final client =
          AgencyClient('http://fake', httpClient: historyClassicClient());
      final entry = CatalogEntry(path: 'tri.yaml', name: 'tri', definition: d);

      await pumpHome(tester,
          app: app, client: client, entry: entry, events: events);
      for (var i = 0; i < 6; i++) {
        await tester.pump();
      }
      expect(app.selectedRunId, runId,
          reason: 'the history run is auto-selected on first launch');
      expect(find.byType(CanvasView), findsOneWidget,
          reason: 'first launch renders the Abyss view');

      await tester.tap(find.text('Classic'));
      await tester.pump();
      await tester.pump(); // post-frame zoom-to-fit
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString('canvas_mode'), 'classic',
          reason: 'toggling to Classic writes the preference');
      expect(app.canvasMode, CanvasMode.classic);
      expect(find.byType(ClassicCanvasView), findsOneWidget);
      expect(app.selectedRunId, runId,
          reason: 'the selection survives the toggle');

      await tester.tap(find.text('Abyss'));
      await tester.pump();
      await tester.pump();
      expect((await SharedPreferences.getInstance()).getString('canvas_mode'),
          'abyss',
          reason: 'toggling back to Abyss writes the preference again');
      expect(find.byType(CanvasView), findsOneWidget);
      expect(app.selectedRunId, runId,
          reason: 'the selection survives the round trip');
    });

    test('(e) fresh preferences default to leftRight (first launch)', () async {
      SharedPreferences.setMockInitialValues(const {});
      final app = AppState();
      await app.init();
      expect(app.canvasTopology, CanvasTopology.leftRight,
          reason: 'no stored canvas_topology → the first-launch default');
    });

    test('(f) setCanvasTopology persists; a re-created AppState restores it',
        () async {
      SharedPreferences.setMockInitialValues(const {});
      final app = AppState();
      await app.init();
      expect(app.canvasTopology, CanvasTopology.leftRight);

      app.setCanvasTopology(CanvasTopology.topDown);
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString('canvas_topology'), 'topDown',
          reason: 'setCanvasTopology writes the enum name to SharedPreferences');

      final restarted = AppState();
      await restarted.init();
      expect(restarted.canvasTopology, CanvasTopology.topDown,
          reason: 'a re-created AppState restores the stored topology');
    });

    test('(g) unrecognized canvas_topology values fall back to leftRight',
        () async {
      for (final junk in ['vertical', 'TOPDOWN', 'leftRight ', '', 'diagonal']) {
        SharedPreferences.setMockInitialValues({'canvas_topology': junk});
        final app = AppState();
        await app.init();
        expect(app.canvasTopology, CanvasTopology.leftRight,
            reason: "canvas_topology='$junk' is not a known topology → default");
      }
      SharedPreferences.setMockInitialValues({'canvas_topology': 'bottomUp'});
      final explicit = AppState();
      await explicit.init();
      expect(explicit.canvasTopology, CanvasTopology.bottomUp,
          reason: 'the explicit bottomUp value stays bottomUp');
    });

    testWidgets('(h) the top-bar topology menu: classic-only, and selecting an '
        'option persists the choice and re-lays out the view',
        (tester) async {
      SharedPreferences.setMockInitialValues(const {});
      tester.view.physicalSize = const Size(1280, 800);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      final d = triDef();
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState();
      await app.init();
      final client =
          AgencyClient('http://fake', httpClient: historyClassicClient());
      final entry = CatalogEntry(path: 'tri.yaml', name: 'tri', definition: d);

      await pumpHome(tester,
          app: app, client: client, entry: entry, events: events);
      for (var i = 0; i < 6; i++) {
        await tester.pump();
      }

      expect(find.byType(PopupMenuButton<CanvasTopology>), findsNothing,
          reason: 'the topology menu is absent in Abyss mode');

      await tester.tap(find.text('Classic'));
      await tester.pump();
      await tester.pump(); // post-frame zoom-to-fit

      final menu = find.byType(PopupMenuButton<CanvasTopology>);
      expect(menu, findsOneWidget,
          reason: 'the classic mode shows the topology menu');

      // The popup opens over a frame-stepped animation; a single large pump
      // leaves the items non-interactive, so advance several small frames.
      await tester.tap(menu);
      for (var i = 0; i < 4; i++) {
        await tester.pump(const Duration(milliseconds: 100));
      }
      await tester.tap(find.text('Top ↓ down'));
      for (var i = 0; i < 4; i++) {
        await tester.pump(const Duration(milliseconds: 100));
      }

      expect(app.canvasTopology, CanvasTopology.topDown,
          reason: 'selecting the option updates the app state');
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString('canvas_topology'), 'topDown',
          reason: 'the choice is persisted');
      expect(viewState(tester).scene!.topology, CanvasTopology.topDown,
          reason: 'the classic view re-lays out under the new topology');
    });
  });

  group('T028 theme parity (US4, FR-022, R14)', () {
    // WCAG-style contrast ratio (mirrors the abyss legibility gate in
    // theme_test.dart): below 2.0 a state hue starts to vanish on a busy
    // background.
    double channel(double v) => v <= 0.03928
        ? v / 12.92
        : math.pow((v + 0.055) / 1.055, 2.4).toDouble();

    double luminance(Color c) =>
        0.2126 * channel(c.r) + 0.7152 * channel(c.g) + 0.0722 * channel(c.b);

    double contrast(Color a, Color b) {
      final la = luminance(a);
      final lb = luminance(b);
      final hi = la > lb ? la : lb;
      final lo = la > lb ? lb : la;
      return (hi + 0.05) / (lo + 0.05);
    }

    /// A theme-independent snapshot of what the scene shows: card positions
    /// and entry flags, directed edges, per-node state encodings (label +
    /// marker + motion), and per-node type icons — exactly the channels
    /// FR-022 demands stay identical between themes (palette only differs).
    Object sceneStructure(ClassicScene s) => {
          'runId': s.runId,
          'cards': {
            for (final e in s.layout.cards.entries)
              e.key: [e.value.rect, e.value.isEntry],
          },
          'edges': [
            for (final e in s.layout.edges)
              [e.fromId, e.toId, e.sourceAnchor, e.targetAnchor]
          ],
          'encodings': {
            for (final e in s.encodings.entries)
              e.key:
                  '${e.value.label}|${e.value.marker.name}|${e.value.animated}'
          },
          'typeIcons': {
            for (final e in s.state.nodes.entries)
              e.key: typeMarkerFor(
                      s.definition.nodeById(e.key)?.type ??
                          NodeType.tryParse(e.value.type) ??
                          NodeType.agent)
                  .icon
                  .codePoint,
          },
        };

    testWidgets('dark and light render identical structure and encodings; '
        'only the palette differs', (tester) async {
      final d = def();
      final store = RunStore(workflowDefinition: d)
        ..seed(RunStatusView.fromJson(settledSnapshot()));

      await pumpView(
          tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump(); // post-frame zoom-to-fit
      expect(painterOf(tester).palette, same(darkClassic()),
          reason: 'the dark theme selects the dark classic palette');
      final dark = viewState(tester).scene!;

      await pumpView(tester,
          view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight),
          mode: ThemeMode.light);
      await tester.pump(); // post-frame zoom-to-fit
      // MaterialApp crossfades theme changes (200 ms); advance past it so
      // Theme.of reflects the light theme (as theme_test.dart's settle does).
      await tester.pump(const Duration(milliseconds: 300));
      await tester.pump();
      expect(painterOf(tester).palette, same(lightClassic()),
          reason: 'the light theme selects the light classic palette');
      final light = viewState(tester).scene!;

      expect(sceneStructure(light), sceneStructure(dark),
          reason: 'layout positions, labels, markers, and icons are '
              'theme-independent; only palette colors differ');
    });

    testWidgets('all six type icons and seven state encodings stay legible '
        'in both themes', (tester) async {
      const minContrast = 2.0;
      for (final p in [darkClassic(), lightClassic()]) {
        for (final s in NodeStatus.values) {
          expect(contrast(p.colorFor(s), p.background),
              greaterThanOrEqualTo(minContrast),
              reason: '${p.mode.name}: state ${s.wire} legible against the ground');
        }
        expect(contrast(p.text, p.background), greaterThanOrEqualTo(minContrast),
            reason: '${p.mode.name}: text legible against the ground');
        expect(contrast(p.accent, p.background), greaterThanOrEqualTo(minContrast),
            reason: '${p.mode.name}: accent legible against the ground');
        expect(contrast(p.edge, p.background), greaterThanOrEqualTo(minContrast),
            reason: '${p.mode.name}: edges legible against the ground');
      }

      // The legend lists every type icon and state encoding in both themes.
      for (final theme in {
        ThemeMode.dark: darkClassic(),
        ThemeMode.light: lightClassic(),
      }.entries) {
        await tester.pumpWidget(MaterialApp(
          theme: themeDataFor(theme.key),
          home: Scaffold(body: ClassicLegend(palette: theme.value)),
        ));
        for (final t in NodeType.values) {
          expect(find.byIcon(typeMarkerFor(t).icon), findsOneWidget,
              reason: '${theme.key.name}: type ${t.wire} icon is listed');
          expect(find.text(t.wire), findsOneWidget,
              reason: '${theme.key.name}: type ${t.wire} name is listed');
        }
        for (final s in NodeStatus.values) {
          expect(find.byKey(ValueKey('state-sample-${s.wire}')), findsOneWidget,
              reason: '${theme.key.name}: state ${s.wire} sample is listed');
          expect(
              find.text(kClassicStateEncodings[s]!.label), findsOneWidget,
              reason: '${theme.key.name}: state ${s.wire} label is listed');
        }
      }
    });
  });

  // ---- US5 helpers (T031–T034) ------------------------------------------

  /// The screen-space center of a classic card: the view's (pan, scale)
  /// transform applied to the card's world rect, offset by the view origin.
  Offset cardScreenCenter(WidgetTester tester, String nodeId) {
    final st = viewState(tester);
    final card = st.scene!.layout.cardOf(nodeId)!;
    final origin = tester.getTopLeft(find.byType(ClassicCanvasView));
    return origin.translate(
      card.rect.center.dx * st.scale + st.pan.dx,
      card.rect.center.dy * st.scale + st.pan.dy,
    );
  }

  /// One mouse-wheel scroll event at [location] (FR-016), consumed exactly as
  /// the canvas consumes it: zoom factor 1.0015^-dy.
  Future<void> wheelZoom(
    WidgetTester tester,
    Offset location,
    Offset scrollDelta,
  ) async {
    // The mouse tracker groups pointers by device, so an `Added` may only
    // follow a `Removed` for the shared mouse device. Add and remove the
    // pointer within each call so repeated calls (e.g. in a loop) stay valid.
    final gesture = await tester.createGesture(kind: PointerDeviceKind.mouse);
    await gesture.addPointer(location: location);
    await tester.pump();
    await gesture.updateWithCustomEvent(PointerScrollEvent(
      position: location,
      scrollDelta: scrollDelta,
    ));
    await gesture.removePointer();
    await tester.pump();
  }

  /// A workflow of a few dozen steps (SC-007): a 30-step chain with a skip
  /// edge every fifth step.
  WorkflowDefinition dozenDef() {
    final buf = StringBuffer('name: dozen\nnodes:\n');
    for (var i = 0; i < 30; i++) {
      buf.write('  n$i: {id: n$i, type: agent, model: draft}\n');
    }
    buf.write('edges:\n');
    for (var i = 0; i < 29; i++) {
      buf.write('  - {from_id: n$i, to_id: n${i + 1}}\n');
      if (i % 5 == 0) {
        buf.write('  - {from_id: n$i, to_id: n${i + 2}}\n');
      }
    }
    return parseWorkflowDefinition(buf.toString());
  }

  // T031 fixtures: a finished run with one step of every terminal shape.
  Map<String, Object> inspectorSnapshot() => {
        'run_id': runId,
        'workflow_name': 'settled',
        'state': 'completed',
        'started_at': ts,
        'nodes': <String, Object>{
          'a': {
            'status': 'completed',
            'attempt': 1,
            'model': 'draft',
            'tokens': 12,
            'duration_seconds': 1.5,
          },
          'b': {
            'status': 'completed_fallback',
            'attempt': 1,
            'model': 'fb',
            'fallback': 'fb',
            'tokens': 30,
            'duration_seconds': 2.0,
          },
          'c': {
            'status': 'failed',
            'attempt': 1,
            'reason': 'boom',
            'tokens': 5,
            'duration_seconds': 0.3,
          },
          'd': {'status': 'skipped', 'attempt': 1, 'reason': 'condition false'},
          'e': {'status': 'skipped', 'attempt': 1, 'reason': 'unresolved_binding'},
        },
        'pending_inputs': <Object>[],
      };

  MockClient inspectorClient() => MockClient((request) async {
        final path = request.url.path;
        final headers = <String, String>{'content-type': 'application/json'};
        if (request.method == 'GET' && path == '/runs') {
          return http.Response(
            jsonEncode([
              {
                'run_id': runId,
                'workflow_name': 'settled',
                'started_at': ts,
                'state': 'completed',
              },
            ]),
            200,
            headers: headers,
          );
        }
        if (request.method == 'GET' && path == '/runs/$runId') {
          return http.Response(jsonEncode(inspectorSnapshot()), 200,
              headers: headers);
        }
        if (request.method == 'GET' && path == '/runs/$runId/result') {
          return http.Response(
            jsonEncode({
              'run_id': runId,
              'status': 'completed',
              'total_tokens': 57,
              'duration_seconds': 6.3,
              'nodes': <Object>[
                {
                  'node_id': 'a',
                  'status': 'completed',
                  'attempt': 1,
                  'tokens': 12,
                  'duration_seconds': 1.5,
                  'output': 'alpha',
                },
                {
                  'node_id': 'b',
                  'status': 'completed_fallback',
                  'attempt': 1,
                  'fallback': 'fb',
                  'tokens': 30,
                  'duration_seconds': 2.0,
                  'output': 'beta',
                },
                {
                  'node_id': 'c',
                  'status': 'failed',
                  'attempt': 1,
                  'reason': 'boom',
                  'tokens': 5,
                  'duration_seconds': 0.3,
                  'output': 'gamma',
                },
              ],
            }),
            200,
            headers: headers,
          );
        }
        return http.Response(
          jsonEncode({'error': {'code': 'unknown_run', 'message': 'nope'}}),
          404,
          headers: headers,
        );
      });

  group('T031 inspector parity (US5 acceptance 1, FR-017)', () {
    testWidgets('tapping a card opens the shared inspector with the full '
        'terminal details (type, model, state, attempt, tokens, duration, '
        'reason, output)', (tester) async {
      SharedPreferences.setMockInitialValues(const {});
      tester.view.physicalSize = const Size(1280, 800);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      final d = def();
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState()..setCanvasMode(CanvasMode.classic);
      final client = AgencyClient('http://fake', httpClient: inspectorClient());
      final entry = CatalogEntry(
          path: 'settled.yaml', name: 'settled', definition: d);

      await pumpHome(
          tester, app: app, client: client, entry: entry, events: events);
      for (var i = 0; i < 6; i++) {
        await tester.pump();
      }
      expect(find.byType(ClassicCanvasView), findsOneWidget);

      Finder row(String value) => find.descendant(
            of: find.byType(Inspector),
            matching: find.text(value),
          );

      // b: completed via the fallback model, with its terminal output.
      await tester.tapAt(cardScreenCenter(tester, 'b'));
      await tester.pump();
      expect(find.byType(Inspector), findsOneWidget,
          reason: 'tapping a classic card opens the shared inspector');
      expect(row('B'), findsOneWidget, reason: 'the title is uppercase');
      expect(row('agent'), findsOneWidget);
      expect(row('completed_fallback'), findsOneWidget);
      expect(row('1/1'), findsOneWidget);
      expect(row('30'), findsOneWidget);
      expect(row('2.0s'), findsOneWidget);
      expect(row('Fallback'), findsOneWidget);
      expect(row('fb'), findsNWidgets(2),
          reason: 'both Model and Fallback name the fallback model');
      expect(row('Output'), findsOneWidget);
      expect(row('beta'), findsOneWidget);

      await tester.tap(find.byIcon(Icons.close));
      await tester.pump();
      expect(find.byType(Inspector), findsNothing);

      // c: a failed step with its error reason and output.
      await tester.tapAt(cardScreenCenter(tester, 'c'));
      await tester.pump();
      expect(find.byType(Inspector), findsOneWidget);
      expect(row('C'), findsOneWidget, reason: 'the title is uppercase');
      expect(row('tool_call'), findsOneWidget);
      expect(row('failed'), findsOneWidget);
      expect(row('boom'), findsOneWidget);
      expect(row('gamma'), findsOneWidget);

      await tester.tap(find.byIcon(Icons.close));
      await tester.pump();

      // d: a skipped step with its skip reason.
      await tester.tapAt(cardScreenCenter(tester, 'd'));
      await tester.pump();
      expect(find.byType(Inspector), findsOneWidget);
      expect(row('D'), findsOneWidget, reason: 'the title is uppercase');
      expect(row('conditional'), findsOneWidget);
      expect(row('skipped'), findsOneWidget);
      expect(row('Skipped: condition false'), findsOneWidget);
    });

    testWidgets('an awaiting_retry step shows the next-retry countdown '
        '(FR-017)', (tester) async {
      SharedPreferences.setMockInitialValues(const {});
      tester.view.physicalSize = const Size(1280, 800);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      final d = def();
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState()..setCanvasMode(CanvasMode.classic);
      final client = AgencyClient('http://fake', httpClient: MockClient(
          (request) async {
        final path = request.url.path;
        final headers = <String, String>{'content-type': 'application/json'};
        if (request.method == 'GET' && path == '/runs') {
          return http.Response(
            jsonEncode([
              {
                'run_id': runId,
                'workflow_name': 'settled',
                'started_at': ts,
                'state': 'running',
              },
            ]),
            200,
            headers: headers,
          );
        }
        if (request.method == 'GET' && path == '/runs/$runId') {
          return http.Response(
            jsonEncode({
              'run_id': runId,
              'workflow_name': 'settled',
              'state': 'running',
              'started_at': ts,
              'nodes': <String, Object>{
                'a': {'status': 'completed', 'attempt': 1, 'model': 'draft'},
                'b': {'status': 'running', 'attempt': 1, 'model': 'draft'},
              },
              'pending_inputs': <Object>[],
            }),
            200,
            headers: headers,
          );
        }
        return http.Response(
          jsonEncode({'error': {'code': 'unknown_run', 'message': 'nope'}}),
          404,
          headers: headers,
        );
      }));
      final entry =
          CatalogEntry(path: 'settled.yaml', name: 'settled', definition: d);

      await pumpHome(
          tester, app: app, client: client, entry: entry, events: events);
      for (var i = 0; i < 6; i++) {
        await tester.pump();
      }

      // The retry delay is 2.5 s after the frame timestamp (10 min ago), so
      // the countdown reads "retrying…" deterministically.
      events.add(frame('node_retrying', 1,
          {'node_id': 'b', 'attempt': 2, 'delay_seconds': 2.5}));
      for (var i = 0; i < 6; i++) {
        await tester.pump();
      }

      await tester.tapAt(cardScreenCenter(tester, 'b'));
      await tester.pump();
      expect(find.byType(Inspector), findsOneWidget);
      expect(find.descendant(
              of: find.byType(Inspector),
              matching: find.text('awaiting_retry')),
          findsOneWidget);
      expect(
          find.descendant(
              of: find.byType(Inspector),
              matching: find.text('2/1')),
          findsOneWidget);
      expect(
          find.descendant(
              of: find.byType(Inspector),
              matching: find.text('retrying…')),
          findsOneWidget);
    });
  });

  group('T032 navigation (FR-016, SC-007)', () {
    testWidgets('drag pans the canvas', (tester) async {
      final d = def();
      final store = RunStore(workflowDefinition: d)
        ..seed(RunStatusView.fromJson(settledSnapshot()));
      await pumpView(
          tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump(); // post-frame zoom-to-fit
      // Capture the values (not the live State) before the gesture mutates them.
      final beforePan = viewState(tester).pan;
      final beforeScale = viewState(tester).scale;
      expect(beforeScale, greaterThan(0));

      await tester.drag(find.byType(ClassicCanvasView), const Offset(50, 30));
      await tester.pump();

      final after = viewState(tester);
      expect(after.pan.dx, greaterThan(beforePan.dx),
          reason: 'dragging right moves the scene right');
      expect(after.pan.dy, greaterThan(beforePan.dy),
          reason: 'dragging down moves the scene down');
      expect(after.pan.dx, lessThanOrEqualTo(beforePan.dx + 50));
      expect(after.pan.dy, lessThanOrEqualTo(beforePan.dy + 30));
      expect(after.scale, beforeScale,
          reason: 'panning never changes the zoom');
    });

    testWidgets('wheel zoom clamps to 0.25–4 and holds the viewport center',
        (tester) async {
      final d = def();
      final store = RunStore(workflowDefinition: d)
        ..seed(RunStatusView.fromJson(settledSnapshot()));
      await pumpView(
          tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump(); // post-frame zoom-to-fit

      final st0 = viewState(tester);
      // Capture the scale value (not the live State) before the gesture mutates it.
      final scale0 = st0.scale;
      const focal = Offset(400, 300); // viewport center of the 800×600 view
      Offset worldAt(Offset focal, ClassicCanvasViewState st) =>
          Offset((focal.dx - st.pan.dx) / st.scale,
              (focal.dy - st.pan.dy) / st.scale);
      final worldBefore = worldAt(focal, st0);

      await wheelZoom(tester, focal, const Offset(0, -100));
      final st1 = viewState(tester);
      expect(st1.scale, closeTo(scale0 * math.pow(1.0015, 100).toDouble(), 1e-9),
          reason: 'scroll up zooms in by 1.0015^-dy');
      final worldAfter = worldAt(focal, st1);
      expect(worldAfter.dx, closeTo(worldBefore.dx, 1e-6),
          reason: 'zoom holds the viewport-center world point fixed (x)');
      expect(worldAfter.dy, closeTo(worldBefore.dy, 1e-6),
          reason: 'zoom holds the viewport-center world point fixed (y)');

      for (var i = 0; i < 20; i++) {
        await wheelZoom(tester, focal, const Offset(0, 300));
      }
      expect(viewState(tester).scale, closeTo(0.25, 1e-9),
          reason: 'zoom-out clamps at 0.25');

      for (var i = 0; i < 20; i++) {
        await wheelZoom(tester, focal, const Offset(0, -300));
      }
      expect(viewState(tester).scale, closeTo(4.0, 1e-9),
          reason: 'zoom-in clamps at 4');
    });

    testWidgets('pinch (two-pointer) zooms the canvas', (tester) async {
      final d = def();
      final store = RunStore(workflowDefinition: d)
        ..seed(RunStatusView.fromJson(settledSnapshot()));
      await pumpView(
          tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump(); // post-frame zoom-to-fit
      final before = viewState(tester).scale;

      final g1 = await tester.startGesture(const Offset(350, 300));
      final g2 = await tester.startGesture(const Offset(450, 300));
      addTearDown(g1.up);
      addTearDown(g2.up);
      await tester.pump();

      // Spread the two pointers from 100 px apart to 300 px apart (3x span).
      await g1.moveBy(const Offset(-100, 0));
      await g2.moveBy(const Offset(100, 0));
      await tester.pump();

      expect(viewState(tester).scale, greaterThan(before),
          reason: 'spreading the pointers zooms in');
      expect(viewState(tester).scale, lessThanOrEqualTo(4.0));
    });

    testWidgets('zoom-to-fit re-fits when the run (store) changes',
        (tester) async {
      final d = def();
      final store = RunStore(workflowDefinition: d)
        ..seed(RunStatusView.fromJson(settledSnapshot()));
      await pumpView(
          tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump(); // post-frame zoom-to-fit
      final firstScale = viewState(tester).scale;
      expect(firstScale, greaterThan(1.0),
          reason: 'def() fits wider than tall in an 800×600 view');

      // A two-step chain fits differently (narrower world, taller zoom).
      final pair = parseWorkflowDefinition('''
name: pair
nodes:
  p: {id: p, type: agent, model: draft}
  q: {id: q, type: agent, model: draft}
edges:
  - {from_id: p, to_id: q}
''');
      final store2 = RunStore(workflowDefinition: pair)
        ..seed(RunStatusView.fromJson({
          'run_id': runId,
          'workflow_name': 'pair',
          'state': 'running',
          'started_at': ts,
          'nodes': <String, Object>{
            'p': {'status': 'completed', 'attempt': 1},
            'q': {'status': 'running', 'attempt': 1},
          },
          'pending_inputs': <Object>[],
        }));
      await pumpView(
        tester,
        view: ClassicCanvasView(
          store: store2,
          definition: pair,
          topology: CanvasTopology.leftRight,
        ),
      );
      await tester.pump(); // post-frame zoom-to-fit on the new run

      final second = viewState(tester);
      expect(second.scale, closeTo(800 / 490, 1e-3),
          reason: 'the new run re-fits (bounds 490×72 in an 800×600 view)');
      expect(second.scale, isNot(equals(firstScale)));
    });

    testWidgets('pumping under each topology lays out the grid per the table',
        (tester) async {
      final pair = parseWorkflowDefinition('''
name: pair
nodes:
  p: {id: p, type: agent, model: draft}
  q: {id: q, type: agent, model: draft}
edges:
  - {from_id: p, to_id: q}
''');
      final store = RunStore(workflowDefinition: pair)
        ..seed(RunStatusView.fromJson({
          'run_id': runId,
          'workflow_name': 'pair',
          'state': 'completed',
          'started_at': ts,
          'nodes': <String, Object>{
            'p': {'status': 'completed', 'attempt': 1},
            'q': {'status': 'completed', 'attempt': 1},
          },
          'pending_inputs': <Object>[],
        }));

      // p=(col 0,row 0), q=(col 1,row 0); pitchX 290, pitchY 108, pad 60.
      const expected = <CanvasTopology, Map<String, Rect>>{
        CanvasTopology.leftRight: {
          'p': Rect.fromLTWH(60, 60, 200, 72),
          'q': Rect.fromLTWH(350, 60, 200, 72),
        },
        CanvasTopology.topDown: {
          'p': Rect.fromLTWH(60, 60, 200, 72),
          'q': Rect.fromLTWH(60, 168, 200, 72),
        },
        CanvasTopology.rightLeft: {
          'p': Rect.fromLTWH(350, 60, 200, 72),
          'q': Rect.fromLTWH(60, 60, 200, 72),
        },
        CanvasTopology.bottomUp: {
          'p': Rect.fromLTWH(60, 168, 200, 72),
          'q': Rect.fromLTWH(60, 60, 200, 72),
        },
      };
      for (final t in CanvasTopology.values) {
        await pumpView(tester,
            view: ClassicCanvasView(store: store, definition: pair, topology: t));
        await tester.pump(); // post-frame zoom-to-fit
        final scene = viewState(tester).scene!;
        expect(scene.topology, t);
        expect(scene.layout.cardOf('p')!.rect, expected[t]!['p'],
            reason: '$t places p per the table');
        expect(scene.layout.cardOf('q')!.rect, expected[t]!['q'],
            reason: '$t places q per the table');
      }
    });

    testWidgets('changing the topology widget param re-lays out and re-fits '
        'without spurious pulses', (tester) async {
      final pair = parseWorkflowDefinition('''
name: pair
nodes:
  p: {id: p, type: agent, model: draft}
  q: {id: q, type: agent, model: draft}
edges:
  - {from_id: p, to_id: q}
''');
      final store = RunStore(workflowDefinition: pair)
        ..seed(RunStatusView.fromJson({
          'run_id': runId,
          'workflow_name': 'pair',
          'state': 'completed',
          'started_at': ts,
          'nodes': <String, Object>{
            'p': {'status': 'completed', 'attempt': 1},
            'q': {'status': 'completed', 'attempt': 1},
          },
          'pending_inputs': <Object>[],
        }));

      await pumpView(tester,
          view: ClassicCanvasView(
              store: store, definition: pair, topology: CanvasTopology.leftRight));
      await tester.pump(); // post-frame zoom-to-fit
      // Capture the values (not the live State) before the re-pump refits.
      final lrScale = viewState(tester).scale;
      expect(lrScale, closeTo(800 / 490, 1e-3),
          reason: 'left→right bounds 490×72 fit 800×600 by width');
      expect(viewState(tester).scene!.layout.cardOf('q')!.rect.left, 350,
          reason: 'q sits to the right of p');

      // Same store + definition, new orientation: re-layout and re-fit.
      await pumpView(tester,
          view: ClassicCanvasView(
              store: store, definition: pair, topology: CanvasTopology.topDown));
      await tester.pump(); // post-frame zoom-to-fit in the new orientation
      final td = viewState(tester);
      expect(td.scene!.topology, CanvasTopology.topDown);
      expect(td.scene!.layout.cardOf('q')!.rect.top, 168,
          reason: 'q now sits below p (top→down)');
      expect(td.scale, closeTo(600 / 180, 1e-3),
          reason: 'the re-oriented bounds 200×180 re-fit 800×600 by height');
      expect(td.scale, isNot(closeTo(lrScale, 1e-6)),
          reason: 'the topology change re-fits the viewport');
      expect(td.pulses, isEmpty,
          reason: 'node statuses are unchanged → no FR-013 pulse fires');
    });

    testWidgets('the layout arrangement never changes under pan/zoom '
        'transforms for a workflow of a few dozen steps', (tester) async {
      final d = dozenDef();
      final store = RunStore(workflowDefinition: d)
        ..seed(RunStatusView.fromJson({
          'run_id': runId,
          'workflow_name': 'dozen',
          'state': 'running',
          'started_at': ts,
          'nodes': <String, Object>{
            'n0': {'status': 'running', 'attempt': 1, 'model': 'draft'},
          },
          'pending_inputs': <Object>[],
        }));
      await pumpView(
          tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump(); // post-frame zoom-to-fit
      final layout0 = viewState(tester).scene!.layout;
      expect(layout0.cards.length, 30);

      for (var i = 0; i < 12; i++) {
        await tester
            .drag(find.byType(ClassicCanvasView), Offset(37.0 - i * 7, 23.0 - i * 3));
        await tester.pump();
        await wheelZoom(tester, const Offset(400, 300), Offset(9.0, -13.0 * i));
      }
      await tester.drag(find.byType(ClassicCanvasView), const Offset(-220, -140));
      await tester.pump();

      final st = viewState(tester);
      expect(st.scene!.layout, layout0,
          reason: 'pan/zoom change the transform only, never the placement');
      expect(st.scene!.layout.cardOf('n0')!.rect,
          layout0.cardOf('n0')!.rect);
    });
  });

  group('keyboard viewport gestures + zoom controls (FR-016)', () {
    Future<void> pumpCanvas(WidgetTester tester) async {
      final d = def();
      final store = RunStore(workflowDefinition: d)
        ..seed(RunStatusView.fromJson(settledSnapshot()));
      await pumpView(
          tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump(); // post-frame zoom-to-fit + canvas focus
    }

    Offset worldAt(Offset focal, ClassicCanvasViewState st) =>
        Offset((focal.dx - st.pan.dx) / st.scale,
            (focal.dy - st.pan.dy) / st.scale);

    double fitScale(ClassicCanvasViewState st, Size size) {
      final b = st.scene!.layout.bounds;
      return math.min(size.width / b.width, size.height / b.height)
          .clamp(0.25, 4.0);
    }

    testWidgets('arrow keys pan the scene by 48 px in the arrow direction',
        (tester) async {
      await pumpCanvas(tester);
      final p0 = viewState(tester).pan;
      final scale0 = viewState(tester).scale;

      expect(await tester.sendKeyEvent(LogicalKeyboardKey.arrowRight), isTrue,
          reason: 'the canvas handles arrow keys');
      await tester.pump();
      expect(viewState(tester).pan, p0 + const Offset(48, 0));
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.pump();
      expect(viewState(tester).pan, p0 + const Offset(48, 48));
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowLeft);
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowUp);
      await tester.pump();
      expect(viewState(tester).pan, p0, reason: 'the round trip cancels');
      expect(viewState(tester).scale, scale0,
          reason: 'panning never changes the zoom');
    });

    testWidgets('Shift + arrow doubles the pan step', (tester) async {
      await pumpCanvas(tester);
      final p0 = viewState(tester).pan;
      await tester.sendKeyDownEvent(LogicalKeyboardKey.shiftLeft);
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowRight);
      await tester.sendKeyUpEvent(LogicalKeyboardKey.shiftLeft);
      await tester.pump();
      expect(viewState(tester).pan, p0 + const Offset(96, 0));
    });

    testWidgets('/ + / - zoom 1.25x anchored at the viewport center, '
        'clamped to 0.25-4', (tester) async {
      await pumpCanvas(tester);
      const center = Offset(400, 300); // 800x600 test surface
      final st0 = viewState(tester);
      final scale0 = st0.scale;
      final worldBefore = worldAt(center, st0);

      await tester.sendKeyEvent(LogicalKeyboardKey.equal);
      await tester.pump();
      final st1 = viewState(tester);
      expect(st1.scale, closeTo(scale0 * 1.25, 1e-9),
          reason: '/= zooms in by 1.25');
      expect(worldAt(center, st1).dx, closeTo(worldBefore.dx, 1e-6));
      expect(worldAt(center, st1).dy, closeTo(worldBefore.dy, 1e-6),
          reason: 'the viewport-center world point stays fixed');

      await tester.sendKeyEvent(LogicalKeyboardKey.minus);
      await tester.pump();
      expect(viewState(tester).scale, closeTo(scale0 * 1.25 / 1.25, 1e-9),
          reason: '- zooms back out by 1/1.25');

      for (var i = 0; i < 30; i++) {
        await tester.sendKeyEvent(LogicalKeyboardKey.minus);
      }
      await tester.pump();
      expect(viewState(tester).scale, closeTo(0.25, 1e-9),
          reason: 'keyboard zoom-out clamps at 0.25');
      for (var i = 0; i < 30; i++) {
        await tester.sendKeyEvent(LogicalKeyboardKey.equal);
      }
      await tester.pump();
      expect(viewState(tester).scale, closeTo(4.0, 1e-9),
          reason: 'keyboard zoom-in clamps at 4');
    });

    testWidgets('0 resets to 100% and F zooms to fit', (tester) async {
      await pumpCanvas(tester);
      const center = Offset(400, 300);

      await tester.sendKeyEvent(LogicalKeyboardKey.equal);
      await tester.pump();
      await tester.sendKeyEvent(LogicalKeyboardKey.digit0);
      await tester.pump();
      expect(viewState(tester).scale, closeTo(1.0, 1e-9),
          reason: 'digit0 resets the zoom to 100%');

      final st = viewState(tester);
      final worldBeforeFit = worldAt(center, st);
      expect(st.scene!, isNotNull);
      await tester.sendKeyEvent(LogicalKeyboardKey.keyF);
      await tester.pump();
      expect(viewState(tester).scale,
          closeTo(fitScale(st, const Size(800, 600)), 1e-9),
          reason: 'f zooms to fit');
      expect(worldAt(center, viewState(tester)).dx,
          closeTo(worldBeforeFit.dx, 1e-6),
          reason: 'fit re-centers the scene (center world point preserved)');
    });

    testWidgets('wheel zoom holds the world point under the cursor fixed',
        (tester) async {
      await pumpCanvas(tester);
      final st0 = viewState(tester);
      // Capture the scale value (not the live State) before the gesture mutates it.
      final scale0 = st0.scale;
      // Off-center, on empty canvas: right of the legend (x ≤ 240) and above
      // the scene (which the initial fit centers vertically).
      const cursor = Offset(500, 80);
      final worldBefore = worldAt(cursor, st0);

      await wheelZoom(tester, cursor, const Offset(0, -100));
      final st1 = viewState(tester);
      expect(st1.scale, closeTo(scale0 * math.pow(1.0015, 100).toDouble(), 1e-9),
          reason: 'scroll up zooms in by 1.0015^-dy');
      expect(worldAt(cursor, st1).dx, closeTo(worldBefore.dx, 1e-6),
          reason: 'zooming holds the world point under the cursor (x)');
      expect(worldAt(cursor, st1).dy, closeTo(worldBefore.dy, 1e-6),
          reason: 'zooming holds the world point under the cursor (y)');
    });

    testWidgets('zoom controls sit bottom-right and drive the view '
        'transform', (tester) async {
      await pumpCanvas(tester);
      final st0 = viewState(tester);
      final scale0 = st0.scale;

      expect(find.byType(ZoomControls), findsOneWidget);
      expect(tester.getBottomRight(find.byType(ZoomControls)),
          const Offset(792, 592),
          reason: 'pinned 8 px from the bottom-right of the 800x600 view');
      expect(find.text('${(scale0 * 100).round()}%'), findsOneWidget,
          reason: 'the label shows the current scale');

      // Tap the percentage label: reset to 100%.
      await tester.tap(find.text('${(scale0 * 100).round()}%'));
      await tester.pump();
      expect(viewState(tester).scale, closeTo(1.0, 1e-9));

      // Tap '-': zoom out 0.8x.
      await tester.tap(find.byIcon(Icons.remove));
      await tester.pump();
      expect(viewState(tester).scale, closeTo(0.8, 1e-9));

      // Tap '+': back to 100%.
      await tester.tap(find.byIcon(Icons.add));
      await tester.pump();
      expect(viewState(tester).scale, closeTo(1.0, 1e-9));

      // Tap fit: the scene fits.
      await tester.tap(find.byIcon(Icons.fit_screen));
      await tester.pump();
      expect(viewState(tester).scale,
          closeTo(fitScale(viewState(tester), const Size(800, 600)), 1e-9));
    });

    testWidgets('tapping the canvas re-acquires keyboard focus',
        (tester) async {
      await pumpCanvas(tester);
      // Move focus away, then a canvas tap must bring it back.
      FocusManager.instance.primaryFocus?.unfocus();
      await tester.pump();

      final p0 = viewState(tester).pan;
      // Empty canvas spot: above the scene (which occupies y 167–433 after
      // the initial fit) and right of the legend (x ≤ 240).
      await tester.tapAt(const Offset(400, 80));
      await tester.pump();
      expect(await tester.sendKeyEvent(LogicalKeyboardKey.arrowRight), isTrue,
          reason: 'after a canvas tap the keyboard works again');
      await tester.pump();
      expect(viewState(tester).pan, p0 + const Offset(48, 0));
    });
  });

  group('T033 stale, foreign-run and convergence (US5 acceptance 4)', () {
    testWidgets('stale: desaturated palette, paused motion, stale badge '
        '(FR-019)', (tester) async {
      final d = def();
      final store = RunStore(workflowDefinition: d)
        ..seed(RunStatusView.fromJson(settledSnapshot()));
      await pumpView(
          tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump(); // post-frame zoom-to-fit
      final base = darkClassic();
      expect(painterOf(tester).palette.background, base.background);

      store.markStale();
      await tester.pump();

      final desat = base.desaturated();
      expect(painterOf(tester).palette.background, desat.background,
          reason: 'the palette desaturates while stale');
      expect(
          painterOf(tester).palette.colorFor(NodeStatus.completed),
          desat.colorFor(NodeStatus.completed),
          reason: 'state colors desaturate while stale');
      expect(find.text('stale — waiting for server'), findsOneWidget,
          reason: 'the shared stale badge is shown');

      final frozen = viewState(tester).clock;
      await tester.pump(const Duration(milliseconds: 300));
      await tester.pump();
      expect(viewState(tester).clock, frozen,
          reason: 'the ticker clock is frozen while stale (motion paused)');
    });

    testWidgets('frames carrying another run’s run_id never alter the '
        'rendered scene (FR-021)', (tester) async {
      final d = def();
      final store = RunStore(workflowDefinition: d)
        ..seed(RunStatusView.fromJson(settledSnapshot()));
      final events = StreamController<EventFrame>.broadcast();
      store.connect(events.stream);
      await pumpView(
          tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump();
      final before = store.state!;
      expect(before.nodes['a']!.tokens, 12);

      events.add(EventFrame(
        eventType: 'node_completed',
        seq: 999,
        timestamp: tsDate,
        runId: 'other-run',
        payload: {'node_id': 'a', 'attempt': 1, 'tokens_used': 999, 'duration_seconds': 9.9},
      ));
      await tester.pump();

      expect(store.state, same(before),
          reason: 'the store drops the foreign frame (run-id guard)');
      expect(viewState(tester).scene!.state, same(before),
          reason: 'the rendered scene keeps the exact prior state');
      expect(viewState(tester).scene!.state.nodes['a']!.tokens, 12,
          reason: 'the foreign 999 tokens never land');
    });

    testWidgets('a snapshot re-synchronization converges the scene to the '
        'server’s state (FR-020)', (tester) async {
      final d = def();
      final store = RunStore(workflowDefinition: d)
        ..seed(RunStatusView.fromJson({
          'run_id': runId,
          'workflow_name': 'settled',
          'state': 'running',
          'started_at': ts,
          'nodes': <String, Object>{
            'a': {'status': 'running', 'attempt': 1, 'model': 'draft'},
          },
          'pending_inputs': <Object>[],
        }));
      await pumpView(
          tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump();

      // Missed frames + a dropped connection: the view is stale.
      store.markStale();
      await tester.pump();
      expect(find.text('stale — waiting for server'), findsOneWidget);

      // Reconnect: the authoritative snapshot wins over everything local.
      final fresh = RunStatusView.fromJson({
        'run_id': runId,
        'workflow_name': 'settled',
        'state': 'completed',
        'started_at': ts,
        'nodes': <String, Object>{
          'a': {
            'status': 'completed',
            'attempt': 1,
            'model': 'draft',
            'tokens': 55,
            'duration_seconds': 2.5,
          },
          'b': {'status': 'running', 'attempt': 1, 'model': 'draft'},
        },
        'pending_inputs': <Object>[],
      });
      await store.resync(runId, (id) async => fresh);
      await tester.pump();

      expect(store.state!.isStale, isFalse,
          reason: 'the seed clears the stale flag');
      expect(find.text('stale — waiting for server'), findsNothing);
      final scene = viewState(tester).scene!;
      expect(scene.state.nodes['a']!.status, NodeStatus.completed);
      expect(scene.state.nodes['a']!.tokens, 55);
      final c0 = viewState(tester).clock;
      await tester.pump(const Duration(milliseconds: 100));
      await tester.pump();
      expect(viewState(tester).clock, greaterThan(c0),
          reason: 'motion resumes after convergence');
    });
  });

  group('T034 HIL prompt in classic mode (US5 acceptance 2, FR-018)', () {
    testWidgets('the shared prompt card overlays the classic canvas and a '
        'submitted answer proceeds the step', (tester) async {
      SharedPreferences.setMockInitialValues(const {});
      tester.view.physicalSize = const Size(1280, 800);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      final hilDef = parseWorkflowDefinition('''
name: hil
nodes:
  approve: {id: approve, type: human_in_loop, model: draft}
  report: {id: report, type: agent, model: draft}
edges:
  - {from_id: approve, to_id: report}
''');
      final deadline = DateTime.now()
          .add(const Duration(minutes: 10))
          .toUtc()
          .toIso8601String();
      final requests = <String>[];
      var submitted = false;
      MockClient hilClient() => MockClient((request) async {
            requests.add('${request.method} ${request.url.path}');
            final headers = <String, String>{'content-type': 'application/json'};
            if (request.method == 'POST' &&
                request.url.path == '/runs/$runId/nodes/approve/input') {
              submitted = true;
              return http.Response(
                  jsonEncode({'run_id': runId, 'state': 'running', 'workflow': 'hil'}),
                  200,
                  headers: headers);
            }
            if (request.method == 'GET' && request.url.path == '/runs') {
              return http.Response(
                jsonEncode([
                  {
                    'run_id': runId,
                    'workflow_name': 'hil',
                    'started_at': ts,
                    'state': 'running',
                  },
                ]),
                200,
                headers: headers,
              );
            }
            if (request.method == 'GET' && request.url.path == '/runs/$runId') {
              final body = submitted
                  ? {
                      'run_id': runId,
                      'workflow_name': 'hil',
                      'state': 'completed',
                      'started_at': ts,
                      'nodes': <String, Object>{
                        'approve': {
                          'status': 'completed',
                          'attempt': 1,
                          'model': 'draft',
                          'tokens': 5,
                          'duration_seconds': 1.0,
                        },
                        'report': {'status': 'pending', 'attempt': 1},
                      },
                      'pending_inputs': <Object>[],
                    }
                  : {
                      'run_id': runId,
                      'workflow_name': 'hil',
                      'state': 'running',
                      'started_at': ts,
                      'nodes': <String, Object>{
                        'approve': {
                          'status': 'running',
                          'attempt': 1,
                          'model': 'draft',
                        },
                      },
                      'pending_inputs': [
                        {
                          'node_id': 'approve',
                          'prompt': 'Confirm the deployment?',
                          'deadline': deadline,
                        },
                      ],
                    };
              return http.Response(jsonEncode(body), 200, headers: headers);
            }
            if (request.method == 'GET' && request.url.path == '/health') {
              return http.Response(
                  jsonEncode({'status': 'ok', 'log_dir': 'runs'}),
                  200,
                  headers: headers);
            }
            return http.Response(
              jsonEncode({'error': {'code': 'unknown_run', 'message': 'nope'}}),
              404,
              headers: headers,
            );
          });

      final events = StreamController<EventFrame>.broadcast();
      final app = AppState()..setCanvasMode(CanvasMode.classic);
      final client = AgencyClient('http://fake', httpClient: hilClient());
      final entry =
          CatalogEntry(path: 'hil.yaml', name: 'hil', definition: hilDef);

      await pumpHome(
          tester, app: app, client: client, entry: entry, events: events);
      for (var i = 0; i < 6; i++) {
        await tester.pump();
      }

      expect(find.byType(ClassicCanvasView), findsOneWidget);
      expect(find.byType(HilPromptCard), findsOneWidget,
          reason: 'the shared prompt card overlays the classic canvas');
      expect(find.text('approve'), findsOneWidget);
      expect(find.text('Confirm the deployment?'), findsOneWidget);
      expect(find.textContaining('deadline in'), findsOneWidget);

      await tester.enterText(
          find.descendant(
              of: find.byType(HilPromptCard),
              matching: find.byType(TextField)),
          'ship it');
      await tester.pump();
      await tester.tap(find.widgetWithText(FilledButton, 'Submit'));
      for (var i = 0; i < 6; i++) {
        await tester.pump();
      }

      expect(submitted, isTrue,
          reason: 'the answer posts to the input endpoint');
      expect(requests, contains('POST /runs/$runId/nodes/approve/input'));
      expect(find.byType(HilPromptCard), findsNothing,
          reason: 'the resync snapshot drops the card once the input resolves');
      expect(find.byType(ClassicCanvasView), findsOneWidget);
    });
  });

  // ---- T040 spec edge cases (polish) ---------------------------------------

  /// The id and model label geometry `ClassicPainter` lays out for a card,
  /// with the painter's exact TextPainter parameters: the header id gets the
  /// space between the type icon and the state label (plus the fb chip when
  /// present); the body model row gets the card width minus padding. Both
  /// are laid out `maxLines: 1, ellipsis: '…'`, so a constrained width that
  /// falls short of the intrinsic width means the card shows an ellipsized
  /// label — the full text only lives in the scene/inspector.
  ({double idIntrinsic, double idPainted, double modelIntrinsic, double modelPainted})
      cardLabelWidths(
    CardRect card,
    SNodeState node,
    ClassicStateEncoding encoding,
  ) {
    final idSpan = TextSpan(
      text: node.nodeId,
      style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w600),
    );
    TextPainter spanTp(
      InlineSpan span, {
      double maxWidth = double.infinity,
      bool ellipsize = false,
    }) {
      final tp = TextPainter(
        text: span,
        textDirection: TextDirection.ltr,
        maxLines: ellipsize ? 1 : null,
        ellipsis: '…',
      );
      tp.layout(maxWidth: maxWidth);
      return tp;
    }

    final idIntrinsic = spanTp(idSpan).width;
    final model = node.model ?? '—';
    final modelSpan = TextSpan(
      text: model,
      style: const TextStyle(fontSize: 11),
    );
    final modelIntrinsic = spanTp(modelSpan).width;
    final modelPainted =
        spanTp(modelSpan, maxWidth: card.rect.width - 20, ellipsize: true).width;

    final labelTp = TextPainter(
      text: TextSpan(
        text: encoding.label,
        style: const TextStyle(fontSize: 11),
      ),
      textDirection: TextDirection.ltr,
    )..layout();
    final chipW = node.status == NodeStatus.completedFallback ? 20.0 : 0.0;
    final idX = card.rect.left + 10 + 14 + 6;
    final labelX = card.rect.right - 10 - labelTp.width - chipW;
    final idPainted = spanTp(
      idSpan,
      maxWidth: labelX - 6 - idX,
      ellipsize: true,
    ).width;

    return (
      idIntrinsic: idIntrinsic,
      idPainted: idPainted,
      modelIntrinsic: modelIntrinsic,
      modelPainted: modelPainted,
    );
  }

  group('T040 spec edge cases (polish)', () {
    const longId = 'step-generate-quarterly-report-0123456789abcdef';
    const longModel = 'local-llama-model-very-long-model-name-0123456789';

    testWidgets('very long step id / model name: ellipsized on the card, '
        'full text available in the inspector', (tester) async {
      final d = parseWorkflowDefinition('''
name: longnames
nodes:
  $longId: {id: $longId, type: agent, model: draft}
''');
      final store = RunStore(workflowDefinition: d)
        ..seed(RunStatusView.fromJson({
          'run_id': runId,
          'workflow_name': 'longnames',
          'state': 'completed',
          'started_at': ts,
          'nodes': <String, Object>{
            longId: {
              'status': 'completed',
              'attempt': 1,
              'model': longModel,
              'tokens': 10,
              'duration_seconds': 1.0,
            },
          },
          'pending_inputs': <Object>[],
        }));

      String? inspected;
      await tester.pumpWidget(MaterialApp(
        theme: themeDataFor(ThemeMode.dark),
        home: Scaffold(
          body: ClassicCanvasView(
            store: store,
            definition: d,
            onInspect: (id) => inspected = id,
            topology: CanvasTopology.leftRight,
          ),
        ),
      ));
      await tester.pump(); // post-frame zoom-to-fit

      final scene = viewState(tester).scene!;
      final node = scene.state.nodes[longId]!;
      expect(node.nodeId, longId, reason: 'the full id survives in the scene');
      expect(node.model, longModel,
          reason: 'the full model name survives in the scene');

      final widths = cardLabelWidths(
        scene.layout.cardOf(longId)!,
        node,
        scene.encodings[longId]!,
      );
      expect(widths.idIntrinsic, greaterThan(widths.idPainted),
          reason: 'the long id does not fit the card header → ellipsized '
              'on the card (maxLines 1, ellipsis "…")');
      expect(widths.modelIntrinsic, greaterThan(widths.modelPainted),
          reason: 'the long model name does not fit the card body row → '
              'ellipsized on the card');

      // Tapping the card inspects by full id ...
      await tester.tapAt(cardScreenCenter(tester, longId));
      await tester.pump();
      expect(inspected, longId);

      // ... and the shared inspector (fed the scene's node, exactly as the
      // home page does) carries the full text.
      await tester.pumpWidget(MaterialApp(
        theme: themeDataFor(ThemeMode.dark),
        home: Scaffold(
          body: Stack(
            children: [
              const SizedBox.expand(),
              Inspector(node: store.state!.nodes[longId]!, onClose: () {}),
            ],
          ),
        ),
      ));
      expect(find.text(longId.toUpperCase()), findsOneWidget,
          reason: 'the inspector title shows the full id (uppercase)');
      expect(find.text(longModel), findsOneWidget,
          reason: 'the inspector Model row carries the full model name');
    });

    testWidgets('failed run with many downstream skips: the failed step is '
        'distinct, the skips are dimmed and clearly not failed',
        (tester) async {
      const n = 10;
      final d = parseWorkflowDefinition((() {
        final buf = StringBuffer('name: chain\nnodes:\n');
        for (var i = 0; i < n; i++) {
          buf.write('  step$i: {id: step$i, type: agent, model: draft}\n');
        }
        buf.write('edges:\n');
        for (var i = 0; i < n - 1; i++) {
          buf.write('  - {from_id: step$i, to_id: step${i + 1}}\n');
        }
        return buf.toString();
      })());
      const failedAt = 1;
      final nodes = <String, Object>{};
      for (var i = 0; i < n; i++) {
        final id = 'step$i';
        nodes[id] = i < failedAt
            ? {
                'status': 'completed',
                'attempt': 1,
                'tokens': 10,
                'duration_seconds': 1.0,
                'model': 'draft'
              }
            : i == failedAt
                ? {'status': 'failed', 'attempt': 1, 'reason': 'step exploded'}
                : {
                    'status': 'skipped',
                    'attempt': 1,
                    'reason': 'upstream_failed'
                  };
      }
      final store = RunStore(workflowDefinition: d)
        ..seed(RunStatusView.fromJson({
          'run_id': runId,
          'workflow_name': 'chain',
          'state': 'failed',
          'started_at': ts,
          'nodes': nodes,
          'pending_inputs': <Object>[],
        }));

      await pumpView(tester, view: ClassicCanvasView(store: store, definition: d, topology: CanvasTopology.leftRight));
      await tester.pump();

      final scene = viewState(tester).scene!;
      expect(scene.layout.cards.keys.length, n,
          reason: 'every step of the failed run is rendered');

      final failed = scene.encodings['step$failedAt']!;
      expect(failed.marker, ClassicMarker.cross);
      expect(failed.label, 'failed');
      expect(scene.state.nodes['step$failedAt']!.error, 'step exploded',
          reason: 'the failure reason stays inspectable');

      for (var i = failedAt + 1; i < n; i++) {
        final enc = scene.encodings['step$i']!;
        expect(enc.marker, ClassicMarker.dim,
            reason: 'downstream step$i is dimmed');
        expect(enc.label, 'skipped');
        expect(enc.animated, isFalse,
            reason: 'skipped cards are motionless');
        expect(
            enc.marker != failed.marker && enc.label != failed.label, isTrue,
            reason: 'skipped is distinguishable from failed without color');
      }
      expect(
          scene.encodings.values
              .where((e) => e.marker == ClassicMarker.cross)
              .length,
          1,
          reason: 'exactly one card — the failed step — carries the cross');
    });
  });

  group('T041 node selection (classic canvas)', () {
    testWidgets('the selected card is highlighted through the painter; the '
        'selection moves with the inspected node and clears', (tester) async {
      final d = def();
      final store = RunStore(workflowDefinition: d)
        ..seed(RunStatusView.fromJson(settledSnapshot()));

      await pumpView(
          tester,
          view: ClassicCanvasView(
            store: store,
            definition: d,
            topology: CanvasTopology.leftRight,
            selectedNodeId: 'b',
          ));
      await tester.pump(); // post-frame zoom-to-fit
      expect(painterOf(tester).selectedNodeId, 'b',
          reason: 'the selected card carries the highlight');

      // Single selection: the highlight follows the inspected node.
      await pumpView(
          tester,
          view: ClassicCanvasView(
            store: store,
            definition: d,
            topology: CanvasTopology.leftRight,
            selectedNodeId: 'd',
          ));
      await tester.pump();
      expect(painterOf(tester).selectedNodeId, 'd');

      // No selection by default.
      await pumpView(
          tester,
          view: ClassicCanvasView(
            store: store,
            definition: d,
            topology: CanvasTopology.leftRight,
          ));
      await tester.pump();
      expect(painterOf(tester).selectedNodeId, isNull,
          reason: 'nothing is selected by default');
    });

    testWidgets('a card tap selects its node; an empty-canvas tap reports '
        'null (clears the selection)', (tester) async {
      final d = def();
      final store = RunStore(workflowDefinition: d)
        ..seed(RunStatusView.fromJson(settledSnapshot()));

      final List<String?> reported = [];
      await pumpView(
          tester,
          view: ClassicCanvasView(
            store: store,
            definition: d,
            topology: CanvasTopology.leftRight,
            onInspect: reported.add,
          ));
      await tester.pump(); // post-frame zoom-to-fit

      // A card tap selects the node under the pointer.
      await tester.tapAt(cardScreenCenter(tester, 'b'));
      await tester.pump();
      expect(reported, const ['b']);

      // An empty-canvas tap (above the fitted scene, right of the legend)
      // reports null so the selection clears.
      await tester.tapAt(const Offset(400, 80));
      await tester.pump();
      expect(reported, const ['b', null]);
    });
  });
}
