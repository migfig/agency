import 'dart:async';
import 'dart:convert';

import 'package:agencyapp/api/agency_client.dart';
import 'package:agencyapp/api/agency_events.dart';
import 'package:agencyapp/api/models.dart';
import 'package:agencyapp/app/app_state.dart';
import 'package:agencyapp/app/home_page.dart';
import 'package:agencyapp/catalog/workflow_def.dart';
import 'package:agencyapp/run/run_state.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:stream_channel/stream_channel.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

/// T042 — connection lifecycle (US5, R2/R11, T3).
///
/// Drives the real [AppState] + [AgencyEvents] against a scripted fake
/// server (in-process HTTP) and fake WebSocket channels. The reachability
/// oracle is debounced: a single failed poll stays hidden (grace window);
/// two consecutive failures declare `disconnected` + stale.
///   (a) a transient blip (one failed poll, recovered) never disconnects,
///       while two consecutive failures ⇒ `disconnected` + rendered run
///       marked stale with the last view kept;
///   (b) WS close while the server is reachable ⇒ `reconnecting`;
///   (c) health recovery ⇒ history refreshed AND the rendered run resynced
///       node-for-node to the server snapshot (SC-002/SC-006);
///   (d) changing the configured address retargets client + events and
///       persists across an app restart (FR-001/FR-002/FR-003);
///   (e) the banner renders the three states (contracts/ui.md).
///
/// No real network, no wall-clock assertions: reconnects run on the real
/// event loop with a 5 s initial backoff (so no reconnect fires during a
/// test) and bounded waits.
void main() {
  const ts = '2026-08-29T10:00:00+00:00';
  const runId = 'run-1';

  WorkflowDefinition demoDef() => parseWorkflowDefinition('''
name: demo
nodes:
  a: {id: a, type: agent, model: draft}
  b: {id: b, type: agent, model: draft}
''');

  RunHistoryEntry entry(String id, String state) => RunHistoryEntry(
        runId: id,
        workflowName: 'demo',
        startedAt: DateTime.parse(ts),
        state: state,
      );

  /// v1: a completed, b running, run still running.
  Map<String, Object> snapshotV1() => {
        'run_id': runId,
        'workflow_name': 'demo',
        'state': 'running',
        'started_at': ts,
        'nodes': <String, Object>{
          'a': {'status': 'completed', 'attempt': 1, 'tokens': 100, 'duration_seconds': 1.0},
          'b': {'status': 'running', 'attempt': 1, 'model': 'draft'},
        },
        'pending_inputs': <Object>[],
      };

  /// v2 (the server's truth while the client was offline): both done.
  Map<String, Object> snapshotV2() => {
        'run_id': runId,
        'workflow_name': 'demo',
        'state': 'completed',
        'started_at': ts,
        'nodes': <String, Object>{
          'a': {'status': 'completed', 'attempt': 1, 'tokens': 100, 'duration_seconds': 1.0},
          'b': {'status': 'completed', 'attempt': 1, 'tokens': 50, 'duration_seconds': 0.5},
        },
        'pending_inputs': <Object>[],
      };

  Future<_Harness> make({
    required List<RunHistoryEntry> history,
    required Map<String, Map<String, Object>> statusByRun,
  }) async {
    SharedPreferences.setMockInitialValues(const {});
    final server = MockServer(history: history, statusByRun: statusByRun);
    final client = AgencyClient(
      'http://127.0.0.1:8000',
      httpClient: MockClient(server.handle),
    );
    final app = AppState();
    await app.init();
    final channels = <FakeChannel>[];
    final events = AgencyEvents(
      'http://127.0.0.1:8000',
      channelFactory: (uri) {
        final c = FakeChannel(uri);
        channels.add(c);
        return c;
      },
      initialBackoff: const Duration(seconds: 5),
    );
    app.attachTransport(client, events);
    events.connect();
    return _Harness(
      app: app,
      client: client,
      events: events,
      server: server,
      channels: channels,
    );
  }

  Future<void> pump({int times = 4}) async {
    for (var i = 0; i < times; i++) {
      await Future<void>.delayed(Duration.zero);
    }
  }

  Future<void> waitFor(bool Function() condition, String what) async {
    final deadline = DateTime.now().add(const Duration(seconds: 5));
    while (!condition()) {
      if (DateTime.now().isAfter(deadline)) fail('timed out waiting for $what');
      await Future<void>.delayed(const Duration(milliseconds: 2));
    }
  }

  /// Node-for-node equality against the authoritative `reduce(snapshot)`
  /// (SC-002: the canvas state must equal the server truth).
  void expectNodeForNode(RunState state, RunStatusView snapshot, WorkflowDefinition def) {
    final expected = reduce(snapshot, definition: def);
    expect(state.runId, expected.runId);
    expect(state.workflow, expected.workflow);
    expect(state.runStatus, expected.runStatus);
    expect(state.isStale, isFalse);
    expect(state.nodes.keys.toSet(), expected.nodes.keys.toSet());
    for (final e in expected.nodes.values) {
      final actual = state.nodes[e.nodeId]!;
      expect(actual.status, e.status, reason: '${e.nodeId} status');
      expect(actual.attempt, e.attempt, reason: '${e.nodeId} attempt');
      expect(actual.model, e.model, reason: '${e.nodeId} model');
      expect(actual.tokens, e.tokens, reason: '${e.nodeId} tokens');
      expect(actual.error, e.error, reason: '${e.nodeId} error');
      expect(actual.skipReason, e.skipReason, reason: '${e.nodeId} skipReason');
    }
  }

  group('T042 connection lifecycle (US5)', () {
    test('(a) one blip stays hidden; two failures ⇒ disconnected + stale, last view kept',
        () async {
      final h = await make(
        history: [entry(runId, 'running')],
        statusByRun: {runId: snapshotV1()},
      );
      final d = demoDef();
      try {
        await h.app.tickHealth();
        expect(h.app.link, ServerLink.connected);

        h.app.selectRun(runId, workflowDefinition: d);
        final store = h.app.selectedRunStore!;
        store.seed(RunStatusView.fromJson(snapshotV1()));

        // A single failed poll is a transient blip: still connected, not stale.
        h.server.healthOk = false;
        await h.app.tickHealth();
        expect(h.app.link, ServerLink.connected,
            reason: 'one failed poll is within the grace window');
        expect(store.state!.isStale, isFalse,
            reason: 'a single blip does not mark the run stale');

        // The second consecutive failure crosses the grace window.
        await h.app.tickHealth();
        expect(h.app.link, ServerLink.disconnected);
        final st = store.state!;
        expect(st.isStale, isTrue, reason: 'the rendered run is marked stale');
        // The last view is kept, not cleared.
        expect(st.runId, runId);
        expect(st.nodes['a']!.status, NodeStatus.completed,
            reason: 'a keeps its last view');
        expect(st.nodes['b']!.status, NodeStatus.running,
            reason: 'b keeps its last view');
      } finally {
        h.app.dispose();
        h.events.close();
      }
    });

    test('(b) a WS close while the server is reachable ⇒ reconnecting', () async {
      final h = await make(
        history: [entry(runId, 'running')],
        statusByRun: {runId: snapshotV1()},
      );
      try {
        await h.app.tickHealth();
        expect(h.app.link, ServerLink.connected);

        h.channels.last.close(1006);
        await pump();

        expect(h.app.link, ServerLink.reconnecting,
            reason: 'the server is reachable but the WS retry is in flight');
      } finally {
        h.app.dispose();
        h.events.close();
      }
    });

    test('(c) recovery ⇒ history refreshed + rendered run resynced node-for-node',
        () async {
      final h = await make(
        history: [entry(runId, 'running'), entry('run-2', 'completed')],
        statusByRun: {runId: snapshotV1(), 'run-2': snapshotV2()},
      );
      final d = demoDef();
      try {
        await h.app.tickHealth();
        h.app.selectRun(runId, workflowDefinition: d);
        final store = h.app.selectedRunStore!;
        store.seed(RunStatusView.fromJson(snapshotV1()));

        // While the client is down the server finishes the run and starts a
        // new one.
        h.server.statusByRun[runId] = snapshotV2();
        h.server.history.add(entry('run-3', 'running'));
        h.server.healthOk = false;
        await h.app.tickHealth();
        expect(h.app.link, isNot(ServerLink.disconnected),
            reason: 'first failure is within the grace window');
        await h.app.tickHealth();
        expect(h.app.link, ServerLink.disconnected);
        expect(store.state!.isStale, isTrue);

        // The server comes back.
        h.server.healthOk = true;
        final listRunsBefore = h.server.listRunsCalls;
        await h.app.tickHealth();
        await waitFor(() => h.app.link == ServerLink.connected, 'the WS to reconnect');

        expect(h.server.listRunsCalls, listRunsBefore + 1,
            reason: 'history is refreshed on reconnect');
        expect(
          h.app.history.map((e) => e.runId).toList(),
          [runId, 'run-2', 'run-3'],
          reason: 'the refreshed history includes the offline run-3',
        );
        expectNodeForNode(store.state!, RunStatusView.fromJson(snapshotV2()), d);
      } finally {
        h.app.dispose();
        h.events.close();
      }
    });

    test('(d) changing the address retargets and persists across a restart',
        () async {
      final h = await make(history: const [], statusByRun: const {});
      try {
        await h.app.tickHealth();
        expect(h.app.serverAddress, 'http://127.0.0.1:8000');
        expect(h.channels.single.uri, Uri.parse('ws://127.0.0.1:8000/events'));

        h.app.setServerAddress('http://10.0.0.5:9000');
        await pump();

        expect(h.client.baseUrl, 'http://10.0.0.5:9000',
            reason: 'the client uses the new address');
        expect(h.events.baseUrl, 'http://10.0.0.5:9000');
        expect(h.channels.length, 2, reason: 'a new connection is dialed');
        expect(h.channels.last.uri, Uri.parse('ws://10.0.0.5:9000/events'));
        expect(h.channels.first.closed, isTrue,
            reason: 'the old connection is torn down');

        final prefs = await SharedPreferences.getInstance();
        expect(prefs.getString('server_address'), 'http://10.0.0.5:9000',
            reason: 'the address is persisted');

        final app2 = AppState();
        try {
          await app2.init();
          expect(app2.serverAddress, 'http://10.0.0.5:9000',
              reason: 'a restarted app reads the persisted address');
        } finally {
          app2.dispose();
        }
      } finally {
        h.app.dispose();
        h.events.close();
      }
    });

    testWidgets('(e) the banner renders the three states', (tester) async {
      SharedPreferences.setMockInitialValues(const {});
      final server = MockServer(history: const [], statusByRun: const {});
      final client = AgencyClient(
        'http://127.0.0.1:8000',
        httpClient: MockClient(server.handle),
      );
      final app = AppState();
      await app.init();
      final channels = <FakeChannel>[];
      final events = AgencyEvents(
        'http://127.0.0.1:8000',
        channelFactory: (uri) {
          final c = FakeChannel(uri);
          channels.add(c);
          return c;
        },
        initialBackoff: const Duration(seconds: 5),
      );
      app.attachTransport(client, events);
      events.connect();
      await app.tickHealth();

      await tester.pumpWidget(
        MaterialApp(home: Scaffold(body: Center(child: ConnectionBanner(app: app)))),
      );

      expect(find.text('reconnecting…'), findsNothing);
      expect(find.text('disconnected — retrying'), findsNothing,
          reason: 'the connected state shows only the quiet dot');

      channels.last.close(1006);
      await tester.pump();
      expect(find.text('reconnecting…'), findsOneWidget,
          reason: 'WS down + server reachable shows the amber banner');

      // One failed poll is a blip: still the amber reconnecting banner.
      server.healthOk = false;
      await app.tickHealth();
      await tester.pump();
      expect(find.text('disconnected — retrying'), findsNothing,
          reason: 'a single blip does not show the red banner');

      // Two consecutive failures cross the grace window: red banner.
      await app.tickHealth();
      await tester.pump();
      expect(find.text('disconnected — retrying'), findsOneWidget,
          reason: 'health down shows the red banner');

      events.close();
      app.dispose();
    });
  });
}

