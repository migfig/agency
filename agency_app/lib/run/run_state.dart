import '../api/models.dart';
import '../catalog/workflow_def.dart';

/// Immutable per-node execution state (data-model S1 node view).
class SNodeState {
  const SNodeState({
    required this.nodeId,
    required this.type,
    this.model,
    required this.status,
    this.attempt = 1,
    this.maxAttempts = 1,
    this.queuedAt,
    this.startedAt,
    this.finishedAt,
    this.tokens,
    this.durationSeconds,
    this.error,
    this.skipReason,
    this.nextRetryAt,
    this.onFallbackPath = false,
    this.fallbackModel,
    this.output,
  });

  final String nodeId;
  final String type;
  final String? model;
  final NodeStatus status;
  final int attempt;
  final int maxAttempts;
  final DateTime? queuedAt;
  final DateTime? startedAt;
  final DateTime? finishedAt;
  final int? tokens;
  final double? durationSeconds;
  final String? error;
  final String? skipReason;
  final DateTime? nextRetryAt;
  final bool onFallbackPath;
  final String? fallbackModel;
  final String? output;

  SNodeState copyWith({
    String? nodeId,
    String? type,
    Object? model = _n,
    NodeStatus? status,
    int? attempt,
    int? maxAttempts,
    Object? queuedAt = _n,
    Object? startedAt = _n,
    Object? finishedAt = _n,
    Object? tokens = _n,
    Object? durationSeconds = _n,
    Object? error = _n,
    Object? skipReason = _n,
    Object? nextRetryAt = _n,
    bool? onFallbackPath,
    Object? fallbackModel = _n,
    Object? output = _n,
  }) {
    return SNodeState(
      nodeId: nodeId ?? this.nodeId,
      type: type ?? this.type,
      model: model == _n ? this.model : model as String?,
      status: status ?? this.status,
      attempt: attempt ?? this.attempt,
      maxAttempts: maxAttempts ?? this.maxAttempts,
      queuedAt: queuedAt == _n ? this.queuedAt : queuedAt as DateTime?,
      startedAt: startedAt == _n ? this.startedAt : startedAt as DateTime?,
      finishedAt: finishedAt == _n ? this.finishedAt : finishedAt as DateTime?,
      tokens: tokens == _n ? this.tokens : tokens as int?,
      durationSeconds: durationSeconds == _n ? this.durationSeconds : durationSeconds as double?,
      error: error == _n ? this.error : error as String?,
      skipReason: skipReason == _n ? this.skipReason : skipReason as String?,
      nextRetryAt: nextRetryAt == _n ? this.nextRetryAt : nextRetryAt as DateTime?,
      onFallbackPath: onFallbackPath ?? this.onFallbackPath,
      fallbackModel: fallbackModel == _n ? this.fallbackModel : fallbackModel as String?,
      output: output == _n ? this.output : output as String?,
    );
  }
}

/// Immutable run-level state (data-model S1).
class RunState {
  const RunState({
    required this.runId,
    required this.workflow,
    required this.runStatus,
    this.startedAt,
    required this.nodes,
    required this.pendingInputs,
    this.isStale = false,
    this.lastEventSeq,
    this.wantsResync = false,
    this.runCancelled = false,
    this.totalTokens,
  });

  final String runId;
  final String workflow;
  final String runStatus;
  final DateTime? startedAt;
  final Map<String, SNodeState> nodes;
  final List<PendingInputView> pendingInputs;
  final bool isStale;
  final int? lastEventSeq;
  final bool wantsResync;
  final bool runCancelled;
  final int? totalTokens;

  RunState copyWith({
    String? runId,
    String? workflow,
    String? runStatus,
    Object? startedAt = _n,
    Map<String, SNodeState>? nodes,
    List<PendingInputView>? pendingInputs,
    bool? isStale,
    Object? lastEventSeq = _n,
    bool? wantsResync,
    bool? runCancelled,
    Object? totalTokens = _n,
  }) {
    return RunState(
      runId: runId ?? this.runId,
      workflow: workflow ?? this.workflow,
      runStatus: runStatus ?? this.runStatus,
      startedAt: startedAt == _n ? this.startedAt : startedAt as DateTime?,
      nodes: nodes ?? this.nodes,
      pendingInputs: pendingInputs ?? this.pendingInputs,
      isStale: isStale ?? this.isStale,
      lastEventSeq: lastEventSeq == _n ? this.lastEventSeq : lastEventSeq as int?,
      wantsResync: wantsResync ?? this.wantsResync,
      runCancelled: runCancelled ?? this.runCancelled,
      totalTokens: totalTokens == _n ? this.totalTokens : totalTokens as int?,
    );
  }
}

const Object _n = Object();

