import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../api/agency_client.dart';
import '../api/agency_events.dart';
import '../api/models.dart';
import '../canvas/classic/classic_topology.dart';
import '../catalog/workflow_catalog.dart';
import '../catalog/workflow_def.dart';
import '../run/run_store.dart';

/// The tri-state connection link (US5, R11 / contracts/ui.md).
enum ServerLink { connected, reconnecting, disconnected }

enum CanvasMode { abyss, classic }

/// App-level state managed as a [ChangeNotifier].
///
/// Bootstraps persistent settings from [SharedPreferences] and holds references
/// to the catalog, run stores, and the connection link. The link derives from
/// two oracles (R11): a 5 s `GET /health` poll (server reachability) and the
/// events WebSocket lifecycle (reconnect signal).
///
/// The reachability oracle is debounced: the server counts as unreachable
/// (and the rendered run stale) only after [_disconnectGracePolls] consecutive
/// failed polls. A single failed poll — a transient blip that recovers by the
/// next poll — keeps the UI in its current state, so brief outages don't flash
/// the scary disconnected/stale state (US5 polish).
class AppState extends ChangeNotifier {
  static const String _kThemeMode = 'theme_mode';
  static const String _kServerAddress = 'server_address';
  static const String _kWorkflowsDir = 'workflows_dir';
  static const String _kCanvasMode = 'canvas_mode';
  static const String _kCanvasTopology = 'canvas_topology';
  static const String _kSidebarVisible = 'sidebar_visible';
  static const String _kMonitorVisible = 'monitor_visible';

  static const String _defaultServerAddress = 'http://127.0.0.1:8000';
  static const String _defaultWorkflowsDir = 'workflows';
  static const ThemeMode _defaultThemeMode = ThemeMode.dark;
  static const CanvasMode _defaultCanvasMode = CanvasMode.abyss;
  static const CanvasTopology _defaultCanvasTopology =
      CanvasTopology.leftRight;
  static const Duration _healthPollInterval = Duration(seconds: 5);

  /// Consecutive failed health polls before the UI declares the server
  /// unreachable. With the 5 s poll interval this is ~10 s of unreachability;
  /// a blip that recovers within one poll cycle never surfaces.
  static const int _disconnectGracePolls = 2;

  SharedPreferences? _prefs;

  /// Current theme mode (persisted).
  ThemeMode _themeMode = _defaultThemeMode;
  ThemeMode get themeMode => _themeMode;

  /// Current canvas rendering mode (persisted).
  CanvasMode _canvasMode = _defaultCanvasMode;
  CanvasMode get canvasMode => _canvasMode;

  /// Current classic-canvas flow topology (persisted).
  CanvasTopology _canvasTopology = _defaultCanvasTopology;
  CanvasTopology get canvasTopology => _canvasTopology;

  /// Whether the sidebar drawer is expanded (persisted). Defaults to expanded
  /// so first launch matches the previous always-visible layout.
  bool _sidebarVisible = true;
  bool get sidebarVisible => _sidebarVisible;

  /// Whether the resource monitor panel is shown (persisted). Defaults to
  /// visible so first launch shows the panel (FR-004).
  bool _monitorVisible = true;
  bool get monitorVisible => _monitorVisible;

  /// Server address (persisted).
  String _serverAddress = _defaultServerAddress;
  String get serverAddress => _serverAddress;

  /// Workflows directory path (persisted).
  String _workflowsDir = _defaultWorkflowsDir;
  String get workflowsDir => _workflowsDir;

  /// The currently selected run id (null = no selection).
  String? _selectedRunId;
  String? get selectedRunId => _selectedRunId;

  /// History list from the server.
  List<RunHistoryEntry> _history = const [];
  List<RunHistoryEntry> get history => List.unmodifiable(_history);

  /// Catalog managed by [WorkflowCatalog]. Widgets listen to this notifier
  /// directly for live path/definition changes.
  final WorkflowCatalog? _catalog;
  WorkflowCatalog? get catalog => _catalog;

  /// Per-run state stores keyed by run id.
  final Map<String, RunStore> _runStores = {};

  /// The selected run's store (null if no run selected).
  RunStore? get selectedRunStore =>
      _selectedRunId != null ? _runStores[_selectedRunId!] : null;

  AgencyClient? _client;
  AgencyEvents? _events;
  StreamSubscription<bool>? _lifecycleSub;
  Timer? _healthPoll;
  int _healthFailures = 0;
  bool _wsOpen = false;
  bool _monitoring = false;

