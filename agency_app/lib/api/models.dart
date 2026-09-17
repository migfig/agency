import 'dart:convert';

/// Wire-faithful mirrors of the Agency HTTP + WebSocket API.
///
/// These classes decode the REAL server shapes (specs/001 + specs/002,
/// `src/agency/api/schemas.py` + `src/agency/core/events.py`), not the idealized
/// `data-model.md` field names. Field names are camelCased Dart accessors over
/// snake_case wire keys.

/// The 7-state node-status vocabulary. The wire (registry.py) uses exactly
/// these strings, so this is a 1:1 mapping with the API.
enum NodeStatus {
  pending('pending'),
  running('running'),
  awaitingRetry('awaiting_retry'),
  completed('completed'),
  completedFallback('completed_fallback'),
  failed('failed'),
  skipped('skipped');

  const NodeStatus(this.wire);
  final String wire;

  static NodeStatus parse(String wire) => values.firstWhere(
        (s) => s.wire == wire,
        orElse: () => NodeStatus.pending,
      );
}

/// A single decoded WebSocket broadcast frame. Also holds the [knownEventTypes]
/// vocabulary the server can emit; unknown types are dropped by
/// [decodeEventFrame] (FR-020).
class EventFrame {
  const EventFrame({
    required this.eventType,
    required this.seq,
    required this.timestamp,
    required this.runId,
    required this.payload,
  });

  static const List<String> knownEventTypes = [
    'vram_threshold_exceeded',
    'vram_freed',
    'model_offloaded',
    'model_reloaded',
    'agent_queued',
    'agent_dequeued',
    'phase_started',
    'phase_completed',
    'summarization_triggered',
    'node_queued',
    'node_started',
    'node_completed',
    'node_failed',
    'node_retrying',
    'node_skipped',
    'fallback_activated',
    'run_cancel',
    'node_input_requested',
    'node_input_resolved',
  ];

  final String eventType;
  final int seq;
  final DateTime timestamp;
  final String? runId;
  final Map<String, dynamic> payload;
}

DateTime? parseIso(String? raw) {
  if (raw == null || raw.isEmpty) return null;
  return DateTime.parse(raw).toUtc();
}

int? asInt(dynamic v) => v is num ? v.toInt() : (v is String ? int.tryParse(v) : null);
double? asDouble(dynamic v) => v is num ? v.toDouble() : (v is String ? double.tryParse(v) : null);

/// Decode a broadcast frame map. Returns `null` (dropping the frame) when the
/// `event_type` is missing or not one of the known types (FR-020).
EventFrame? decodeEventFrame(dynamic raw) {
  if (raw is! Map) return null;
  final m = raw.cast<String, dynamic>();
  final type = m['event_type'];
  if (type is! String || !EventFrame.knownEventTypes.contains(type)) return null;
  final ts = parseIso(m['timestamp'] as String?);
  if (ts == null) return null;
  final payload = (m['payload'] is Map) ? (m['payload'] as Map).cast<String, dynamic>() : const <String, dynamic>{};
  final runId = m['run_id'] as String?;
  final seq = asInt(m['seq']) ?? 0;
  return EventFrame(
    eventType: type,
    seq: seq,
    timestamp: ts,
    runId: runId,
    payload: payload,
  );
}

/// Decode a broadcast frame from its raw JSON string (FR-020 drop semantics).
EventFrame? decodeEventFrameString(String raw) {
  dynamic decoded;
  try {
    decoded = jsonDecode(raw);
  } catch (_) {
    return null;
  }
  return decodeEventFrame(decoded);
}

class RunAccepted {
  const RunAccepted({required this.runId, required this.state, required this.workflow});
  final String runId;
  final String state;
  final String workflow;

  factory RunAccepted.fromJson(Map<String, dynamic> j) => RunAccepted(
        runId: j['run_id'] as String,
        state: j['state'] as String? ?? 'running',
        workflow: j['workflow'] as String? ?? '',
      );
}