SNodeState _baseNode(String id, WorkflowDefinition? def) {
  final d = def?.nodeById(id);
  return SNodeState(
    nodeId: id,
    type: d?.type.wire ?? 'unknown',
    model: d?.model,
    status: NodeStatus.pending,
    attempt: 1,
    maxAttempts: d?.retry?.maxAttempts ?? 1,
  );
}

bool _isTerminal(NodeStatus s) =>
    s == NodeStatus.completed ||
    s == NodeStatus.completedFallback ||
    s == NodeStatus.failed ||
    s == NodeStatus.skipped;

/// True when every node the run is expected to execute has reached a terminal
/// status: the run has no in-flight work left, so the server is about to settle
/// it into a terminal state and its result view is authoritative (research R2).
///
/// When [definition] is supplied the check is over the definition's complete
/// node set — the state's node mirror is only partial mid-run (it gains a node
/// as the server emits its first lifecycle event), so "every node in the state
/// is terminal" would be true too early, before downstream nodes have started.
/// Without a definition it falls back to the state's node set.
bool allNodesTerminal(RunState state, {WorkflowDefinition? definition}) {
  if (definition != null) {
    if (definition.nodes.isEmpty) return false;
    return definition.nodes.values.every((d) {
      final n = state.nodes[d.id];
      return n != null && _isTerminal(n.status);
    });
  }
  return state.nodes.isNotEmpty &&
      state.nodes.values.every((n) => _isTerminal(n.status));
}

int? _maxSeq(int? a, int b) => (a == null || b > a) ? b : a;

RunState _bump(RunState state, EventFrame frame, {bool wantsResync = false}) {
  return state.copyWith(
    isStale: false,
    lastEventSeq: _maxSeq(state.lastEventSeq, frame.seq),
    wantsResync: state.wantsResync || wantsResync,
  );
}

/// Build a full, authoritative [RunState] from a server snapshot.
///
/// Invariant 1: every definition node id appears in [RunState.nodes] (default
/// pending), and every snapshot node (including orphans) is included.
/// Invariant 2: statuses use the 7-state vocabulary. Invariant 4: a pending
/// input whose node is not running/awaiting_retry marks the state resync.
RunState reduce(RunStatusView snapshot, {WorkflowDefinition? definition}) {
  final nodes = <String, SNodeState>{};
  for (final d in (definition?.nodes.values ?? const <WorkflowNodeDef>[])) {
    nodes[d.id] = _baseNode(d.id, definition);
  }
  for (final e in snapshot.nodes.entries) {
    final v = e.value;
    final base = nodes[e.key] ?? _baseNode(e.key, definition);
    nodes[e.key] = SNodeState(
      nodeId: e.key,
      type: base.type,
      model: v.model ?? base.model,
      status: NodeStatus.parse(v.status),
      attempt: v.attempt,
      maxAttempts: base.maxAttempts,
      tokens: v.tokens,
      durationSeconds: v.durationSeconds,
      error: v.status == 'failed' ? v.reason : null,
      skipReason: v.status == 'skipped' ? v.reason : null,
      onFallbackPath: v.fallback != null,
      fallbackModel: v.fallback,
    );
  }

  var wantsResync = false;
  for (final p in snapshot.pendingInputs) {
    final n = nodes[p.nodeId];
    if (n == null || (n.status != NodeStatus.running && n.status != NodeStatus.awaitingRetry)) {
      wantsResync = true;
    }
  }

  return RunState(
    runId: snapshot.runId,
    workflow: snapshot.workflowName,
    runStatus: snapshot.state,
    startedAt: snapshot.startedAt,
    nodes: nodes,
    pendingInputs: snapshot.pendingInputs,
    isStale: false,
    lastEventSeq: null,
    wantsResync: wantsResync,
    runCancelled: false,
  );
}

