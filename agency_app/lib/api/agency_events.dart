import 'dart:async';
import 'dart:convert';
import 'package:web_socket_channel/web_socket_channel.dart';
import 'models.dart';

/// Derive the ws(s) URL from a configured http(s) base address by scheme swap.
String eventsUrl(String httpAddress) {
  final u = Uri.parse(httpAddress);
  final scheme = u.scheme == 'https' ? 'wss' : 'ws';
  return Uri(scheme: scheme, host: u.host, port: u.port, path: '/events').toString();
}

/// Creates the [WebSocketChannel] for [uri]. Injectable for tests.
typedef ChannelFactory = WebSocketChannel Function(Uri uri);

/// Manages the `/events` WebSocket connection with automatic reconnection.
///
/// The client never sends data (contract rule). [frames] is a long-lived
/// broadcast stream that survives reconnects; [lifecycle] emits `true` when a
/// dial is made and `false` when the connection drops. Reconnect backoff
/// starts at [initialBackoff], doubles per attempt, caps at 30 s, and resets
/// when a live connection delivers a data message. A 1008 close (server-side
/// slow consumer) is additionally reported to [logger] (R11).
///
/// **Liveness** (002 contract): the server keeps the connection alive with
/// protocol-level WebSocket pings (answered transparently by the client stack)
/// and sends no data-level heartbeat — a healthy idle connection carries no
/// frames at all. The client therefore must never treat "no frames" as death;
/// dead connections surface as closes, which drive [\_scheduleReconnect].
/// UI-level connectivity is driven by the app's periodic HTTP health poll.
class AgencyEvents {
  AgencyEvents(
    String baseUrl, {
    ChannelFactory? channelFactory,
    Duration initialBackoff = const Duration(seconds: 1),
    void Function(int closeCode)? onClosed,
    void Function(String message)? logger,
  })  : _baseUrl = baseUrl,
        _channelFactory = channelFactory ?? WebSocketChannel.connect,
        _initialBackoff = initialBackoff,
        _backoff = initialBackoff,
        _controller = StreamController<EventFrame>.broadcast(),
        _lifecycleController = StreamController<bool>.broadcast() {
    final closed = onClosed;
    final log = logger;
    this.onClosed = closed;
    this.logger = log;
  }

  static const Duration _maxBackoff = Duration(seconds: 30);

  final ChannelFactory _channelFactory;
  final Duration _initialBackoff;
  final StreamController<EventFrame> _controller;
  final StreamController<bool> _lifecycleController;

  String _baseUrl;
  Duration _backoff;

  /// Called with the effective close code on every close (resync hook).
  void Function(int closeCode)? onClosed;

  /// Diagnostic sink; receives the 1008 slow-consumer warning.
  void Function(String message)? logger;

  WebSocketChannel? _channel;
  StreamSubscription? _sub;
  Timer? _reconnectTimer;
  bool _closed = false;

  /// The configured http(s) base address (updated by [retarget]).
  String get baseUrl => _baseUrl;

  /// Decoded event frames; the stream survives reconnects.
  Stream<EventFrame> get frames => _controller.stream;

  /// `true` after a dial is made, `false` when the connection drops.
  Stream<bool> get lifecycle => _lifecycleController.stream;

  /// Optimistic: a dial has been made and the channel is not known closed.
  bool get connected => _channel != null;

  /// Open the WebSocket (idempotent); reconnects are scheduled automatically
  /// after any close.
  void connect() {
    if (_closed || _channel != null) return;
    _dial();
  }

  /// Point the client at [address] and dial immediately, tearing down the
  /// current connection first (T047).
  void retarget(String address) {
    if (_closed) return;
    _baseUrl = address;
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _backoff = _initialBackoff;
    _teardown();
    _dial();
  }

  /// Permanently close the connection manager.
  void close() {
    if (_closed) return;
    _closed = true;
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _teardown();
    _controller.close();
    _lifecycleController.close();
  }

  void _dial() {
    final channel = _channelFactory(Uri.parse(eventsUrl(_baseUrl)));
    _channel = channel;
    _sub = channel.stream.listen(
      (raw) {
        _backoff = _initialBackoff;
        final frame = decodeEventFrameString(
          raw is String ? raw : utf8.decode(raw as List<int>),
        );
        if (frame == null) return;
        // Unknown event types are dropped (forward compatibility).
        if (EventFrame.knownEventTypes.contains(frame.eventType)) {
          _controller.add(frame);
        }
      },
      onError: (Object e, StackTrace st) {
        if (!_controller.isClosed) _controller.addError(e, st);
      },
      onDone: () => _onChannelClosed(channel),
    );
    if (!_lifecycleController.isClosed) _lifecycleController.add(true);
  }

  void _onChannelClosed(WebSocketChannel channel) {
    if (_closed || _channel != channel) return;
    final code = channel.closeCode ?? 0;
    if (code == 1008) {
      logger?.call('events stream closed with 1008 (slow consumer); resyncing state');
    }
    onClosed?.call(code);
    _channel = null;
    _sub = null;
    if (!_lifecycleController.isClosed) _lifecycleController.add(false);
    _scheduleReconnect();
  }

  void _teardown() {
    _sub?.cancel();
    _sub = null;
    final channel = _channel;
    _channel = null;
    channel?.sink.close(1000, 'client retarget');
  }

  void _scheduleReconnect() {
    if (_closed) return;
    final delay = _backoff;
    final doubled = delay * 2;
    _backoff = doubled > _maxBackoff ? _maxBackoff : doubled;
    _reconnectTimer = Timer(delay, () {
      _reconnectTimer = null;
      if (!_closed && _channel == null) _dial();
    });
  }
}