class RunHistoryEntry {
  const RunHistoryEntry({
    required this.runId,
    required this.workflowName,
    required this.startedAt,
    required this.state,
  });
  final String runId;
  final String workflowName;
  final DateTime startedAt;
  final String state;

  factory RunHistoryEntry.fromJson(Map<String, dynamic> j) => RunHistoryEntry(
        runId: j['run_id'] as String,
        workflowName: j['workflow_name'] as String? ?? '',
        startedAt: parseIso(j['started_at'] as String?) ?? DateTime.fromMillisecondsSinceEpoch(0),
        state: j['state'] as String? ?? 'interrupted',
      );
}

class NodeStateView {
  const NodeStateView({
    required this.status,
    this.model,
    this.tokens,
    this.durationSeconds,
    required this.attempt,
    this.fallback,
    this.reason,
  });
  final String status;
  final String? model;
  final int? tokens;
  final double? durationSeconds;
  final int attempt;
  final String? fallback;
  final String? reason;

  factory NodeStateView.fromJson(Map<String, dynamic> j) => NodeStateView(
        status: j['status'] as String? ?? 'pending',
        model: j['model'] as String?,
        tokens: asInt(j['tokens']),
        durationSeconds: asDouble(j['duration_seconds']),
        attempt: asInt(j['attempt']) ?? 1,
        fallback: j['fallback'] as String?,
        reason: j['reason'] as String?,
      );
}

class PendingInputView {
  const PendingInputView({required this.nodeId, required this.prompt, this.deadline});
  final String nodeId;
  final String prompt;
  final DateTime? deadline;

  factory PendingInputView.fromJson(Map<String, dynamic> j) => PendingInputView(
        nodeId: j['node_id'] as String? ?? '',
        prompt: j['prompt'] as String? ?? '',
        deadline: parseIso(j['deadline'] as String?),
      );
}

class RunStatusView {
  const RunStatusView({
    required this.runId,
    required this.workflowName,
    required this.state,
    required this.startedAt,
    this.replaySource,
    required this.nodes,
    required this.pendingInputs,
  });
  final String runId;
  final String workflowName;
  final String state;
  final DateTime startedAt;
  final String? replaySource;
  final Map<String, NodeStateView> nodes;
  final List<PendingInputView> pendingInputs;

  factory RunStatusView.fromJson(Map<String, dynamic> j) {
    final nodes = (j['nodes'] as Map<String, dynamic>? ?? const {})
        .map((k, v) => MapEntry(k, NodeStateView.fromJson((v as Map).cast<String, dynamic>())));
    final pending = (j['pending_inputs'] as List<dynamic>? ?? const <dynamic>[])
        .map((e) => PendingInputView.fromJson((e as Map).cast<String, dynamic>()))
        .toList();
    return RunStatusView(
      runId: j['run_id'] as String,
      workflowName: j['workflow_name'] as String? ?? '',
      state: j['state'] as String? ?? 'running',
      startedAt: parseIso(j['started_at'] as String?) ?? DateTime.fromMillisecondsSinceEpoch(0),
      replaySource: j['replay_source'] as String?,
      nodes: nodes,
      pendingInputs: pending,
    );
  }
}

class RunResultNodeView {
  const RunResultNodeView({
    required this.nodeId,
    required this.status,
    this.model,
    this.tokens,
    this.durationSeconds,
    required this.attempt,
    this.fallback,
    this.reason,
    this.output,
  });
  final String nodeId;
  final String status;
  final String? model;
  final int? tokens;
  final double? durationSeconds;
  final int attempt;
  final String? fallback;
  final String? reason;
  final String? output;

  factory RunResultNodeView.fromJson(Map<String, dynamic> j) => RunResultNodeView(
        nodeId: j['node_id'] as String? ?? '',
        status: j['status'] as String? ?? 'pending',
        model: j['model'] as String?,
        tokens: asInt(j['tokens']),
        durationSeconds: asDouble(j['duration_seconds']),
        attempt: asInt(j['attempt']) ?? 1,
        fallback: j['fallback'] as String?,
        reason: j['reason'] as String?,
        output: j['output']?.toString(),
      );
}

