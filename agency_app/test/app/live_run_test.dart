import 'dart:async';
import 'dart:convert';

import 'package:agencyapp/api/agency_client.dart';
import 'package:agencyapp/api/models.dart';
import 'package:agencyapp/app/app_state.dart';
import 'package:agencyapp/app/home_page.dart';
import 'package:agencyapp/canvas/canvas_scene.dart';
import 'package:agencyapp/canvas/canvas_view.dart';
import 'package:agencyapp/canvas/state_encodings.dart';
import 'package:agencyapp/catalog/catalog_entry.dart';
import 'package:agencyapp/catalog/workflow_def.dart';
import 'package:agencyapp/run/run_state.dart';
import 'package:agencyapp/theme/theme_data.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

/// T021 — the live-loop widget test (SC-001, SC-002, SC-003, SC-007).
///
/// Pumps the real [HomePage] against a scripted fake [AgencyClient] and a fake
/// `StreamController<EventFrame>`, then proves:
///   (a) start selects the returned run and the canvas renders it;
///   (b) a deterministic frame sequence moves each node's encoding exactly per
///       the 7-state vocabulary (and never disturbs the other nodes);
///   (c) the terminal scene equals `reduce(snapshot)` for the same run —
///       SC-002's "the canvas always reflects the true node state" guarantee.
///
/// No filesystem, no real network, no wall-clock dependence.
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

  Map<String, Object> initialSnapshot() => {
        'run_id': runId,
        'workflow_name': 'demo',
        'state': 'running',
        'started_at': ts,
        'nodes': <String, Object>{},
        'pending_inputs': <Object>[],
      };

  Map<String, Object> terminalSnapshot() => {
        'run_id': runId,
        'workflow_name': 'demo',
        'state': 'completed',
        'started_at': ts,
        'nodes': <String, Object>{
          'a': {
            'status': 'completed',
            'attempt': 2,
            'tokens': 100,
            'duration_seconds': 1.0,
          },
          'b': {
            'status': 'completed_fallback',
            'attempt': 1,
            'tokens': 20,
            'duration_seconds': 0.5,
            'fallback': 'fb',
          },
          'c': {
            'status': 'failed',
            'attempt': 1,
            'reason': 'boom',
          },
          'd': {
            'status': 'skipped',
            'reason': 'unresolved_binding',
          },
        },
        'pending_inputs': <Object>[],
      };

  /// The server-side status snapshot; mutable so a test can settle the run
  /// the way the server does (the store's finalize pass polls this).
  Map<String, Object> serverStatus = initialSnapshot();

  MockClient fakeClient() => MockClient((request) async {
        final path = request.url.path;
        final headers = <String, String>{'content-type': 'application/json'};
        if (request.method == 'POST' && path == '/runs') {
          return http.Response(
            jsonEncode({'run_id': runId, 'state': 'running', 'workflow': 'demo'}),
            201,
            headers: headers,
          );
        }
        if (request.method == 'GET' && path == '/runs') {
          return http.Response('[]', 200, headers: headers);
        }
        if (request.method == 'GET' && path == '/runs/$runId') {
          return http.Response(jsonEncode(serverStatus), 200, headers: headers);
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

  Future<void> start(WidgetTester tester) async {
    await tester.tap(find.text('Start').first);
    await settle(tester);
  }

  CanvasScene sceneOf(WidgetTester tester) =>
      tester.state<CanvasViewState>(find.byType(CanvasView)).scene!;

  group('T021 live loop', () {
    testWidgets('(a) start selects the run and the canvas renders it',
        (tester) async {
      final d = demoDef();
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState();
      final client = AgencyClient('http://fake', httpClient: fakeClient());
      final entry = CatalogEntry(path: 'demo.yaml', name: 'demo', definition: d);

      await pumpHome(tester, app: app, client: client, entry: entry, events: events);
      await start(tester);

      expect(app.selectedRunId, runId, reason: 'start selects the returned run');
      expect(find.byType(CanvasView), findsOneWidget,
          reason: 'the canvas renders the selected run');
      final scene = sceneOf(tester);
      expect(scene.encodingsByNode.keys.toSet(), {'a', 'b', 'c', 'd'},
          reason: 'every workflow node is present in the scene');
      for (final id in ['a', 'b', 'c', 'd']) {
        expect(scene.encodingsByNode[id], kStateEncodings[NodeStatus.pending],
            reason: '$id renders pending before any node event');
      }
    });

    testWidgets('(a2) start posts the workflow file path, not the name',
        (tester) async {
      String? postedWorkflow;
      final headers = <String, String>{'content-type': 'application/json'};
      final client = AgencyClient('http://fake', httpClient: MockClient((request) async {
            if (request.method == 'POST' && request.url.path == '/runs') {
              postedWorkflow =
                  (jsonDecode(request.body) as Map<String, dynamic>)['workflow']
                      as String?;
              return http.Response(
                jsonEncode({'run_id': runId, 'state': 'running', 'workflow': 'demo'}),
                201,
                headers: headers,
              );
            }
            if (request.method == 'GET' && request.url.path == '/runs') {
              return http.Response('[]', 200, headers: headers);
            }
            return http.Response(
              jsonEncode({'error': {'code': 'unknown_run', 'message': 'nope'}}),
              404,
              headers: headers,
            );
          }));
      final d = demoDef();
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState();
      final entry = CatalogEntry(path: 'demo.yaml', name: 'demo', definition: d);

      await pumpHome(tester, app: app, client: client, entry: entry, events: events);
      await start(tester);

      expect(postedWorkflow, isNotNull, reason: 'start posted a workflow field');
      expect(postedWorkflow, endsWith('demo.yaml'),
          reason: 'the server resolves the field as a file path on its host');
      expect(postedWorkflow, isNot(equals('demo')),
          reason: 'a bare name is not a resolvable path');
    });

    testWidgets('(b) a deterministic frame sequence moves each node per T1',
        (tester) async {
      final d = demoDef();
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState();
      final client = AgencyClient('http://fake', httpClient: fakeClient());
      final entry = CatalogEntry(path: 'demo.yaml', name: 'demo', definition: d);

      await pumpHome(tester, app: app, client: client, entry: entry, events: events);
      await start(tester);

      Future<void> feed(EventFrame f) async {
        events.add(f);
        await tester.pump();
      }

      CanvasScene s() => sceneOf(tester);

      // All four nodes become known + pending.
      await feed(frame('node_queued', 1, {'node_id': 'a'}));
      await feed(frame('node_queued', 2, {'node_id': 'b'}));
      await feed(frame('node_queued', 3, {'node_id': 'c'}));
      await feed(frame('node_queued', 4, {'node_id': 'd'}));
      for (final id in ['a', 'b', 'c', 'd']) {
        expect(s().encodingsByNode[id], kStateEncodings[NodeStatus.pending],
            reason: '$id pending after node_queued');
      }

      // a: running.
      await feed(frame('node_started', 5, {'node_id': 'a', 'attempt': 1, 'model': 'draft'}));
      expect(s().encodingsByNode['a'], kStateEncodings[NodeStatus.running]);
      expect(s().encodingsByNode['b'], kStateEncodings[NodeStatus.pending],
          reason: 'b untouched while a runs');

      // a: failed with retry remaining -> awaiting_retry; node_retrying keeps it there.
      await feed(frame('node_failed', 6, {'node_id': 'a', 'attempt': 1, 'error': 'transient'}));
      expect(s().encodingsByNode['a'], kStateEncodings[NodeStatus.awaitingRetry]);
      await feed(frame('node_retrying', 7, {'node_id': 'a', 'attempt': 1, 'delay_seconds': 1.5}));
      expect(s().encodingsByNode['a'], kStateEncodings[NodeStatus.awaitingRetry],
          reason: 'node_retrying holds awaiting_retry');

      // a: started on attempt 2 -> running again.
      await feed(frame('node_started', 8, {'node_id': 'a', 'attempt': 2, 'model': 'draft'}));
      expect(s().encodingsByNode['a'], kStateEncodings[NodeStatus.running]);

      // b: failed (single attempt, has fallback) -> awaiting_retry on fallback path.
      await feed(frame('node_failed', 9, {'node_id': 'b', 'attempt': 1, 'error': 'boom'}));
      expect(s().encodingsByNode['b'], kStateEncodings[NodeStatus.awaitingRetry]);

      // c: failed (single attempt, no fallback) -> failed (final).
      await feed(frame('node_failed', 10, {'node_id': 'c', 'attempt': 1, 'error': 'boom'}));
      expect(s().encodingsByNode['c'], kStateEncodings[NodeStatus.failed]);

      // d: skipped.
      await feed(frame('node_skipped', 11, {'node_id': 'd', 'reason': 'unresolved_binding'}));
      expect(s().encodingsByNode['d'], kStateEncodings[NodeStatus.skipped]);

      // a: completed.
      await feed(frame('node_completed', 12,
          {'node_id': 'a', 'attempt': 2, 'tokens_used': 100, 'duration_seconds': 1.0}));
      expect(s().encodingsByNode['a'], kStateEncodings[NodeStatus.completed]);

      // b: completed on fallback -> completed_fallback.
      await feed(frame('node_completed', 13,
          {'node_id': 'b', 'attempt': 1, 'fallback': 'fb', 'tokens_used': 20, 'duration_seconds': 0.5}));
      expect(s().encodingsByNode['b'], kStateEncodings[NodeStatus.completedFallback]);

      // Every node is now terminal: settle the server snapshot and let the
      // store's finalize pass (status poll -> result fold) finish.
      serverStatus = terminalSnapshot();
      for (var i = 0; i < 3; i++) {
        await tester.pump(const Duration(milliseconds: 500));
      }
    });

    testWidgets('(c) the terminal scene equals reduce(snapshot) for the same run',
        (tester) async {
      final d = demoDef();
      final events = StreamController<EventFrame>.broadcast();
      final app = AppState();
      final client = AgencyClient('http://fake', httpClient: fakeClient());
      final entry = CatalogEntry(path: 'demo.yaml', name: 'demo', definition: d);

      await pumpHome(tester, app: app, client: client, entry: entry, events: events);
      await start(tester);

      final sequence = <EventFrame>[
        frame('node_queued', 1, {'node_id': 'a'}),
        frame('node_queued', 2, {'node_id': 'b'}),
        frame('node_queued', 3, {'node_id': 'c'}),
        frame('node_queued', 4, {'node_id': 'd'}),
        frame('node_started', 5, {'node_id': 'a', 'attempt': 1, 'model': 'draft'}),
        frame('node_failed', 6, {'node_id': 'a', 'attempt': 1, 'error': 'transient'}),
        frame('node_retrying', 7, {'node_id': 'a', 'attempt': 1, 'delay_seconds': 1.5}),
        frame('node_started', 8, {'node_id': 'a', 'attempt': 2, 'model': 'draft'}),
        frame('node_failed', 9, {'node_id': 'b', 'attempt': 1, 'error': 'boom'}),
        frame('node_failed', 10, {'node_id': 'c', 'attempt': 1, 'error': 'boom'}),
        frame('node_skipped', 11, {'node_id': 'd', 'reason': 'unresolved_binding'}),
        frame('node_completed', 12,
            {'node_id': 'a', 'attempt': 2, 'tokens_used': 100, 'duration_seconds': 1.0}),
        frame('node_completed', 13,
            {'node_id': 'b', 'attempt': 1, 'fallback': 'fb', 'tokens_used': 20, 'duration_seconds': 0.5}),
      ];
      for (final f in sequence) {
        events.add(f);
        await tester.pump();
      }

      // The run is over: settle the server snapshot and let the store's
      // finalize pass re-seed from the terminal snapshot and fold in the
      // result view (the frames carried no per-node output).
      serverStatus = terminalSnapshot();
      for (var i = 0; i < 3; i++) {
        await tester.pump(const Duration(milliseconds: 500));
      }

      final actual = sceneOf(tester);
      final expected = buildScene(
        reduce(RunStatusView.fromJson(terminalSnapshot()), definition: d),
        definition: d,
      );

      expect(actual.encodingsByNode.keys.toSet(), expected.encodingsByNode.keys.toSet(),
          reason: 'same node set as the authoritative snapshot');
      for (final id in expected.encodingsByNode.keys) {
        expect(actual.encodingsByNode[id], expected.encodingsByNode[id],
            reason: 'encoding for $id matches reduce(snapshot)');
      }
      // Layout is pure over (definition, node set) and must agree too.
      final ap = actual.layout.positions;
      final ep = expected.layout.positions;
      expect(ap.keys.toSet(), ep.keys.toSet(), reason: 'same laid-out node set');
      for (final id in ep.keys) {
        final (ca, ra) = (ap[id]!.col, ap[id]!.row);
        final (ce, re) = (ep[id]!.col, ep[id]!.row);
        expect((ca, ra), (ce, re), reason: 'layout position for $id matches');
      }

      // Finalize enriched the state beyond the frames: the authoritative run
      // status and API-level total tokens come from the status + result
      // views, and the summary bar reflects them.
      final store = app.selectedRunStore!;
      expect(store.state!.runStatus, 'completed',
          reason: 'the run status settled from the server snapshot');
      expect(store.state!.totalTokens, 120,
          reason: 'total tokens from the result view');
      expect(find.text('completed'), findsOneWidget);
      expect(find.text('120 tokens'), findsOneWidget);
    });
  });
}
