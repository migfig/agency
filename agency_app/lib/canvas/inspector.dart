import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../api/models.dart';
import '../../run/run_state.dart';

class Inspector extends StatelessWidget {
  const Inspector({super.key, required this.node, required this.onClose});

  final SNodeState node;
  final VoidCallback onClose;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Positioned(
      top: 8,
      right: 8,
      bottom: 8,
      width: 320,
      child: Card(
        elevation: 0,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(12),
          side: BorderSide(color: scheme.onSurface.withValues(alpha: 0.25)),
        ),
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  Expanded(
                    child: Text(
                      node.nodeId.toUpperCase(),
                      style: const TextStyle(
                        fontWeight: FontWeight.w600,
                        fontSize: 16,
                      ),
                    ),
                  ),
                  IconButton(
                    icon: const Icon(Icons.close, size: 20),
                    onPressed: onClose,
                  ),
                ],
              ),
              const SizedBox(height: 8),
              _row('Type', node.type),
              if (node.model != null) _row('Model', node.model!),
              if (node.fallbackModel != null || node.onFallbackPath)
                _row('Fallback', node.fallbackModel ?? '—'),
              _row('State', node.status.wire),
              _row('Attempt', '${node.attempt}/${node.maxAttempts}'),
              if (node.tokens != null) _row('Tokens', '${node.tokens}'),
              if (node.durationSeconds != null)
                _row(
                  'Duration',
                  '${node.durationSeconds!.toStringAsFixed(1)}s',
                ),
              if (node.error != null) ...[
                const SizedBox(height: 4),
                Text(node.error!, style: TextStyle(color: scheme.error)),
              ],
              if (node.skipReason != null) ...[
                const SizedBox(height: 4),
                Text(
                  'Skipped: ${node.skipReason}',
                  style: TextStyle(color: scheme.onSurfaceVariant),
                ),
              ],
              if (node.status == NodeStatus.awaitingRetry &&
                  node.nextRetryAt != null) ...[
                const SizedBox(height: 4),
                _retryCountdown(node.nextRetryAt!),
              ],
              if (node.output != null) ...[
                const SizedBox(height: 8),
                const Text(
                  'Output',
                  style: TextStyle(fontWeight: FontWeight.w600),
                ),
                const SizedBox(height: 4),
                Flexible(
                  child: SingleChildScrollView(
                    child: Text(
                      node.output!,
                      style: TextStyle(color: scheme.onSurfaceVariant),
                    ),
                  ),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }

  Widget _row(String label, String value) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Text(label, style: const TextStyle(color: Colors.grey)),
          Flexible(child: Text(value, overflow: TextOverflow.ellipsis)),
        ],
      ),
    );
  }

  Widget _retryCountdown(DateTime nextRetryAt) {
    final remaining = nextRetryAt.difference(DateTime.now());
    if (remaining.isNegative) {
      return const Text('retrying…', style: TextStyle(color: Colors.amber));
    }
    final secs = math.max(0, remaining.inSeconds);
    return Text(
      'Next retry in ${secs}s',
      style: const TextStyle(color: Colors.amber),
    );
  }
}
