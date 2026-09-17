import 'package:yaml/yaml.dart';

/// The 6 node types (yaml_engine/schema.py).
enum NodeType {
  agent('agent'),
  conditional('conditional'),
  merge('merge'),
  broadcast('broadcast'),
  humanInLoop('human_in_loop'),
  toolCall('tool_call');

  const NodeType(this.wire);
  final String wire;

  static NodeType? tryParse(String s) {
    for (final t in values) {
      if (t.wire == s) return t;
    }
    return null;
  }
}

/// Raised when a workflow YAML is unusable; the message names the offending
/// node/field so the catalog can render it readable (FR-022).
class WorkflowParseError implements Exception {
  const WorkflowParseError(this.message);
  final String message;

  @override
  String toString() => 'WorkflowParseError: $message';
}

class RetryPolicyMirror {
  const RetryPolicyMirror({
    required this.maxAttempts,
    required this.backoff,
    this.baseDelaySeconds,
    this.maxDelaySeconds,
    this.timeoutSeconds,
  });
  final int maxAttempts;
  final String backoff;
  final double? baseDelaySeconds;
  final double? maxDelaySeconds;
  final double? timeoutSeconds;
}

class EdgeDef {
  const EdgeDef({required this.fromId, required this.toId});
  final String fromId;
  final String toId;
}

class PhaseDef {
  const PhaseDef({required this.id, required this.name, required this.nodeIds});
  final String id;
  final String name;
  final List<String> nodeIds;
}

class WorkflowNodeDef {
  const WorkflowNodeDef({
    required this.id,
    required this.type,
    this.model,
    this.prompt,
    this.tool,
    this.retry,
    this.fallback,
  });
  final String id;
  final NodeType type;
  final String? model;
  final String? prompt;
  final String? tool;
  final RetryPolicyMirror? retry;
  final String? fallback;
}

class WorkflowDefinition {
  const WorkflowDefinition({
    required this.name,
    required this.sourcePath,
    required this.lastModified,
    required this.nodes,
    required this.edges,
    required this.phases,
  });
  final String name;
  final String sourcePath;
  final int lastModified;
  final Map<String, WorkflowNodeDef> nodes;
  final List<EdgeDef> edges;
  final List<PhaseDef> phases;

  WorkflowNodeDef? nodeById(String id) => nodes[id];

  /// The retry policy mirror for optimistic client classification, if any.
  RetryPolicyMirror? retryPolicyMirror(String id) => nodeById(id)?.retry;

  /// The declared fallback model for a node, if any.
  String? fallbackMirror(String id) => nodeById(id)?.fallback;
}

int? _i(dynamic v) => v is num ? v.toInt() : (v is String ? int.tryParse(v) : null);
double? _d(dynamic v) => v is num ? v.toDouble() : (v is String ? double.tryParse(v) : null);

RetryPolicyMirror? _parseRetry(dynamic r) {
  if (r is! Map) return null;
  return RetryPolicyMirror(
    maxAttempts: _i(r['max_attempts']) ?? 1,
    backoff: r['backoff'] is String ? r['backoff'] as String : 'exponential',
    baseDelaySeconds: _d(r['base_delay_seconds']) ?? 1.0,
    maxDelaySeconds: _d(r['max_delay_seconds']),
    timeoutSeconds: _d(r['timeout_seconds']),
  );
}

String? _parseModel(dynamic m) {
  if (m is String) return m;
  if (m is Map) {
    final inner = m['path'] ?? m['name'] ?? m['endpoint'];
    return inner is String ? inner : null;
  }
  return null;
}

String? _parseFallback(dynamic f) {
  // Real workflows declare fallback as a plain string (the model name);
  // tolerate a {model: name} map defensively.
  if (f is String) return f;
  if (f is Map) {
    final inner = f['model'];
    return inner is String ? inner : null;
  }
  return null;
}

/// Parse a workflow YAML document into a lenient [WorkflowDefinition].
///
/// Only the minimal required set is surfaced: `name`, `nodes` (id/type/model/
/// retry/fallback/prompt), `edges`, `phases`. Unknown keys are ignored; YAML
/// anchors/aliases and multi-line scalars resolve via the native loader.
WorkflowDefinition parseWorkflowDefinition(
  String yamlText, {
  String sourcePath = '',
  int lastModified = 0,
}) {
  dynamic loaded;
  try {
    loaded = loadYaml(yamlText);
  } on YamlException catch (e) {
    throw WorkflowParseError('unreadable YAML: ${e.message}');
  }

  if (loaded is! Map) {
    throw const WorkflowParseError('missing name');
  }

  final name = loaded['name'];
  if (name is! String || name.trim().isEmpty) {
    throw const WorkflowParseError('missing name');
  }

  final nodes = <String, WorkflowNodeDef>{};
  final rawNodes = loaded['nodes'];
  if (rawNodes is Map) {
    for (final entry in rawNodes.entries) {
      final key = entry.key is String ? entry.key as String : entry.key.toString();
      final spec = entry.value;
      if (spec is! Map) {
        throw WorkflowParseError('node "$key" is not a valid node definition');
      }

      final idRaw = spec['id'];
      final id = idRaw is String && idRaw.trim().isNotEmpty
          ? idRaw
          : (idRaw == null ? key : (throw WorkflowParseError('node "$key" has an invalid node id')));

      final typeRaw = spec['type'];
      if (typeRaw is! String) {
        throw WorkflowParseError('node "$id" has an invalid node type');
      }
      final type = NodeType.tryParse(typeRaw);
      if (type == null) {
        throw WorkflowParseError('node "$id" has an invalid node type: $typeRaw');
      }

      nodes[id] = WorkflowNodeDef(
        id: id,
        type: type,
        model: _parseModel(spec['model']),
        prompt: spec['prompt'] is String ? spec['prompt'] as String : null,
        tool: spec['tool'] is String
            ? spec['tool'] as String
            : (spec['tool_name'] is String ? spec['tool_name'] as String : null),
        retry: _parseRetry(spec['retry']),
        fallback: _parseFallback(spec['fallback']),
      );
    }
  }

  final edges = <EdgeDef>[];
  final rawEdges = loaded['edges'];
  if (rawEdges is List) {
    for (final e in rawEdges) {
      if (e is Map && e['from_id'] is String && e['to_id'] is String) {
        edges.add(EdgeDef(fromId: e['from_id'] as String, toId: e['to_id'] as String));
      }
    }
  }

  final phases = <PhaseDef>[];
  final rawPhases = loaded['phases'];
  if (rawPhases is List) {
    for (final p in rawPhases) {
      if (p is Map) {
        final id = p['id'] is String ? p['id'] as String : 'phase_${phases.length}';
        final name = p['name'] is String ? p['name'] as String : id;
        final ids = (p['node_ids'] is List
                ? p['node_ids'] as List
                : const <Object>[])
            .whereType<String>()
            .toList();
        phases.add(PhaseDef(id: id, name: name, nodeIds: ids));
      }
    }
  }

  return WorkflowDefinition(
    name: name,
    sourcePath: sourcePath,
    lastModified: lastModified,
    nodes: nodes,
    edges: edges,
    phases: phases,
  );
}
