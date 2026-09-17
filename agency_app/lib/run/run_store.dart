import 'dart:async';
import 'package:flutter/foundation.dart';
import '../api/models.dart';
import '../catalog/workflow_def.dart';
import 'run_state.dart';

/// Holds one run's [RunState], seeds it from a server snapshot, and applies
/// subsequent broadcast frames through the reducer.
///
/// Foreign / null `run_id` frames are dropped by [apply] itself (FR-015), so
/// this store never needs the rendered run id as a separate filter.
class RunStore extends ChangeNotifier {
  RunStore({this.workflowDefinition});

  /// How long the finalize pass keeps polling the status snapshot for the
  /// server to settle the run (10 × 500 ms).
  static const int _finalizeAttempts = 10;
  static const Duration _finalizeDelay = Duration(milliseconds: 500);

  final WorkflowDefinition? workflowDefinition;

  RunState? _state;
  RunState? get state => _state;
  String? get runId => _state?.runId;

  StreamSubscription? _sub;
  Future<RunStatusView> Function(String runId)? _fetchStatus;
  Future<RunResultView> Function(String runId)? _fetchResult;
  VoidCallback? _onFinalized;
  bool _finalized = false;
  bool _finalizing = false;
  Timer? _finalizeTimer;
  Completer<void>? _finalizeWaiter;
  int _generation = 0;

  /// Seed the store from a full server snapshot (research R2). Call once
  /// before [connect] so frames have a baseline state to reduce onto.
  void seed(RunStatusView snapshot) {
    _state = reduce(snapshot, definition: workflowDefinition);
    notifyListeners();
  }

  /// Subscribe to an event stream and reduce each frame into the current state.
  ///
  /// When [fetchStatus] and [fetchResult] are supplied, the store finalizes
  /// the run once every node has reached a terminal status: broadcast frames
  /// carry no per-node output and there is no run-level terminal event, so
  /// the authoritative status + result views (per-node output, total tokens)
  /// are folded in at that point (US2). [onFinalized] fires once the finalize
  /// pass ends so callers can refresh dependent views (e.g. history).
  void connect(
    Stream<EventFrame> frames, {
    Future<RunStatusView> Function(String runId)? fetchStatus,
    Future<RunResultView> Function(String runId)? fetchResult,
    VoidCallback? onFinalized,
  }) {
    _cancel();
    _fetchStatus = fetchStatus;
    _fetchResult = fetchResult;
    _onFinalized = onFinalized;
    _sub = frames.listen(
      (frame) {
        final current = _state;
        if (current == null) return;
        _state = apply(current, frame, definition: workflowDefinition);
        notifyListeners();
        _maybeFinalize();
      },
      onError: (_) {},
      onDone: _cancel,
    );
  }

  /// Mark the rendered run stale while the server is unreachable (invariant
  /// 5): the last view is kept, the canvas desaturates and pauses.
  void markStale() {
    final current = _state;
    if (current == null || current.isStale) return;
    _state = current.copyWith(isStale: true);
    notifyListeners();
  }

  /// Re-fetch the authoritative snapshot for [runId] and rebuild state from it
  /// (research R2). Called after a state-changing action (e.g. an HIL submit)
  /// so the UI reflects the server truth; the rebuild is dropped when the
  /// store no longer holds [runId] (FR-015).
  ///
  /// When [fetchResult] is supplied and the snapshot is already settled, the
  /// terminal result view is folded in as well — otherwise the re-seed would
  /// drop the per-node output accumulated from frames (snapshots carry none).
  Future<void> resync(
    String runId,
    Future<RunStatusView> Function(String runId) fetch, {
    Future<RunResultView> Function(String runId)? fetchResult,
  }) async {
    final snapshot = await fetch(runId);
    if (_state?.runId != runId) return;
    seed(snapshot);
    if (fetchResult != null && _isSettled(snapshot.state)) {
      _finalized = true;
      try {
        final result = await fetchResult(runId);
        if (_state?.runId == runId) applyResult(result);
      } catch (_) {
        // No result available (e.g. an interrupted run): the snapshot seed
        // above is the best node view available.
      }
    }
  }

  /// Fold a terminal run's result view into the current state (US2).
  /// Results for another run are ignored (run-id guard, FR-015).
  void applyResult(RunResultView result) {
    final current = _state;
    if (current == null || current.runId != result.runId) return;
    _state = mergeResult(current, result);
    _finalized = true;
    notifyListeners();
  }

  /// Drop the current run and forget its state.
  void reset() {
    _cancel();
    _stopFinalizeWait();
    _state = null;
    _finalized = false;
    _finalizing = false;
    _generation++;
    notifyListeners();
  }

  void _cancel() {
    _sub?.cancel();
    _sub = null;
  }

  bool _isSettled(String state) =>
      state == 'completed' || state == 'failed';

  /// Kick off the one-shot finalize pass once every node is terminal and the
  /// store was connected with fetchers (see [connect]).
  ///
  /// [allNodesTerminal] is the client's "the run is probably over" signal; the
  /// server is the arbiter. [_finalized] is only set once the server confirms a
  /// settled state (in [_finalize]/[applyResult]), so a pass that gives up
  /// (server still running) leaves it clear and a later frame can re-trigger.
  void _maybeFinalize() {
    if (_finalized || _finalizing) return;
    final status = _fetchStatus;
    final result = _fetchResult;
    final current = _state;
    if (status == null || result == null || current == null) return;
    if (!allNodesTerminal(current, definition: workflowDefinition)) return;
    _finalizing = true;
    unawaited(_finalize(current.runId, status, result));
  }

  /// Poll the status snapshot until the server settles the run, then seed
  /// from it and fold in the result view (per-node output, total tokens).
  /// The run-id/generation guard drops the work when the store moves on.
  Future<void> _finalize(
    String runId,
    Future<RunStatusView> Function(String runId) fetchStatus,
    Future<RunResultView> Function(String runId) fetchResult,
  ) async {
    final gen = _generation;
    bool alive() => gen == _generation && _state?.runId == runId;
    try {
      for (var i = 0; i < _finalizeAttempts; i++) {
        if (!alive()) return;
        RunStatusView snapshot;
        try {
          snapshot = await fetchStatus(runId);
        } catch (_) {
          return; // transport down; a later resync path recovers
        }
        if (!alive()) return;
        if (_isSettled(snapshot.state)) {
          seed(snapshot);
          try {
            final result = await fetchResult(runId);
            if (alive()) applyResult(result);
          } catch (_) {
            // No result available (e.g. an interrupted run): the snapshot
            // seed above is the best node view available.
          }
          // Server confirmed a settled state: the run is final. Marking it
          // here (not at trigger time) means a pass that gives up — the server
          // was still running — stays re-triggerable by a later frame.
          _finalized = true;
          return;
        }
        if (i < _finalizeAttempts - 1) {
          await _finalizeWait();
        }
      }
    } finally {
      _finalizing = false;
      if (gen == _generation) _onFinalized?.call();
    }
  }

  Future<void> _finalizeWait() {
    final waiter = Completer<void>();
    _finalizeWaiter = waiter;
    _finalizeTimer = Timer(_finalizeDelay, waiter.complete);
    return waiter.future;
  }

  void _stopFinalizeWait() {
    _finalizeTimer?.cancel();
    _finalizeTimer = null;
    final waiter = _finalizeWaiter;
    _finalizeWaiter = null;
    if (waiter != null && !waiter.isCompleted) waiter.complete();
  }

  @override
  void dispose() {
    _cancel();
    _stopFinalizeWait();
    _finalizing = false;
    _generation++;
    super.dispose();
  }
}