class RunResultView {
  const RunResultView({
    required this.runId,
    required this.status,
    required this.totalTokens,
    required this.durationSeconds,
    required this.nodes,
  });
  final String runId;
  final String status;
  final int totalTokens;
  final double durationSeconds;
  final List<RunResultNodeView> nodes;

  factory RunResultView.fromJson(Map<String, dynamic> j) {
    final nodes = (j['nodes'] as List<dynamic>? ?? const <dynamic>[])
        .map((e) => RunResultNodeView.fromJson((e as Map).cast<String, dynamic>()))
        .toList();
    return RunResultView(
      runId: j['run_id'] as String,
      status: j['status'] as String? ?? 'interrupted',
      totalTokens: asInt(j['total_tokens']) ?? 0,
      durationSeconds: asDouble(j['duration_seconds']) ?? 0.0,
      nodes: nodes,
    );
  }
}

class HealthResponse {
  const HealthResponse({required this.status, required this.logDir});
  final String status;
  final String logDir;

  factory HealthResponse.fromJson(Map<String, dynamic> j) => HealthResponse(
        status: j['status'] as String? ?? 'unknown',
        logDir: j['log_dir'] as String? ?? '',
      );
}

class ErrorBody {
  const ErrorBody({required this.code, required this.message, this.details});
  final String code;
  final String message;
  final Map<String, dynamic>? details;

  factory ErrorBody.fromJson(Map<String, dynamic> j) => ErrorBody(
        code: j['code'] as String? ?? 'invalid_request',
        message: j['message'] as String? ?? '',
        details: (j['details'] is Map) ? (j['details'] as Map).cast<String, dynamic>() : null,
      );

  /// Parse the full `{"error": {...}}` response envelope; `null`-safe.
  static ErrorBody? parseResponse(dynamic jsonBody) {
    if (jsonBody is! Map) return null;
    final inner = jsonBody['error'];
    if (inner is! Map) return null;
    return ErrorBody.fromJson(inner.cast<String, dynamic>());
  }
}

/// The 9 API error codes (schemas.py).
class ErrorCodes {
  static const String unknownWorkflow = 'unknown_workflow';
  static const String invalidWorkflow = 'invalid_workflow';
  static const String modelValidationFailed = 'model_validation_failed';
  static const String noReplaySource = 'no_replay_source';
  static const String unknownRun = 'unknown_run';
  static const String unknownNode = 'unknown_node';
  static const String inputNotAwaiting = 'input_not_awaiting';
  static const String runNotFinished = 'run_not_finished';
  static const String invalidRequest = 'invalid_request';

  static const List<String> all = [
    unknownWorkflow,
    invalidWorkflow,
    modelValidationFailed,
    noReplaySource,
    unknownRun,
    unknownNode,
    inputNotAwaiting,
    runNotFinished,
    invalidRequest,
  ];
}

/// Map a wire error code to a short, human, action-oriented message.
String userFacingMessage(String code) {
  switch (code) {
    case ErrorCodes.unknownWorkflow:
      return 'That workflow is not in the catalog.';
    case ErrorCodes.invalidWorkflow:
      return 'The workflow file could not be validated.';
    case ErrorCodes.modelValidationFailed:
      return 'A required model could not be resolved before the run.';
    case ErrorCodes.noReplaySource:
      return 'No previous run is available to replay from.';
    case ErrorCodes.unknownRun:
      return 'This run is not known to the server.';
    case ErrorCodes.unknownNode:
      return 'That node does not exist in the run.';
    case ErrorCodes.inputNotAwaiting:
      return 'The node is not waiting for input.';
    case ErrorCodes.runNotFinished:
      return 'The run has not finished yet.';
    case ErrorCodes.invalidRequest:
      return 'The request was malformed.';
    default:
      return 'The server reported an error.';
  }
}
