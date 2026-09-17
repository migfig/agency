import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';

import 'metric_meta.dart';
import 'metrics_dialog.dart';
import 'monitor_card.dart';
import 'resource_monitor.dart';
import 'system_metrics.dart';

/// The in-app resource monitor panel: a themed column of [MonitorCard]s, one
/// per enabled metric in display order, fed by a [ResourceMonitor]
/// (contracts/ui.md "Panel"/"Cards").
///
/// The panel hosts its own `Positioned(top: 8, right: 8, bottom: 8,
/// width: 250)` — the region only sets the bounds; the visible [Card] is
/// top-aligned and sizes to its content (the header row plus the visible
/// cards, or just the header row when collapsed — the ClassicLegend
/// pattern). If the canvas area is shorter than all the cards, the body
/// scrolls inside the capped region instead of overflowing it. The header
/// carries the per-metric visibility
/// control (gear → [MetricsDialog], FR-012) and the collapse/expand
/// control (`Icons.expand_less`/`expand_more`, the classic-legend pattern,
/// FR-005/SC-003). The widget holds **no state**: the per-session
/// `collapsed` flag lives in the [ResourceMonitor] (never persisted), so
/// a topbar hide/show that unmounts the panel cannot reset it.
class ResourceMonitorPanel extends StatelessWidget {
  const ResourceMonitorPanel({super.key, required this.monitor});

  final ResourceMonitor monitor;

  /// Opens the per-metric visibility dialog (FR-012, US3). The dialog is
  /// fed the controller's `visible` set and drives `setMetricVisible`, so
  /// the panel body reflows through the controller's notification.
  void _showMetricsDialog(BuildContext context) {
    showDialog<void>(
      context: context,
      builder: (context) => MetricsDialog(
        visible: monitor.visible,
        onToggle: monitor.setMetricVisible,
      ),
    );
  }

  /// One card row for a metric: a 150px [MonitorCard] with an 8px gap
  /// below (the row is the unit the panel body sums to size itself).
  Widget _card(MetricKey key, SystemMetrics current) {
    final history = monitor.histories[key]!;
    final start = monitor.sampleOrdinal - history.length;
    return SizedBox(
      height: 150,
      child: Padding(
        padding: const EdgeInsets.only(bottom: 8),
        child: MonitorCard(
          key: ValueKey(key),
          title: key.title,
          value: key.formatValue(current),
          subValue: key.subValue(current),
          unit: key.unitFor(current),
          spots: [
            for (var i = 0; i < history.length; i++)
              FlSpot((start + i).toDouble(), history[i]),
          ],
          color: key.accent,
          maxY: key.maxY(history),
        ),
      ),
    );
  }

  /// The card body: the "waiting" placeholder before the first sample, or a
  /// column of [MonitorCard]s for the enabled metrics, so the panel's
  /// height is the sum of its card rows plus the panel padding. The rows
  /// sit in a scroll view that is invisible while they fit (the viewport
  /// sizes to its child) and only scrolls if the canvas area is too short
  /// for all the rows — the panel never overflows the area.
  Widget _body(ColorScheme scheme) {
    final current = monitor.current;
    if (current == null) {
      return Center(
        child: Text(
          'waiting for first sample…',
          style: TextStyle(
            fontSize: 12,
            color: scheme.onSurfaceVariant,
          ),
        ),
      );
    }
    final enabled = kMetricDisplayOrder
        .where((k) => monitor.visible[k] == true)
        .toList();
    return SingleChildScrollView(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          for (final key in enabled) _card(key, current),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    // The Positioned region (top 8 / bottom 8 / width 250) only bounds the
    // panel; the [Align] keeps the [Card] content-sized and top-aligned, so
    // the visible panel is exactly the header plus its cards — and clamps
    // to the region (body scrolling) when the area is too short.
    return Positioned(
      top: 8,
      right: 8,
      bottom: 8,
      width: 250,
      child: Align(
        alignment: Alignment.topRight,
        child: Card(
          elevation: 0,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(12),
            side: BorderSide(
              color: scheme.onSurface.withValues(alpha: 0.25),
            ),
          ),
          color: Theme.of(context).cardTheme.color ??
              scheme.surfaceContainerHighest,
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: ListenableBuilder(
              listenable: monitor,
              builder: (context, _) {
                final collapsed = monitor.collapsed;
                return Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Expanded(
                          child: Text(
                            'Resource Monitor',
                            style: TextStyle(
                              fontSize: 16,
                              fontWeight: FontWeight.w600,
                              color: scheme.onSurface,
                            ),
                          ),
                        ),
                        // Per-metric visibility (FR-012): the same manner as
                        // the reference app's settings dialog, theme-adapted.
                        IconButton(
                          tooltip: 'Configure metrics',
                          icon: const Icon(Icons.settings, size: 18),
                          onPressed: () => _showMetricsDialog(context),
                        ),
                        // Collapse/expand (FR-005/SC-003), the ClassicLegend
                        // pattern: the flag lives in the controller, so the
                        // panel stays stateless and a topbar hide/show
                        // (widget unmount) cannot reset it.
                        IconButton(
                          tooltip: collapsed
                              ? 'Expand resource monitor'
                              : 'Collapse resource monitor',
                          icon: Icon(
                            collapsed ? Icons.expand_more : Icons.expand_less,
                            size: 18,
                          ),
                          onPressed: () =>
                              monitor.setCollapsed(!collapsed),
                        ),
                      ],
                    ),
                    if (!collapsed) ...[
                      const SizedBox(height: 12),
                      // Flexible (loose) so the body takes the column's
                      // remainder only when it does not fit — while it fits,
                      // the scroll view stays content-sized.
                      Flexible(child: _body(scheme)),
                    ],
                  ],
                );
              },
            ),
          ),
        ),
      ),
    );
  }
}