/// Scripted in-process HTTP fake for the Agency API.
class MockServer {
  MockServer({
    required this.history,
    required this.statusByRun,
  });

  bool healthOk = true;
  final List<RunHistoryEntry> history;
  final Map<String, Map<String, Object>> statusByRun;
  int listRunsCalls = 0;

  Future<http.Response> handle(http.Request request) async {
    const headers = <String, String>{'content-type': 'application/json'};
    final path = request.url.path;
    if (request.method == 'GET' && path == '/health') {
      if (!healthOk) {
        return http.Response(
          jsonEncode({'error': {'code': 'server_unavailable', 'message': 'down'}}),
          503,
          headers: headers,
        );
      }
      return http.Response(
        jsonEncode({'status': 'ok', 'log_dir': 'runs'}),
        200,
        headers: headers,
      );
    }
    if (request.method == 'GET' && path == '/runs') {
      listRunsCalls++;
      return http.Response(
        jsonEncode([
          for (final e in history)
            {
              'run_id': e.runId,
              'workflow_name': e.workflowName,
              'started_at': e.startedAt.toIso8601String(),
              'state': e.state,
            },
        ]),
        200,
        headers: headers,
      );
    }
    final m = RegExp(r'^/runs/([^/]+)$').firstMatch(path);
    if (request.method == 'GET' && m != null) {
      final body = statusByRun[m.group(1)!];
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
  }
}

class _Harness {
  _Harness({
    required this.app,
    required this.client,
    required this.events,
    required this.server,
    required this.channels,
  });

