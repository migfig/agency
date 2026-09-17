import 'dart:async';

import 'package:agencyapp/api/agency_client.dart';
import 'package:agencyapp/api/models.dart';
import 'package:agencyapp/app/app_state.dart';
import 'package:agencyapp/app/home_page.dart';
import 'package:agencyapp/canvas/canvas_view.dart';
import 'package:agencyapp/canvas/scene_painter.dart';
import 'package:agencyapp/catalog/catalog_entry.dart';
import 'package:agencyapp/catalog/workflow_def.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

/// T048 — the optional environment strip (contracts/ui.md): run_id-null
/// vram/model lifecycle frames become faint drifting motes at the top of the
/// canvas — decorative, never state-bearing, may be absent.
void main() {
  const ts = '2026-08-29T10:00:00+00:00';
  const runId = 'run-1';

  WorkflowDefinition demoDef() => parseWorkflowDefinition('''
name: demo
nodes:
  a: {id: a, type: agent, model: draft}
  b: {id: b, type: tool_call, tool: echo}
''');

  EventFrame envFrame(String type, int seq) => decodeEventFrame({
        'event_type': type,
        'seq': seq,
        'timestamp': ts,
        'run_id': null,
        'payload': <String, Object>{},
      })!;

  EventFrame runFrame(String type, int seq, String nodeId) => decodeEventFrame({
        'event_type': type,
        'seq': seq,
        'timestamp': ts,
        'run_id': runId,
        'payload': {'node_id': nodeId},
      })!;

  MockClient fakeClient() => MockClient((request) async {
        final path = request.url.path;
        final headers = <String, String>{'content-type': 'application/json'};
        if (request.method == 'POST' && path == '/runs') {
          return http.Response(
            '{"run_id": "$runId", "state": "running", "workflow": "demo"}',
            201,
            headers: headers,
          );
        }
        if (request.method == 'GET' && path == '/runs') {
          return http.Response('[]', 200, headers: headers);
        }
        if (request.method == 'GET' && path == '/runs/$runId') {
          return http.Response(
            '{"run_id": "$runId", "workflow_name": "demo", "state": "running", "started_at": "$ts", "nodes": {}, "pending_inputs": []}',
            200,
            headers: headers,
          );
        }
        return http.Response(
          '{"error": {"code": "unknown_run", "message": "nope"}}',
          404,
          headers: headers,
        );
      });

  Future<void> settle(WidgetTester tester) async {
    for (var i = 0; i < 3; i++) {
      await tester.pump();
    }
  }

  Future<void> pumpHome(
    WidgetTester tester,
    StreamController<EventFrame> events,
  ) async {
    final app = AppState();
    final client = AgencyClient('http://fake', httpClient: fakeClient());
    final d = demoDef();
    final entry = CatalogEntry(path: 'demo.yaml', name: 'demo', definition: d);
    await tester.pumpWidget(MaterialApp(
      home: HomePage(
        app: app,
        client: client,
        entries: [entry],
        eventStream: events.stream,
      ),
    ));
    await tester.tap(find.text('Start').first);
    await settle(tester);
  }

  CanvasViewState canvas(WidgetTester tester) =>
      tester.state<CanvasViewState>(find.byType(CanvasView));

  group('T048 environment strip', () {
    testWidgets('run_id-null vram/model frames become motes (in order)',
        (tester) async {
      final events = StreamController<EventFrame>.broadcast();
      await pumpHome(tester, events);

      events.add(envFrame('vram_threshold_exceeded', 100));
      events.add(envFrame('model_reloaded', 101));
      await settle(tester);

      final motes = canvas(tester).motes;
      expect(motes, hasLength(2));
      expect(motes[0].seed, 100);
      expect(motes[0].kind, 'vram_threshold_exceeded');
      expect(motes[1].seed, 101);
      expect(motes[1].kind, 'model_reloaded');
    });

    testWidgets('run-scoped frames are never collected', (tester) async {
      final events = StreamController<EventFrame>.broadcast();
      await pumpHome(tester, events);

      events.add(runFrame('node_started', 1, 'a'));
      events.add(runFrame('node_completed', 2, 'a'));
      await settle(tester);

      expect(canvas(tester).motes, isEmpty,
          reason: 'only run_id-null env frames feed the strip');
    });

    testWidgets('motes never touch the scene (state-free)', (tester) async {
      final events = StreamController<EventFrame>.broadcast();
      await pumpHome(tester, events);

      final before = canvas(tester).scene!;
      events.add(envFrame('vram_freed', 200));
      events.add(envFrame('model_offloaded', 201));
      await settle(tester);

      final after = canvas(tester).scene!;
      expect(after.encodingsByNode.keys, before.encodingsByNode.keys);
      for (final id in before.encodingsByNode.keys) {
        expect(after.encodingsByNode[id], before.encodingsByNode[id],
            reason: 'motes must not disturb node encodings ($id)');
      }
      expect(canvas(tester).motes, hasLength(2));
    });

    testWidgets('the mote list is bounded (oldest dropped)', (tester) async {
      final events = StreamController<EventFrame>.broadcast();
      await pumpHome(tester, events);

      for (var i = 0; i < 40; i++) {
        events.add(envFrame(i.isEven ? 'vram_freed' : 'model_offloaded', 300 + i));
      }
      await settle(tester);

      final motes = canvas(tester).motes;
      expect(motes, hasLength(32));
      expect(motes.first.seed, 308);
      expect(motes.last.seed, 339);
    });
  });

  group('T048 mote geometry (pure, deterministic)', () {
    test('geometry is a pure function of (mote, clock, size)', () {
      const mote = EnvMote(seed: 42, bornAt: 3.0, kind: 'model_reloaded');
      const size = Size(1000, 800);
      for (final clock in [3.5, 5.0, 9.9]) {
        final a = envMoteGeometry(mote, clock: clock, size: size)!;
        final b = envMoteGeometry(mote, clock: clock, size: size)!;
        expect(a.offset, b.offset, reason: 'same inputs ⇒ same position');
        expect(a.radius, b.radius);
        expect(a.fade, b.fade);
        expect(a.fade, greaterThan(0.0));
        expect(a.fade, lessThan(1.0));
        expect(a.offset.dx, inInclusiveRange(-20.0, 1020.0));
        expect(a.offset.dy, inInclusiveRange(9.0, 41.0));
        expect(a.radius, inInclusiveRange(kMoteMinRadius, 2.6));
      }
    });

    test('unborn and expired motes yield no geometry', () {
      const mote = EnvMote(seed: 7, bornAt: 10.0, kind: 'vram_freed');
      const size = Size(800, 600);
      expect(envMoteGeometry(mote, clock: 9.9, size: size), isNull,
          reason: 'unborn (clock < bornAt)');
      expect(envMoteGeometry(mote, clock: 10.0 + kMoteLife + 1, size: size),
          isNull,
          reason: 'expired');
    });

    test('distinct seeds yield distinct lanes (no per-frame randomness)', () {
      const size = Size(1200, 700);
      final a = envMoteGeometry(
        const EnvMote(seed: 1, bornAt: 0, kind: 'vram_freed'),
        clock: 2.0,
        size: size,
      )!;
      final b = envMoteGeometry(
        const EnvMote(seed: 2, bornAt: 0, kind: 'vram_freed'),
        clock: 2.0,
        size: size,
      )!;
      expect(a.offset.dy, isNot(b.offset.dy),
          reason: 'seeds separate the motes deterministically');
    });
  });
}
