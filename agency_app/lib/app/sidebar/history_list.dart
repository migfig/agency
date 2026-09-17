import 'package:flutter/material.dart';

import '../../api/models.dart';
import 'section_controls.dart';

/// The HISTORY sidebar section: past runs, newest first, each with a state
/// badge and a relative start age (US2). The section is collapsible from
/// its header and filters by run id / workflow name plus run state.
class HistoryList extends StatefulWidget {
  const HistoryList({
    super.key,
    required this.entries,
    required this.selectedRunId,
    required this.onSelect,
    this.now,
  });

  /// Server history, ascending (oldest first); displayed reversed.
  final List<RunHistoryEntry> entries;

  final String? selectedRunId;

  final ValueChanged<String> onSelect;

  /// Injectable for tests; defaults to [DateTime.now].
  final DateTime? now;

  @override
  State<HistoryList> createState() => _HistoryListState();
}

/// The run states the server derives (registry.py), in display order.
const List<String> _knownStates = [
  'running',
  'completed',
  'failed',
  'interrupted',
];

class _HistoryListState extends State<HistoryList> {
  bool _expanded = true;
  String _query = '';
  String _stateFilter = 'all';

  bool get _filtering => _query.trim().isNotEmpty || _stateFilter != 'all';

  List<RunHistoryEntry> get _filtered {
    final q = _query.trim().toLowerCase();
    final filtered = widget.entries
        .where((e) {
          if (_stateFilter != 'all' && e.state != _stateFilter) return false;
          if (q.isEmpty) return true;
          return e.workflowName.toLowerCase().contains(q) ||
              e.runId.toLowerCase().contains(q) ||
              shortRunId(e.runId).toLowerCase().contains(q);
        })
        .toList()
        .reversed
        .toList();
    return filtered;
  }

  /// The states present in the current history, in [_knownStates] order,
  /// so the menu only offers what is actually filterable.
  List<String> get _stateOptions {
    final present = <String>{};
    for (final e in widget.entries) {
      present.add(e.state);
    }
    return _knownStates.where(present.contains).toList();
  }

  @override
  Widget build(BuildContext context) {
    final reference = widget.now ?? DateTime.now();
    final filtered = _filtered;
    final total = widget.entries.length;
    final count = _filtering ? '${filtered.length}/$total' : '$total';
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SidebarSectionHeader(
          title: 'HISTORY',
          expanded: _expanded,
          onToggle: () => setState(() {
            _expanded = !_expanded;
          }),
          count: count,
        ),
        if (_expanded) ...[
          SidebarFilterField(
            hint: 'Filter runs…',
            onChanged: (q) => setState(() {
              _query = q;
            }),
            trailing: _stateMenu(),
          ),
          if (filtered.isEmpty)
            SidebarEmptyHint(
              text: total == 0 ? 'No runs yet.' : 'No matches.',
            )
          else
            for (final e in filtered) _row(context, e, reference),
        ],
      ],
    );
  }

  /// The compact state filter: All + the states present in the history.
  /// Selecting a state narrows the list; "All states" clears the filter.
  Widget _stateMenu() {
    final scheme = Theme.of(context).colorScheme;
    final active = _stateFilter != 'all';
    return PopupMenuButton<String>(
      tooltip: 'Filter by state',
      icon: Icon(
        Icons.filter_list,
        size: 16,
        color: active ? scheme.primary : scheme.onSurfaceVariant,
      ),
      onSelected: (s) => setState(() {
        _stateFilter = s;
      }),
      itemBuilder: (context) => [
        PopupMenuItem<String>(
          value: 'all',
          child: Text(
            'All states',
            style: TextStyle(
              fontWeight: _stateFilter == 'all' ? FontWeight.w600 : null,
            ),
          ),
        ),
        for (final s in _stateOptions)
          PopupMenuItem<String>(
            value: s,
            child: Text(
              s,
              style: TextStyle(
                fontWeight: _stateFilter == s ? FontWeight.w600 : null,
              ),
            ),
          ),
      ],
    );
  }

  Widget _row(BuildContext context, RunHistoryEntry entry, DateTime now) {
    final scheme = Theme.of(context).colorScheme;
    final selected = entry.runId == widget.selectedRunId;
    return InkWell(
      onTap: () => widget.onSelect(entry.runId),
      child: Container(
        decoration: selected
            ? BoxDecoration(
                color: scheme.primary.withValues(alpha: 0.12),
                border: Border(left: BorderSide(width: 3, color: scheme.primary)),
              )
            : null,
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    entry.workflowName,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(fontWeight: FontWeight.w500),
                  ),
                ),
                const SizedBox(width: 8),
                Text(
                  shortRunId(entry.runId),
                  style: TextStyle(color: scheme.onSurfaceVariant, fontSize: 12),
                ),
                const SizedBox(width: 8),
                _badge(context, entry.state),
              ],
            ),
            const SizedBox(height: 2),
            Text(
              relativeTimeAgo(entry.startedAt, now),
              style: TextStyle(color: scheme.onSurfaceVariant, fontSize: 12),
            ),
          ],
        ),
      ),
    );
  }

  Widget _badge(BuildContext context, String state) {
    final scheme = Theme.of(context).colorScheme;
    final Color color;
    switch (state) {
      case 'running':
        color = scheme.primary;
      case 'completed':
        color = const Color(0xFF4CAF50);
      case 'failed':
        color = scheme.error;
      default:
        color = scheme.onSurfaceVariant;
    }
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.16),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Text(state, style: TextStyle(color: color, fontSize: 11)),
    );
  }
}

/// Display form of a run id: ids of 12 characters or fewer are kept intact;
/// longer ones truncate to the prefix up to the first dash plus the next ten
/// characters (e.g. `r-0123456789abcdef…` → `r-0123456789`).
String shortRunId(String runId) {
  if (runId.length <= 12) return runId;
  final dash = runId.indexOf('-');
  if (dash > 0 && runId.length - dash - 1 >= 10) {
    return runId.substring(0, dash + 11);
  }
  return runId.substring(0, 12);
}

/// Human-friendly relative age: under a minute is "just now", then minutes,
/// hours, days.
String relativeTimeAgo(DateTime time, DateTime now) {
  final diff = now.difference(time);
  if (diff.inSeconds < 60) return 'just now';
  if (diff.inMinutes < 60) return '${diff.inMinutes}m ago';
  if (diff.inHours < 24) return '${diff.inHours}h ago';
  return '${diff.inDays}d ago';
}