  final AppState app;
  final AgencyClient client;
  final AgencyEvents events;
  final MockServer server;
  final List<FakeChannel> channels;
}

/// A scripted fake WebSocket channel. The test drives it: [close] completes
/// the stream with a close code, mirroring how a real channel reports the
/// close on `closeCode`.
class FakeChannel extends StreamChannelMixin implements WebSocketChannel {
  FakeChannel(this.uri);

  final Uri uri;
  final StreamController<String> _controller = StreamController<String>();
  late final _FakeSink _sink = _FakeSink(this);
  int? _closeCode;
  String? _closeReason;

  @override
  Stream<String> get stream => _controller.stream;

  @override
  WebSocketSink get sink => _sink;

  @override
  String? get protocol => null;

  @override
  int? get closeCode => _closeCode;

  @override
  String? get closeReason => _closeReason;

  @override
  Future<void> get ready => Future<void>.value();

  bool get closed => _controller.isClosed;

  void deliver(String data) => _controller.add(data);

  void close([int? code, String? reason]) {
    if (_controller.isClosed) return;
    _closeCode = code;
    _closeReason = reason;
    _controller.close();
  }
}

class _FakeSink implements WebSocketSink {
  _FakeSink(this._channel);

  final FakeChannel _channel;
  final Completer<void> _done = Completer<void>();

  @override
  void add(dynamic data) {}

  @override
  void addError(Object error, [StackTrace? stackTrace]) {}

  @override
  Future<void> addStream(Stream stream) => Future<void>.value();

  @override
  Future<void> close([int? closeCode, String? closeReason]) {
    _channel.close(closeCode, closeReason);
    if (!_done.isCompleted) _done.complete();
    return Future<void>.value();
  }

  @override
  Future get done => _done.future;
}