  /// The server counts as unreachable only after [_disconnectGracePolls]
  /// consecutive failed health polls (a transient blip stays hidden).
  bool get _isDisconnected => _healthFailures >= _disconnectGracePolls;

  /// The tri-state connection link (R11).
  ServerLink get link {
    if (_isDisconnected) return ServerLink.disconnected;
    return _wsOpen ? ServerLink.connected : ServerLink.reconnecting;
  }

  AppState({this._catalog});

  /// Load persisted settings from [SharedPreferences]. Call once on startup.
  Future<void> init() async {
    _prefs = await SharedPreferences.getInstance();
    _loadPrefs();
  }

  void _loadPrefs() {
    if (_prefs == null) return;
    final rawTheme = _prefs!.getString(_kThemeMode);
    if (rawTheme == 'light') _themeMode = ThemeMode.light;
    final rawCanvas = _prefs!.getString(_kCanvasMode);
    if (rawCanvas == 'classic') _canvasMode = CanvasMode.classic;
    // Unrecognized values keep the default (mirrors canvas mode above).
    final rawTopology = _prefs!.getString(_kCanvasTopology);
    for (final t in CanvasTopology.values) {
      if (t.name == rawTopology) {
        _canvasTopology = t;
        break;
      }
    }
    _serverAddress = _prefs!.getString(_kServerAddress) ?? _defaultServerAddress;
    _workflowsDir = _prefs!.getString(_kWorkflowsDir) ?? _defaultWorkflowsDir;
    // A missing key keeps the expanded default; only an explicit `false`
    // collapses the sidebar (mirrors the canvas-mode "unrecognized → default").
    _sidebarVisible = _prefs!.getBool(_kSidebarVisible) ?? true;
    // A missing key keeps the visible default; only an explicit `false`
    // hides the panel (mirrors the sidebar toggle above).
    _monitorVisible = _prefs!.getBool(_kMonitorVisible) ?? true;
  }

  void setThemeMode(ThemeMode mode) {
    if (mode != _themeMode) {
      _themeMode = mode;
      _prefs?.setString(_kThemeMode, mode == ThemeMode.dark ? 'dark' : 'light');
      notifyListeners();
    }
  }

  void setCanvasMode(CanvasMode mode) {
    if (mode != _canvasMode) {
      _canvasMode = mode;
      _prefs?.setString(_kCanvasMode, mode.name);
      notifyListeners();
    }
  }

  void setCanvasTopology(CanvasTopology topology) {
    if (topology != _canvasTopology) {
      _canvasTopology = topology;
      _prefs?.setString(_kCanvasTopology, topology.name);
      notifyListeners();
    }
  }

  /// Expand or collapse the sidebar drawer (persisted).
  void setSidebarVisible(bool visible) {
    if (visible != _sidebarVisible) {
      _sidebarVisible = visible;
      _prefs?.setBool(_kSidebarVisible, visible);
      notifyListeners();
    }
  }

  /// Show or hide the resource monitor panel (persisted). Sampling and the
  /// panel state (histories, `current`, `collapsed`) live in the
  /// `ResourceMonitor` controller, so hiding never loses them (US2-AS5).
  void setMonitorVisible(bool visible) {
    if (visible != _monitorVisible) {
      _monitorVisible = visible;
      _prefs?.setBool(_kMonitorVisible, visible);
      notifyListeners();
    }
  }

  /// Persist the new address, retarget client + events, and re-poll health
  /// immediately (T047).
  void setServerAddress(String address) {
    if (address == _serverAddress) return;
    _serverAddress = address;
    _prefs?.setString(_kServerAddress, address);
    final client = _client;
    final events = _events;
    if (client == null || events == null) {
      notifyListeners();
      return;
    }
    client.baseUrl = address;
    events.retarget(address);
    // The new address is unproven: show disconnected until it answers a poll.
    _healthFailures = _disconnectGracePolls;
    _markStale();
    _updateLink();
    unawaited(_pollHealth());
  }

  void setWorkflowsDir(String dir) {
    if (dir != _workflowsDir) {
      _workflowsDir = dir;
      _prefs?.setString(_kWorkflowsDir, dir);
      notifyListeners();
    }
  }

