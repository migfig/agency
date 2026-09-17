import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../run/run_state.dart';

class SummaryBar extends StatelessWidget {
  const SummaryBar({super.key, required this.state, this.workflowName});

  final RunState state;
  final String? workflowName;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final elapsed = DateTime.now().difference(state.startedAt ?? DateTime.now());
    return SizedBox(
      height: 40,
      child: DecoratedBox(
        decoration: BoxDecoration(
          border: Border(
            bottom: BorderSide(color: scheme.onSurfaceVariant.withValues(alpha: 0.35)),
          ),
        ),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          child: Row(
            children: [
              Flexible(
                child: Text(
                  workflowName ?? state.runId,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(fontWeight: FontWeight.w600),
                ),
              ),
              const SizedBox(width: 12),
              Text(
                state.runStatus,
                style: TextStyle(
                  color: state.runStatus == 'running' ? scheme.primary : scheme.onSurfaceVariant,
                ),
              ),
              if (state.runCancelled) ...[
                const SizedBox(width: 12),
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                  decoration: BoxDecoration(
                    border: Border.all(color: scheme.error),
                    borderRadius: BorderRadius.circular(4),
                  ),
                  child: Text(
                    'cancelled',
                    style: TextStyle(color: scheme.error, fontSize: 11),
                  ),
                ),
              ],
              const Spacer(),
              Text(
                _formatDuration(math.max(0, elapsed.inMilliseconds / 1000.0)),
                style: TextStyle(color: scheme.onSurfaceVariant),
              ),
              if (state.runStatus != 'running') ...[
                const SizedBox(width: 12),
                Text(
                  '${(state.totalTokens ?? _totalTokens(state))} tokens',
                  style: TextStyle(color: scheme.onSurfaceVariant),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }

  int _totalTokens(RunState state) {
    return state.nodes.values.fold(0, (s, n) => s + (n.tokens ?? 0));
  }

  String _formatDuration(double seconds) {
    final s = seconds.round();
    final h = s ~/ 3600;
    final m = (s % 3600) ~/ 60;
    final sec = s % 60;
    if (h > 0) return '$h:${m.toString().padLeft(2, '0')}:${sec.toString().padLeft(2, '0')}';
    return '$m:${sec.toString().padLeft(2, '0')}';
  }
}
