import 'package:flutter/material.dart';

import '../../catalog/catalog_entry.dart';
import 'section_controls.dart';

/// The WORKFLOWS sidebar section: the live catalog, each row with a Start
/// action (US1). The section is collapsible from its header and filters
/// rows by name or path as the catalog grows.
class WorkflowList extends StatefulWidget {
  const WorkflowList({
    super.key,
    required this.entries,
    required this.startingPaths,
    required this.startErrors,
    required this.onStart,
  });

  final List<CatalogEntry> entries;
  final Set<String> startingPaths;
  final Map<String, String> startErrors;
  final ValueChanged<CatalogEntry> onStart;

  @override
  State<WorkflowList> createState() => _WorkflowListState();
}

class _WorkflowListState extends State<WorkflowList> {
  bool _expanded = true;
  String _query = '';

  bool get _filtering => _query.trim().isNotEmpty;

  List<CatalogEntry> get _filtered {
    final q = _query.trim().toLowerCase();
    if (q.isEmpty) return widget.entries;
    return widget.entries
        .where((e) =>
            e.name.toLowerCase().contains(q) ||
            e.path.toLowerCase().contains(q))
        .toList();
  }

  @override
  Widget build(BuildContext context) {
    final filtered = _filtered;
    final total = widget.entries.length;
    final count = _filtering ? '${filtered.length}/$total' : '$total';
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SidebarSectionHeader(
          title: 'WORKFLOWS',
          expanded: _expanded,
          onToggle: () => setState(() {
            _expanded = !_expanded;
          }),
          count: count,
        ),
        if (_expanded) ...[
          SidebarFilterField(
            hint: 'Filter workflows…',
            onChanged: (q) => setState(() {
              _query = q;
            }),
          ),
          if (filtered.isEmpty)
            SidebarEmptyHint(
              text: total == 0 ? 'No workflows found.' : 'No matches.',
            )
          else
            for (final entry in filtered) _row(context, entry),
        ],
      ],
    );
  }

  Widget _row(BuildContext context, CatalogEntry entry) {
    final scheme = Theme.of(context).colorScheme;
    final dimmed = entry.isUnreadable;
    final starting = widget.startingPaths.contains(entry.path);
    final error = widget.startErrors[entry.path];
    return Tooltip(
      message: dimmed ? entry.parseError! : entry.path,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    entry.name,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      color: dimmed ? scheme.onSurfaceVariant : scheme.onSurface,
                    ),
                  ),
                ),
                if (starting)
                  const Padding(
                    padding: EdgeInsets.symmetric(horizontal: 8),
                    child: SizedBox(
                      width: 12,
                      height: 12,
                      child: CircularProgressIndicator(strokeWidth: 1.5),
                    ),
                  )
                else if (!dimmed)
                  TextButton(
                    onPressed: () => widget.onStart(entry),
                    child: const Text('Start'),
                  ),
              ],
            ),
            if (error != null)
              Text(
                error,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(color: scheme.error, fontSize: 12),
              ),
          ],
        ),
      ),
    );
  }
}
