import 'dart:async';

import 'package:agencyapp/api/agency_events.dart';
import 'package:agencyapp/api/models.dart';
import 'package:fake_async/fake_async.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:stream_channel/stream_channel.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

/// T043 — event reconnect policy (US5, R11).
///
/// Proves the reconnection behavior against a scripted fake WebSocket (an
/// injectable channel factory) under a faked clock (FakeAsync):
///   (a) after EVERY close code (1000/1001/1008) the client retries at
///       1 s → doubling → 30 s cap (attempts at t = 0,1,3,7,15,31,61,91);
///   (b) the backoff resets once a live connection delivers a data message;
///   (c) 1008 additionally reports the close to the resync hook and logs a
///       client-side slow-consumer warning;
///   (d) retarget() tears down the current connection and dials the new
///       address immediately;
///   (e) frames decoded before and after a reconnect land in the same
///       long-lived `frames` stream.
void main() {
  const ts = '2026-08-29T10:00:00+00:00';

  String frameJson(String type) =>
      '{"event_type": "$type", "seq": 1, "timestamp": "$ts", "run_id": "run-1", '
      '"payload": {"node_id": "a"}}';

  group('T043 reconnect policy (US5, R11)', () {
    test('(a) attempts double from 1 s to the 30 s cap after any close code',
        () {
      for (final code in [1000, 1001, 1008]) {
        fakeAsync((fa) {
          final channels = <FakeChannel>[];
          final attemptTimes = <int>[];
          final closeCodes = <int>[];
          late final AgencyEvents events;
          events = AgencyEvents(
            'http://127.0.0.1:8000',
            channelFactory: (uri) {
              final ch = FakeChannel();
              channels.add(ch);
              attemptTimes.add(fa.elapsed.inSeconds);
              return ch;
            },
            onClosed: closeCodes.add,
          );

          events.connect();
          expect(attemptTimes, [0], reason: 'close $code: initial attempt at t=0');

          for (final wait in [1, 2, 4, 8, 16, 30, 30]) {
            channels.last.close(code);
            fa.flushMicrotasks();
            fa.elapse(Duration(seconds: wait));
          }

          expect(
            attemptTimes,
            [0, 1, 3, 7, 15, 31, 61, 91],
            reason: 'close $code: backoff doubles 1 s → 30 s cap',
          );
          expect(
            closeCodes,
            List.filled(7, code),
            reason: 'every close with code $code is reported to the resync hook',
          );
          events.close();
        });
      }
    });

    test('(b) the backoff resets once a live connection delivers a message', () {
      fakeAsync((fa) {
        final channels = <FakeChannel>[];
        final attemptTimes = <int>[];
        final received = <EventFrame>[];
        late final AgencyEvents events;
        events = AgencyEvents(
          'http://127.0.0.1:8000',
          channelFactory: (uri) {
            final ch = FakeChannel();
            channels.add(ch);
            attemptTimes.add(fa.elapsed.inSeconds);
            return ch;
          },
        );
        events.frames.listen(received.add);

        events.connect();
        // Fail twice so the backoff would grow to 4 s.
        channels.last.close(1006);
        fa.flushMicrotasks();
        fa.elapse(const Duration(seconds: 1)); // attempt at t=1
        channels.last.close(1006);
        fa.flushMicrotasks();
        fa.elapse(const Duration(seconds: 2)); // attempt at t=3

        // This connection is live: it delivers a frame, proving liveness and
        // resetting the backoff to the initial 1 s.
        channels.last.deliver(frameJson('node_started'));
        fa.flushMicrotasks();
        expect(received.map((f) => f.eventType).toList(), ['node_started']);

        channels.last.close(1006);
        fa.flushMicrotasks();
        fa.elapse(const Duration(seconds: 1)); // attempt at t=4, not t=7
        expect(attemptTimes, [0, 1, 3, 4],
            reason: 'a live connection resets the backoff to the initial 1 s');
        events.close();
      });
    });

    test('(c) 1008 reports the close and logs the slow-consumer warning', () {
      fakeAsync((fa) {
        final channels = <FakeChannel>[];
        final closeCodes = <int>[];
        final logs = <String>[];
        late final AgencyEvents events;
        events = AgencyEvents(
          'http://127.0.0.1:8000',
          channelFactory: (uri) {
            final ch = FakeChannel();
            channels.add(ch);
            return ch;
          },
          onClosed: closeCodes.add,
          logger: logs.add,
        );

        events.connect();
        channels.last.close(1008, 'overflow');
        fa.flushMicrotasks();

        expect(closeCodes, [1008],
            reason: 'the 1008 close reaches the resync hook');
        expect(logs.any((m) => m.contains('1008')), isTrue,
            reason: 'a client-side slow-consumer warning is logged');
        events.close();
      });
    });

    test('(d) retarget tears down the current connection and dials the new address',
        () {
      fakeAsync((fa) {
        final channels = <FakeChannel>[];
        final uris = <Uri>[];
        late final AgencyEvents events;
        events = AgencyEvents(
          'http://127.0.0.1:8000',
          channelFactory: (uri) {
            uris.add(uri);
            final ch = FakeChannel();
            channels.add(ch);
            return ch;
          },
        );
        events.connect();
        expect(uris, [Uri.parse('ws://127.0.0.1:8000/events')]);

        events.retarget('http://10.0.0.5:9000');
        fa.flushMicrotasks();

        expect(events.baseUrl, 'http://10.0.0.5:9000');
        expect(channels.length, 2, reason: 'a new connection is dialed');
        expect(uris.last, Uri.parse('ws://10.0.0.5:9000/events'));
        expect(channels.first.closed, isTrue,
            reason: 'the old connection is torn down');
        expect(events.connected, isTrue, reason: 'the retargeted attempt is live');
        events.close();
      });
    });

    test('(e) frames survive a reconnect in the same long-lived stream', () {
      fakeAsync((fa) {
        final channels = <FakeChannel>[];
        final received = <EventFrame>[];
        late final AgencyEvents events;
        events = AgencyEvents(
          'http://127.0.0.1:8000',
          channelFactory: (uri) {
            final ch = FakeChannel();
            channels.add(ch);
            return ch;
          },
        );
        events.frames.listen(received.add);

        events.connect();
        channels.last.deliver(frameJson('node_started'));
        fa.flushMicrotasks();

        channels.last.close(1006);
        fa.flushMicrotasks();
        fa.elapse(const Duration(seconds: 1));
        channels.last.deliver(frameJson('node_completed'));
        fa.flushMicrotasks();

        expect(
          received.map((f) => f.eventType).toList(),
          ['node_started', 'node_completed'],
          reason: 'the frames stream is not cut by a reconnect',
        );
        events.close();
      });
    });

    test('(f) an idle channel is NOT treated as dead (002: no data heartbeat)',
        () {
      // The 002 contract: the server keeps the link alive with protocol-level
      // pings (invisible to the app layer) and sends no data heartbeat, so a
      // healthy idle connection carries no frames. The client must keep such
      // a connection open — closing it on silence would recycle a perfectly
      // healthy link (and churn the UI into reconnecting/stale).
      fakeAsync((fa) {
        final channels = <FakeChannel>[];
        final closeCodes = <int>[];
        late final AgencyEvents events;
        events = AgencyEvents(
          'http://127.0.0.1:8000',
          channelFactory: (uri) {
            final ch = FakeChannel();
            channels.add(ch);
            return ch;
          },
          onClosed: closeCodes.add,
        );

        events.connect();
        // Stay silent well past the old 20 s liveness window.
        fa.elapse(const Duration(seconds: 60));
        fa.flushMicrotasks();

        expect(channels.length, 1,
            reason: 'no redial: an idle connection is not recycled');
        expect(channels.single.closed, isFalse,
            reason: 'the silent channel stays open');
        expect(events.connected, isTrue,
            reason: 'the manager still considers the link live');
        expect(closeCodes, isEmpty,
            reason: 'no close is reported for a healthy idle link');
        events.close();
      });
    });
  });
}

/// A scripted fake WebSocket channel. The test drives it: [deliver] pushes a
/// frame to the stream side; [close] completes the stream with a close code,
/// mirroring how a real channel reports the close on `closeCode`.
class FakeChannel extends StreamChannelMixin implements WebSocketChannel {
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
    // Mirror IOWebSocket: clients may only close with 1000 or 3000–4999.
    if (closeCode != null &&
        closeCode != 1000 &&
        (closeCode < 3000 || closeCode > 4999)) {
      throw ArgumentError(
          'close code must be 1000 or in the range 3000-4999: $closeCode');
    }
    _channel.close(closeCode, closeReason);
    if (!_done.isCompleted) _done.complete();
    return Future<void>.value();
  }

  @override
  Future get done => _done.future;
}