/// Apply one broadcast frame to [state], returning a new state.
///
/// Foreign / null run_id frames are ignored (FR-015). A node in a terminal
/// status never re-transitions (the frame is dropped and resync scheduled).
/// `node_failed` is classified optimistically from the local definition's
/// retry/fallback mirrors; confirmed by `node_retrying` / `fallback_activated`.
RunState apply(RunState state, EventFrame frame, {WorkflowDefinition? definition}) {
  if (frame.runId != state.runId) return state;

  final t = frame.eventType;
  if (t == 'run_cancel') {
    return _bump(state, frame).copyWith(runCancelled: true);
  }

  final p = frame.payload;
  final id = p['node_id'];
  if (id is! String) return _bump(state, frame);

  // Human-in-the-loop input frames adjust the pending-input list directly;
  // they do not change node status (node_started already marks it running).
  if (t == 'node_input_requested') {
    final pending = List<PendingInputView>.from(state.pendingInputs)
      ..removeWhere((e) => e.nodeId == id);
    pending.add(PendingInputView(
      nodeId: id,
      prompt: p['prompt'] is String ? p['prompt'] as String : '',
      deadline: parseIso(p['deadline'] as String?),
    ));
    return _bump(state, frame).copyWith(pendingInputs: pending);
  }
  if (t == 'node_input_resolved') {
    final pending =
        state.pendingInputs.where((e) => e.nodeId != id).toList(growable: false);
    return _bump(state, frame).copyWith(pendingInputs: pending);
  }

  final existing = state.nodes[id];
  if (existing != null && _isTerminal(existing.status)) {
    return _bump(state, frame, wantsResync: true);
  }

  var node = existing ?? _baseNode(id, definition);
  final ts = frame.timestamp;
  final model = p['model'] is String ? p['model'] as String : node.model;

  switch (t) {
    case 'node_queued':
      node = node.copyWith(status: NodeStatus.pending, queuedAt: ts);
    case 'node_started':
      node = node.copyWith(
        status: NodeStatus.running,
        startedAt: ts,
        attempt: asInt(p['attempt']) ?? node.attempt,
        model: model,
      );
    case 'node_retrying':
      node = node.copyWith(
        status: NodeStatus.awaitingRetry,
        attempt: asInt(p['attempt']) ?? node.attempt,
        error: p['error'] is String ? p['error'] as String : node.error,
        nextRetryAt: ts.add(Duration(milliseconds: ((asDouble(p['delay_seconds']) ?? 0) * 1000).round())),
      );
    case 'node_failed':
      final fbSet = p['fallback'] is String && (p['fallback'] as String).isNotEmpty;
      final attempt = asInt(p['attempt']) ?? node.attempt;
      final d = definition?.nodeById(id);
      NodeStatus ns;
      bool onFb;
      if (fbSet) {
        ns = NodeStatus.failed;
        onFb = node.onFallbackPath;
      } else if (d == null) {
        ns = NodeStatus.failed;
        onFb = false;
      } else {
        final maxA = d.retry?.maxAttempts ?? 1;
        final hasFb = d.fallback != null && (d.fallback!).isNotEmpty;
        if (attempt < maxA) {
          ns = NodeStatus.awaitingRetry;
          onFb = false;
        } else if (hasFb) {
          ns = NodeStatus.awaitingRetry;
          onFb = true;
        } else {
          ns = NodeStatus.failed;
          onFb = node.onFallbackPath;
        }
      }
      node = node.copyWith(
        status: ns,
        attempt: attempt,
        error: p['error'] is String ? p['error'] as String : node.error,
        onFallbackPath: onFb,
        fallbackModel: p['fallback'] is String ? p['fallback'] as String : null,
      );
    case 'node_completed':
      final fb = p['fallback'] is String ? p['fallback'] as String : null;
      node = node.copyWith(
        status: fb != null ? NodeStatus.completedFallback : NodeStatus.completed,
        finishedAt: ts,
        tokens: asInt(p['tokens_used']),
        durationSeconds: asDouble(p['duration_seconds']),
        attempt: asInt(p['attempt']) ?? node.attempt,
        onFallbackPath: fb != null,
        fallbackModel: fb,
        model: model,
        output: p['output']?.toString(),
      );
    case 'fallback_activated':
      final fb = p['fallback'] is String ? p['fallback'] as String : null;
      node = node.copyWith(
        fallbackModel: fb,
        model: fb ?? node.model,
        onFallbackPath: true,
      );
    case 'node_skipped':
      node = node.copyWith(
        status: NodeStatus.skipped,
        finishedAt: ts,
        skipReason: p['reason'] is String ? p['reason'] as String : node.skipReason,
      );
    default:
      // phase / summarization / agent / vram / model frames for this run:
      // no node-state change, just bookkeeping.
      return _bump(state, frame);
  }

  final newNodes = Map<String, SNodeState>.from(state.nodes)..[id] = node;
  return state.copyWith(
    nodes: newNodes,
    isStale: false,
    lastEventSeq: _maxSeq(state.lastEventSeq, frame.seq),
  );
}

/// Mark a run state stale (disconnect) while keeping the last view (invariant 5).
RunState markStale(RunState state) => state.copyWith(isStale: true);

/// Fold a terminal run's result view (GET /runs/{id}/result) into [state]:
/// per-node tokens/duration/output refinement, the authoritative run status,
/// and the API-level total token count (US2).
RunState mergeResult(RunState state, RunResultView result) {
  final byId = <String, RunResultNodeView>{
    for (final v in result.nodes) v.nodeId: v,
  };
  final nodes = <String, SNodeState>{};
  for (final e in state.nodes.entries) {
    final r = byId[e.key];
    nodes[e.key] = r == null
        ? e.value
        : e.value.copyWith(
            tokens: r.tokens ?? e.value.tokens,
            durationSeconds: r.durationSeconds ?? e.value.durationSeconds,
            output: r.output ?? e.value.output,
          );
  }
  return state.copyWith(
    nodes: nodes,
    runStatus: result.status,
    totalTokens: result.totalTokens,
  );
}
