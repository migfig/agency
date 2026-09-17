import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';

/// One theme-aware metric card: title row (accent dot + title), a large
/// value + unit, an optional sub-value, and a [LineChart] trend window
/// (research R2/R8/R9). The card is the unit of per-metric visibility
/// (FR-014) and is theme-agnostic: all colors come from the app theme
/// (FR-011).
class MonitorCard extends StatelessWidget {
  const MonitorCard({
    super.key,
    required this.title,
    required this.value,
    this.subValue,
    required this.unit,
    required this.spots,
    required this.color,
    this.maxY = 100,
  });

  final String title;
  final String value;
  final String? subValue;
  final String unit;
  final List<FlSpot> spots;
  final Color color;
  final double maxY;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final cardColor = Theme.of(context).cardTheme.color ??
        scheme.surfaceContainerHighest;

    return Card(
      elevation: 0,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: BorderSide(
          color: scheme.onSurface.withValues(alpha: 0.25),
        ),
      ),
      color: cardColor,
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(Icons.circle, size: 8, color: color),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    title,
                    style: TextStyle(
                      fontSize: 14,
                      color: scheme.onSurfaceVariant,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 8),
            Row(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                Text(
                  value,
                  style: TextStyle(
                    fontSize: 24,
                    color: scheme.onSurface,
                    fontWeight: FontWeight.bold,
                  ),
                ),
                const SizedBox(width: 4),
                Text(
                  unit,
                  style: TextStyle(
                    fontSize: 14,
                    color: scheme.onSurfaceVariant,
                  ),
                ),
              ],
            ),
            if (subValue != null) ...[
              const SizedBox(height: 4),
              Text(
                subValue!,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  fontSize: 12,
                  color: scheme.onSurfaceVariant.withValues(alpha: 0.8),
                ),
              ),
            ],
            const SizedBox(height: 12),
            Expanded(
              child: LineChart(
                duration: Duration.zero,
                LineChartData(
                  minX: spots.isEmpty ? 0 : spots.first.x,
                  maxX: spots.isEmpty
                      ? 1
                      : (spots.last.x > spots.first.x
                          ? spots.last.x
                          : spots.first.x),
                  maxY: maxY,
                  lineTouchData: const LineTouchData(enabled: false),
                  gridData: FlGridData(show: false),
                  titlesData: FlTitlesData(
                    show: false,
                  ),
                  borderData: FlBorderData(show: false),
                  lineBarsData: [
                    LineChartBarData(
                      spots: spots,
                      color: color,
                      isCurved: true,
                      barWidth: 2,
                      isStrokeCapRound: true,
                      dotData: const FlDotData(show: false),
                      belowBarData: BarAreaData(
                        show: true,
                        // fl_chart 1.2.0 throws when both `color` and
                        // `gradient` are set, so only the gradient is used.
                        gradient: LinearGradient(
                          begin: Alignment.topCenter,
                          end: Alignment.bottomCenter,
                          colors: [
                            color.withValues(alpha: 0.3),
                            color.withValues(alpha: 0.0),
                          ],
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
