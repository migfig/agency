import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;

import 'package:agencyapp/api/agency_client.dart';
import 'package:agencyapp/api/models.dart';
import 'package:agencyapp/app/app_state.dart';
import 'package:agencyapp/app/home_page.dart';
import 'package:agencyapp/app/sidebar/history_list.dart';
import 'package:agencyapp/canvas/canvas_scene.dart';
import 'package:agencyapp/canvas/canvas_view.dart';
import 'package:agencyapp/canvas/classic/classic_encodings.dart';
import 'package:agencyapp/canvas/classic/classic_scene.dart';
import 'package:agencyapp/canvas/classic/classic_view.dart';
import 'package:agencyapp/canvas/inspector.dart';
import 'package:agencyapp/canvas/scene_painter.dart';
import 'package:agencyapp/canvas/state_encodings.dart';
import 'package:agencyapp/catalog/catalog_entry.dart';
import 'package:agencyapp/catalog/workflow_def.dart';
import 'package:agencyapp/theme/theme_data.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

/// T031 — history render (US2): a terminal run renders from
/// `GET /runs/{id}` + `GET /runs/{id}/result` with every step in its terminal
/// status, metrics, per-node output, and failure reasons inspectable; a
/// failed run distinguishes the failed node from downstream consequences;
/// node ids no longer in the (edited) definition still render in an orphan
/// column (FR-012).
///
/// No filesystem, no real network, no wall-clock dependency.
void main() {
  const oldTs = '2026-08-29T08:00:00Z';
  const newTs = '2026-08-29T10:00:00Z';

  WorkflowDefinition demoDef() => parseWorkflowDefinition('''
name: demo
nodes:
  a: {id: a, type: agent, model: draft}
  b: {id: b, type: agent, model: draft}
edges:
  - {from_id: a, to_id: b}
''');

  WorkflowDefinition demo2Def() => parseWorkflowDefinition('''
name: demo2
nodes:
  a: {id: a, type: agent, model: draft}
  b: {id: b, type: agent, model: draft}
edges:
  - {from_id: a, to_id: b}
''');

  MockClient fakeClient({
    required List<RunHistoryEntry> history,
    Map<String, Map<String, Object>> statusByRun = const {},
    Map<String, Map<String, Object>> resultByRun = const {},
  }) {
    const headers = <String, String>{'content-type': 'application/json'};
    return MockClient((request) async {
      final path = request.url.path;
      if (request.method == 'GET' && path == '/runs') {
        return http.Response(
          jsonEncode([
            for (final e in history)
              {
                'run_id': e.runId,
                'workflow_name': e.workflowName,
                'started_at': e.startedAt.toUtc().toIso8601String(),
                'state': e.state,
              },
          ]),
          200,
          headers: headers,
        );
      }
      final m = RegExp(r'^/runs/([^/]+)(/result)?$').firstMatch(path);
      if (request.method == 'GET' && m != null) {
        final id = m.group(1)!;
        final body = m.group(2) != null ? resultByRun[id] : statusByRun[id];
        if (body == null) {
          return http.Response(
            jsonEncode({'error': {'code': 'unknown_run', 'message': 'nope'}}),
            404,
            headers: headers,
          );
        }
        return http.Response(jsonEncode(body), 200, headers: headers);
      }
      return http.Response(
        jsonEncode({'error': {'code': 'unknown_run', 'message': 'nope'}}),
        404,
        headers: headers,
      );
    });
  }

  Future<void> settle(WidgetTester tester) async {
    for (var i = 0; i < 6; i++) {
      await tester.pump();
    }
  }

  Future<void> pumpHome(
    WidgetTester tester, {
    required AppState app,
    required AgencyClient client,
    required List<CatalogEntry> entries,
    required StreamController<EventFrame> events,
  }) async {
    await tester.pumpWidget(MaterialApp(
      theme: themeDataFor(ThemeMode.dark),
      home: HomePage(
        app: app,
        client: client,
        entries: entries,
        eventStream: events.stream,
      ),
    ));
  }

  CanvasScene sceneOf(WidgetTester tester) =>
      tester.state<CanvasViewState>(find.byType(CanvasView)).scene!;

  ClassicScene classicSceneOf(WidgetTester tester) =>
      tester.state<ClassicCanvasViewState>(find.byType(ClassicCanvasView)).scene!;

  /// Tap a node at exactly the screen position the deterministic
  /// zoom-to-fit places it (mirrors CanvasViewState._zoomToFit using the
  /// public [layoutBounds] / [worldPositionOf]).
  Future<void> tapNode(WidgetTester tester, String nodeId) async {
    final canvas = find.byType(CanvasView);
    final scene = tester.state<CanvasViewState>(canvas).scene!;
    final size = tester.getSize(canvas);
    final origin = tester.getTopLeft(canvas);
    final bounds = layoutBounds(scene.layout);
    final scale = math.min(size.width / bounds.width, size.height / bounds.height)
        .clamp(0.25, 4.0)
        .toDouble();
    final pan = Offset(
      size.width / 2 - (bounds.width * scale) / 2,
      size.height / 2 - (bounds.height * scale) / 2,
    );
    final world = worldPositionOf(scene.layout.positionOf(nodeId));
    await tester.tapAt(origin + world * scale + pan);
    await tester.pump();
  }

  Finder historyRow(String runId) =>
      find.descendant(of: find.byType(HistoryList), matching: find.text(runId));

  group('T031 history render', () {
    testWidgets('(a) a completed run renders every step terminally with result details',
        (tester) async {
      final d = demoDef();
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState();
      final client = AgencyClient(
        'http://fake',
        httpClient: fakeClient(
          history: [
            RunHistoryEntry(
                runId: 'r-old',
                workflowName: 'demo',
                startedAt: DateTime.parse(oldTs).toUtc(),
                state: 'completed'),
            RunHistoryEntry(
                runId: 'r-new',
                workflowName: 'demo',
                startedAt: DateTime.parse(newTs).toUtc(),
                state: 'running'),
          ],
          statusByRun: {
            'r-old': {
              'run_id': 'r-old',
              'workflow_name': 'demo',
              'state': 'completed',
              'started_at': oldTs,
              'nodes': <String, Object>{
                'a': {
                  'status': 'completed',
                  'attempt': 1,
                  'tokens': 100,
                  'duration_seconds': 1.0,
                  'model': 'draft',
                },
                'b': {
                  'status': 'completed_fallback',
                  'attempt': 2,
                  'tokens': 20,
                  'duration_seconds': 2.0,
                  'model': 'fb-model',
                  'fallback': 'fb-model',
                },
                'c': {'status': 'failed', 'attempt': 1, 'reason': 'boom'},
                'd': {
                  'status': 'skipped',
                  'attempt': 1,
                  'reason': 'unresolved_binding',
                },
              },
              'pending_inputs': <Object>[],
            },
            'r-new': {
              'run_id': 'r-new',
              'workflow_name': 'demo',
              'state': 'running',
              'started_at': newTs,
              'nodes': <String, Object>{
                'a': {'status': 'running', 'attempt': 1, 'model': 'draft'},
              },
              'pending_inputs': <Object>[],
            },
          },
          resultByRun: {
            'r-old': {
              'run_id': 'r-old',
              'status': 'completed',
              'total_tokens': 130,
              'duration_seconds': 12.5,
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
                  'attempt': 2,
                  'tokens': 20,
                  'duration_seconds': 2.0,
                  'fallback': 'fb-model',
                },
                {'node_id': 'c', 'status': 'failed', 'attempt': 1, 'reason': 'boom'},
                {
                  'node_id': 'd',
                  'status': 'skipped',
                  'attempt': 1,
                  'reason': 'unresolved_binding',
                },
              ],
            },
          },
        ),
      );
      final entry = CatalogEntry(path: 'demo.yaml', name: 'demo', definition: d);

      await pumpHome(tester,
          app: app, client: client, entries: [entry], events: events);
      await settle(tester);

      expect(app.selectedRunId, 'r-new',
          reason: 'the newest run is auto-selected on startup');
      expect(sceneOf(tester).encodingsByNode['a'],
          kStateEncodings[NodeStatus.running]);

      await tester.tap(historyRow('r-old'));
      await settle(tester);

      expect(app.selectedRunId, 'r-old');
      final st = app.storeFor('r-old').state!;
      expect(st.runStatus, 'completed');
      expect(st.nodes['a']!.status, NodeStatus.completed);
      expect(st.nodes['a']!.tokens, 100);
      expect(st.nodes['a']!.durationSeconds, 1.0);
      expect(st.nodes['a']!.output, 'alpha',
          reason: 'per-node output comes from the result view');
      expect(st.nodes['b']!.status, NodeStatus.completedFallback);
      expect(st.nodes['b']!.onFallbackPath, isTrue);
      expect(st.nodes['c']!.status, NodeStatus.failed);
      expect(st.nodes['c']!.error, 'boom');
      expect(st.nodes['d']!.status, NodeStatus.skipped);
      expect(st.nodes['d']!.skipReason, 'unresolved_binding');

      final scene = sceneOf(tester);
      expect(scene.encodingsByNode['a'], kStateEncodings[NodeStatus.completed]);
      expect(scene.encodingsByNode['b'],
          kStateEncodings[NodeStatus.completedFallback]);
      expect(scene.encodingsByNode['c'], kStateEncodings[NodeStatus.failed]);
      expect(scene.encodingsByNode['d'], kStateEncodings[NodeStatus.skipped]);

      expect(find.text('130 tokens'), findsOneWidget,
          reason: 'the summary bar shows the API total, not the node sum (120)');

      await tapNode(tester, 'c');
      expect(find.text('boom'), findsOneWidget,
          reason: 'failure reason is inspectable from the canvas');

      await tester.tap(historyRow('r-new'));
      await settle(tester);

      expect(app.selectedRunId, 'r-new');
      expect(sceneOf(tester).encodingsByNode['a'],
          kStateEncodings[NodeStatus.running],
          reason: 'switching back resumes the live run');
    });

    testWidgets(
        '(b) a failed run distinguishes the failed node from downstream consequences',
        (tester) async {
      final d = demoDef();
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState();
      final client = AgencyClient(
        'http://fake',
        httpClient: fakeClient(
          history: [
            RunHistoryEntry(
                runId: 'f-run',
                workflowName: 'demo',
                startedAt: DateTime.parse(newTs).toUtc(),
                state: 'failed'),
          ],
          statusByRun: {
            'f-run': {
              'run_id': 'f-run',
              'workflow_name': 'demo',
              'state': 'failed',
              'started_at': newTs,
              'nodes': <String, Object>{
                'a': {
                  'status': 'completed',
                  'attempt': 1,
                  'tokens': 100,
                  'duration_seconds': 1.0,
                  'model': 'draft',
                },
                'b': {'status': 'failed', 'attempt': 1, 'reason': 'b exploded'},
                'c': {
                  'status': 'skipped',
                  'attempt': 1,
                  'reason': 'unresolved_binding',
                },
                'd': {
                  'status': 'skipped',
                  'attempt': 1,
                  'reason': 'unresolved_binding',
                },
              },
              'pending_inputs': <Object>[],
            },
          },
          resultByRun: {
            'f-run': {
              'run_id': 'f-run',
              'status': 'failed',
              'total_tokens': 110,
              'duration_seconds': 3.2,
              'nodes': <Object>[
                {
                  'node_id': 'a',
                  'status': 'completed',
                  'attempt': 1,
                  'tokens': 100,
                  'duration_seconds': 1.0,
                  'output': 'a-out',
                },
                {'node_id': 'b', 'status': 'failed', 'attempt': 1, 'reason': 'b exploded'},
                {
                  'node_id': 'c',
                  'status': 'skipped',
                  'attempt': 1,
                  'reason': 'unresolved_binding',
                },
                {
                  'node_id': 'd',
                  'status': 'skipped',
                  'attempt': 1,
                  'reason': 'unresolved_binding',
                },
              ],
            },
          },
        ),
      );
      final entry = CatalogEntry(path: 'demo.yaml', name: 'demo', definition: d);

      await pumpHome(tester,
          app: app, client: client, entries: [entry], events: events);
      await settle(tester);

      expect(app.selectedRunId, 'f-run');
      final st = app.storeFor('f-run').state!;
      expect(st.runStatus, 'failed',
          reason: 'the summary bar shows the API state as-is');
      expect(st.nodes['a']!.output, 'a-out');
      expect(st.nodes['b']!.status, NodeStatus.failed);
      expect(st.nodes['b']!.error, 'b exploded');
      expect(st.nodes['c']!.status, NodeStatus.skipped);
      expect(st.nodes['d']!.status, NodeStatus.skipped);

      final scene = sceneOf(tester);
      expect(scene.encodingsByNode['b'], kStateEncodings[NodeStatus.failed]);
      expect(scene.encodingsByNode['c'], kStateEncodings[NodeStatus.skipped]);
      expect(find.text('110 tokens'), findsOneWidget,
          reason: 'API total (110), not the node sum (100)');

      await tapNode(tester, 'b');
      expect(find.text('b exploded'), findsOneWidget,
          reason: 'the failed node is inspectable');
    });

    testWidgets(
        '(c) nodes absent from the (edited) definition render in an orphan column',
        (tester) async {
      final d2 = demo2Def();
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState();
      final client = AgencyClient(
        'http://fake',
        httpClient: fakeClient(
          history: [
            RunHistoryEntry(
                runId: 'o-run',
                workflowName: 'demo2',
                startedAt: DateTime.parse(newTs).toUtc(),
                state: 'completed'),
          ],
          statusByRun: {
            'o-run': {
              'run_id': 'o-run',
              'workflow_name': 'demo2',
              'state': 'completed',
              'started_at': newTs,
              'nodes': <String, Object>{
                'a': {
                  'status': 'completed',
                  'attempt': 1,
                  'tokens': 10,
                  'duration_seconds': 1.0,
                  'model': 'draft',
                },
                'b': {
                  'status': 'completed',
                  'attempt': 1,
                  'tokens': 10,
                  'duration_seconds': 1.0,
                  'model': 'draft',
                },
                'x': {
                  'status': 'completed',
                  'attempt': 1,
                  'tokens': 25,
                  'duration_seconds': 1.5,
                  'model': 'draft',
                },
              },
              'pending_inputs': <Object>[],
            },
          },
          resultByRun: {
            'o-run': {
              'run_id': 'o-run',
              'status': 'completed',
              'total_tokens': 45,
              'duration_seconds': 4.0,
              'nodes': <Object>[
                {
                  'node_id': 'a',
                  'status': 'completed',
                  'attempt': 1,
                  'tokens': 10,
                  'output': 'a-out',
                },
                {
                  'node_id': 'b',
                  'status': 'completed',
                  'attempt': 1,
                  'tokens': 10,
                  'output': 'b-out',
                },
                {
                  'node_id': 'x',
                  'status': 'completed',
                  'attempt': 1,
                  'tokens': 25,
                  'output': 'xout',
                },
              ],
            },
          },
        ),
      );
      final entry = CatalogEntry(path: 'demo2.yaml', name: 'demo2', definition: d2);

      await pumpHome(tester,
          app: app, client: client, entries: [entry], events: events);
      await settle(tester);

      expect(app.selectedRunId, 'o-run');
      final scene = sceneOf(tester);
      for (final id in ['a', 'b', 'x']) {
        expect(scene.layout.positions.containsKey(id), isTrue,
            reason: 'node $id renders despite being absent from the definition');
      }
      final x = scene.layout.positionOf('x');
      expect(x.col, scene.layout.columnCount - 1,
          reason: 'orphan nodes sit in a trailing column');
      expect(worldPositionOf(x).dx,
          greaterThan(worldPositionOf(scene.layout.positionOf('b')).dx));
      expect(app.storeFor('o-run').state!.nodes['x']!.output, 'xout');
      expect(find.text('45 tokens'), findsOneWidget);
    });
  });

  /// T024 — classic-mode history render (US3): any past run renders its
  /// complete settled state from the server's run data alone (no live
  /// stream) — terminal status, type, model, tokens, duration, attempt
  /// count, fallback used, and failure/skip reason per step (FR-014); a
  /// failed run distinguishes the failed step, dims downstream skips, and
  /// keeps the reason inspectable (FR-012); a fallback step carries the
  /// fb chip and the inspector names the fallback model (FR-011).
  group('T024 classic history render (US3)', () {
    testWidgets(
        '(a) two finished runs: selecting each renders every step terminally '
        'with its metrics from stored data alone', (tester) async {
      final d = demoDef();
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState();
      final client = AgencyClient(
        'http://fake',
        httpClient: fakeClient(
          history: [
            RunHistoryEntry(
                runId: 'h-old',
                workflowName: 'demo',
                startedAt: DateTime.parse(oldTs).toUtc(),
                state: 'completed'),
            RunHistoryEntry(
                runId: 'h-term',
                workflowName: 'demo',
                startedAt: DateTime.parse(newTs).toUtc(),
                state: 'completed'),
          ],
          statusByRun: {
            'h-old': {
              'run_id': 'h-old',
              'workflow_name': 'demo',
              'state': 'completed',
              'started_at': oldTs,
              'nodes': <String, Object>{
                'a': {
                  'status': 'completed',
                  'attempt': 1,
                  'tokens': 10,
                  'duration_seconds': 1.0,
                  'model': 'draft',
                },
                'b': {'status': 'skipped', 'attempt': 1, 'reason': 'condition false'},
              },
              'pending_inputs': <Object>[],
            },
            'h-term': {
              'run_id': 'h-term',
              'workflow_name': 'demo',
              'state': 'completed',
              'started_at': newTs,
              'nodes': <String, Object>{
                'a': {
                  'status': 'completed',
                  'attempt': 1,
                  'tokens': 100,
                  'duration_seconds': 1.0,
                  'model': 'draft',
                },
                'b': {
                  'status': 'completed_fallback',
                  'attempt': 2,
                  'tokens': 20,
                  'duration_seconds': 2.0,
                  'model': 'fb-model',
                  'fallback': 'fb-model',
                },
                'c': {'status': 'failed', 'attempt': 1, 'reason': 'boom'},
                'd': {
                  'status': 'skipped',
                  'attempt': 1,
                  'reason': 'unresolved_binding',
                },
              },
              'pending_inputs': <Object>[],
            },
          },
          resultByRun: {
            'h-old': {
              'run_id': 'h-old',
              'status': 'completed',
              'total_tokens': 10,
              'duration_seconds': 1.0,
              'nodes': <Object>[
                {
                  'node_id': 'a',
                  'status': 'completed',
                  'attempt': 1,
                  'tokens': 10,
                  'duration_seconds': 1.0,
                  'output': 'old-a',
                },
                {
                  'node_id': 'b',
                  'status': 'skipped',
                  'attempt': 1,
                  'reason': 'condition false',
                },
              ],
            },
            'h-term': {
              'run_id': 'h-term',
              'status': 'completed',
              'total_tokens': 130,
              'duration_seconds': 12.5,
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
                  'attempt': 2,
                  'tokens': 20,
                  'duration_seconds': 2.0,
                  'fallback': 'fb-model',
                },
                {
                  'node_id': 'c',
                  'status': 'failed',
                  'attempt': 1,
                  'reason': 'boom',
                },
                {
                  'node_id': 'd',
                  'status': 'skipped',
                  'attempt': 1,
                  'reason': 'unresolved_binding',
                },
              ],
            },
          },
        ),
      );
      final entry = CatalogEntry(path: 'demo.yaml', name: 'demo', definition: d);

      await pumpHome(tester,
          app: app, client: client, entries: [entry], events: events);
      await settle(tester);

      expect(app.selectedRunId, 'h-term',
          reason: 'the newest finished run is auto-selected');

      // Switch to the classic view: the same store renders classically.
      app.setCanvasMode(CanvasMode.classic);
      await tester.pump();
      await tester.pump(); // post-frame zoom-to-fit
      expect(find.byType(ClassicCanvasView), findsOneWidget);

      final st = app.storeFor('h-term').state!;
      final scene = classicSceneOf(tester);
      expect(scene.runId, 'h-term');

      // Every step is placed and carries its terminal encoding.
      expect(scene.layout.cards.keys.toSet(), {'a', 'b', 'c', 'd'});
      for (final e in st.nodes.entries) {
        expect(scene.encodings[e.key], kClassicStateEncodings[e.value.status],
            reason: '${e.key} shows its terminal state');
      }
      expect(st.nodes['a']!.status, NodeStatus.completed);
      expect(st.nodes['b']!.status, NodeStatus.completedFallback);
      expect(st.nodes['c']!.status, NodeStatus.failed);
      expect(st.nodes['d']!.status, NodeStatus.skipped);

      // The terminal metrics the card renders: model · tokens · duration ·
      // attempts, plus the failure/skip reasons where relevant.
      expect(st.nodes['a']!.model, 'draft');
      expect(st.nodes['a']!.tokens, 100);
      expect(st.nodes['a']!.durationSeconds, 1.0);
      expect(st.nodes['a']!.attempt, 1);
      expect(st.nodes['b']!.model, 'fb-model');
      expect(st.nodes['b']!.attempt, 2);
      expect(st.nodes['c']!.error, 'boom');
      expect(st.nodes['d']!.skipReason, 'unresolved_binding');
      // Per-node output from the result view.
      expect(st.nodes['a']!.output, 'alpha');

      // Settled from the stored run data alone: no live frames applied.
      expect(st.lastEventSeq, isNull,
          reason: 'no live stream is required for a history run');

      // Selecting the other finished run re-renders it classically.
      await tester.tap(historyRow('h-old'));
      await settle(tester);
      expect(app.selectedRunId, 'h-old');
      final stOld = app.storeFor('h-old').state!;
      final sceneOld = classicSceneOf(tester);
      expect(sceneOld.runId, 'h-old');
      expect(sceneOld.layout.cards.keys.toSet(), {'a', 'b'});
      expect(sceneOld.encodings['a'], kClassicStateEncodings[NodeStatus.completed]);
      expect(sceneOld.encodings['b'], kClassicStateEncodings[NodeStatus.skipped]);
      expect(stOld.nodes['a']!.tokens, 10);
      expect(stOld.nodes['a']!.output, 'old-a');
      expect(stOld.nodes['b']!.skipReason, 'condition false');
      expect(stOld.lastEventSeq, isNull);
    });

    testWidgets(
        '(b) a failed run distinguishes the failed step, dims downstream '
        'skips, and keeps the reason inspectable', (tester) async {
      final d = demoDef();
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState();
      final client = AgencyClient(
        'http://fake',
        httpClient: fakeClient(
          history: [
            RunHistoryEntry(
                runId: 'h-fail',
                workflowName: 'demo',
                startedAt: DateTime.parse(newTs).toUtc(),
                state: 'failed'),
          ],
          statusByRun: {
            'h-fail': {
              'run_id': 'h-fail',
              'workflow_name': 'demo',
              'state': 'failed',
              'started_at': newTs,
              'nodes': <String, Object>{
                'a': {
                  'status': 'completed',
                  'attempt': 1,
                  'tokens': 100,
                  'duration_seconds': 1.0,
                  'model': 'draft',
                },
                'b': {'status': 'failed', 'attempt': 1, 'reason': 'b exploded'},
                'c': {
                  'status': 'skipped',
                  'attempt': 1,
                  'reason': 'unresolved_binding',
                },
                'd': {
                  'status': 'skipped',
                  'attempt': 1,
                  'reason': 'unresolved_binding',
                },
              },
              'pending_inputs': <Object>[],
            },
          },
          resultByRun: {
            'h-fail': {
              'run_id': 'h-fail',
              'status': 'failed',
              'total_tokens': 110,
              'duration_seconds': 3.2,
              'nodes': <Object>[
                {
                  'node_id': 'a',
                  'status': 'completed',
                  'attempt': 1,
                  'tokens': 100,
                  'duration_seconds': 1.0,
                  'output': 'a-out',
                },
                {
                  'node_id': 'b',
                  'status': 'failed',
                  'attempt': 1,
                  'reason': 'b exploded',
                },
                {
                  'node_id': 'c',
                  'status': 'skipped',
                  'attempt': 1,
                  'reason': 'unresolved_binding',
                },
                {
                  'node_id': 'd',
                  'status': 'skipped',
                  'attempt': 1,
                  'reason': 'unresolved_binding',
                },
              ],
            },
          },
        ),
      );
      final entry = CatalogEntry(path: 'demo.yaml', name: 'demo', definition: d);

      await pumpHome(tester,
          app: app, client: client, entries: [entry], events: events);
      await settle(tester);
      expect(app.selectedRunId, 'h-fail');

      app.setCanvasMode(CanvasMode.classic);
      await tester.pump();
      await tester.pump();
      expect(find.byType(ClassicCanvasView), findsOneWidget);

      final scene = classicSceneOf(tester);
      expect(scene.layout.cards.keys.toSet(), {'a', 'b', 'c', 'd'});

      // The failed step is clearly distinguished (cross glyph channel), and
      // the downstream consequences are dimmed skips, no motion.
      expect(scene.encodings['b']!.marker, ClassicMarker.cross);
      expect(scene.encodings['b']!.label, 'failed');
      expect(scene.encodings['c']!.marker, ClassicMarker.dim);
      expect(scene.encodings['d']!.marker, ClassicMarker.dim);
      expect(scene.encodings['c']!.label, 'skipped');
      expect(scene.encodings['c']!.animated, isFalse,
          reason: 'skipped cards are dimmed with no motion');
      expect(scene.encodings['b']!.label, isNot(scene.encodings['c']!.label),
          reason: 'failed vs skipped is distinguishable without color');

      // The failure reason is available on inspection (shared inspector).
      final node = app.storeFor('h-fail').state!.nodes['b']!;
      expect(node.error, 'b exploded');
      await tester.pumpWidget(MaterialApp(
        theme: themeDataFor(ThemeMode.dark),
        home: Scaffold(
          body: Stack(children: [Inspector(node: node, onClose: () {})]),
        ),
      ));
      expect(find.text('b exploded'), findsOneWidget,
          reason: 'the failed node is inspectable in classic mode');
    });

    testWidgets(
        '(c) a fallback step carries the fb chip and the inspector names the '
        'fallback model', (tester) async {
      final d = demoDef();
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState();
      final client = AgencyClient(
        'http://fake',
        httpClient: fakeClient(
          history: [
            RunHistoryEntry(
                runId: 'h-fb',
                workflowName: 'demo',
                startedAt: DateTime.parse(newTs).toUtc(),
                state: 'completed'),
          ],
          statusByRun: {
            'h-fb': {
              'run_id': 'h-fb',
              'workflow_name': 'demo',
              'state': 'completed',
              'started_at': newTs,
              'nodes': <String, Object>{
                'a': {
                  'status': 'completed',
                  'attempt': 1,
                  'tokens': 10,
                  'duration_seconds': 1.0,
                  'model': 'draft',
                },
                'b': {
                  'status': 'completed_fallback',
                  'attempt': 2,
                  'tokens': 20,
                  'duration_seconds': 2.0,
                  'model': 'fb-model',
                  'fallback': 'fb-model',
                },
              },
              'pending_inputs': <Object>[],
            },
          },
          resultByRun: {
            'h-fb': {
              'run_id': 'h-fb',
              'status': 'completed',
              'total_tokens': 30,
              'duration_seconds': 3.0,
              'nodes': <Object>[
                {
                  'node_id': 'a',
                  'status': 'completed',
                  'attempt': 1,
                  'tokens': 10,
                  'duration_seconds': 1.0,
                  'output': 'a-out',
                },
                {
                  'node_id': 'b',
                  'status': 'completed_fallback',
                  'attempt': 2,
                  'tokens': 20,
                  'duration_seconds': 2.0,
                  'fallback': 'fb-model',
                },
              ],
            },
          },
        ),
      );
      final entry = CatalogEntry(path: 'demo.yaml', name: 'demo', definition: d);

      await pumpHome(tester,
          app: app, client: client, entries: [entry], events: events);
      await settle(tester);
      expect(app.selectedRunId, 'h-fb');

      app.setCanvasMode(CanvasMode.classic);
      await tester.pump();
      await tester.pump();

      final st = app.storeFor('h-fb').state!;
      final scene = classicSceneOf(tester);

      // The persistent fb chip channel: check glyph + fb chip, distinct
      // from plain completed.
      expect(scene.encodings['b']!.marker, ClassicMarker.checkFb);
      expect(scene.encodings['b']!.label, 'fallback');
      expect(scene.encodings['a']!.marker, ClassicMarker.check);
      expect(st.nodes['b']!.fallbackModel, 'fb-model');
      expect(st.nodes['b']!.onFallbackPath, isTrue);

      // The shared inspector names the fallback model (FR-011).
      final node = st.nodes['b']!;
      await tester.pumpWidget(MaterialApp(
        theme: themeDataFor(ThemeMode.dark),
        home: Scaffold(
          body: Stack(children: [Inspector(node: node, onClose: () {})]),
        ),
      ));
      expect(find.text('Fallback'), findsOneWidget);
      expect(find.text('fb-model'), findsWidgets,
          reason: 'the inspector names the fallback model');
    });
  });
}