  /// Attach the transport pair and follow the WebSocket lifecycle.
  void attachTransport(AgencyClient client, AgencyEvents events) {
    _client = client;
    _events = events;
    events.onClosed = _onWsClosed;
    _wsOpen = events.connected;
    _lifecycleSub = events.lifecycle.listen((open) {
      _wsOpen = open;
      _updateLink();
    });
  }

  /// Start the 5 s health poll (reachability oracle) and open the events
  /// stream. Call once after [attachTransport].
  void startMonitoring() {
    if (_monitoring) return;
    _monitoring = true;
    unawaited(_pollHealth());
    _healthPoll = Timer.periodic(
      _healthPollInterval,
      (_) => unawaited(_pollHealth()),
    );
    _events?.connect();
  }

  /// One health poll. The periodic poll drives this in production; tests call
  /// it directly as a seam.
  Future<void> tickHealth() => _pollHealth();

  Future<void> _pollHealth() async {
    final client = _client;
    if (client == null) return;
    bool ok;
    try {
      await client.health();
      ok = true;
    } catch (_) {
      ok = false;
    }
    final wasDisconnected = _isDisconnected;
    _healthFailures = ok ? 0 : _healthFailures + 1;
    final isDisconnected = _isDisconnected;
    if (!wasDisconnected && isDisconnected) {
      // The grace window has elapsed: the server is really gone.
      _markStale();
    } else if (wasDisconnected && !isDisconnected) {
      // The server is back: resync so the stale banner clears (R11).
      await _onEnterConnected();
    }
    _updateLink();
  }

  /// Entering `connected` (R11): refresh history and resync the rendered run
  /// so the canvas matches the server truth (SC-002/SC-006). The resync is the
  /// step that clears the stale banner, so it runs even if the history refresh
  /// fails.
  Future<void> _onEnterConnected() async {
    final client = _client;
    if (client == null) return;
    try {
      setHistory(await client.listRuns());
    } catch (_) {
      // History refresh failed; the resync below still clears the stale
      // banner against server truth.
    }
    final store = selectedRunStore;
    final runId = store?.state?.runId ?? _selectedRunId;
    if (store == null || runId == null) return;
    try {
      await store.resync(
        runId,
        client.runStatus,
        fetchResult: client.runResult,
      );
    } catch (_) {}
  }

  void _markStale() {
    selectedRunStore?.markStale();
  }

  /// A 1008 close means the server dropped the stream as a slow consumer:
  /// re-fetch the authoritative snapshot for the rendered run (R11).
  void _onWsClosed(int closeCode) {
    if (closeCode != 1008) return;
    final client = _client;
    final store = selectedRunStore;
    final runId = store?.state?.runId;
    if (client == null || store == null || runId == null) return;
    unawaited(store.resync(
      runId,
      client.runStatus,
      fetchResult: client.runResult,
    ).catchError((_) {}));
  }

  void _updateLink() {
    notifyListeners();
  }

  /// Select a run. Creates a [RunStore] if needed.
  void selectRun(String runId, {WorkflowDefinition? workflowDefinition}) {
    if (_selectedRunId == runId) return;
    _selectedRunId = runId;
    if (!_runStores.containsKey(runId)) {
      _runStores[runId] = RunStore(workflowDefinition: workflowDefinition);
    }
    notifyListeners();
  }

  /// Deselect the current run.
  void deselectRun() {
    _selectedRunId = null;
    notifyListeners();
  }

  /// Update the history list (called after `GET /runs`).
  void setHistory(List<RunHistoryEntry> entries) {
    if (!listEquals(_history, entries)) {
      _history = List.from(entries);
      notifyListeners();
    }
  }

  /// Get or create a [RunStore] for a given run id.
  RunStore storeFor(String runId, {WorkflowDefinition? workflowDefinition}) {
    return _runStores.putIfAbsent(
      runId,
      () => RunStore(workflowDefinition: workflowDefinition),
    );
  }

  /// Clean up resources for a completed or deselected run.
  void disposeRunStore(String runId) {
    final store = _runStores.remove(runId);
    store?.dispose();
    if (_selectedRunId == runId) _selectedRunId = null;
  }

  @override
  void dispose() {
    _healthPoll?.cancel();
    _healthPoll = null;
    _lifecycleSub?.cancel();
    _lifecycleSub = null;
    for (final store in _runStores.values) {
      store.dispose();
    }
    _runStores.clear();
    _catalog?.dispose();
    super.dispose();
  }
}
