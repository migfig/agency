import 'package:flutter/material.dart';

import 'system_metrics.dart';

/// The five sampled metrics. Enum declaration order is NOT the card display
/// order — see [kMetricDisplayOrder].
enum MetricKey {
  cpu,
  ram,
  gpu,
  netDown,
  netUp;

  /// Card title (the reference app's title).
  String get title => switch (this) {
        MetricKey.cpu => 'CPU Usage',
        MetricKey.ram => 'RAM Usage',
        MetricKey.gpu => 'GPU Usage',
        MetricKey.netDown => 'Network Down',
        MetricKey.netUp => 'Network Up',
      };

  /// Fixed per-metric accent (the metric's visual identity, kept in both
  /// themes).
  Color get accent => switch (this) {
        MetricKey.cpu => Colors.blueAccent,
        MetricKey.ram => Colors.purpleAccent,
        MetricKey.gpu => Colors.greenAccent,
        MetricKey.netDown => Colors.orangeAccent,
        MetricKey.netUp => Colors.redAccent,
      };

  /// Large value text, `%.1f`; network scales KB/s → MB/s above 1024.
  String formatValue(SystemMetrics m) => switch (this) {
        MetricKey.cpu => m.cpuUsage.toStringAsFixed(1),
        MetricKey.ram => m.ramUsage.toStringAsFixed(1),
        MetricKey.gpu => m.gpuUsage.toStringAsFixed(1),
        MetricKey.netDown => _netValue(m.networkIn),
        MetricKey.netUp => _netValue(m.networkOut),
      };

  /// Unit beside the value; network scales between `KB/s` and `MB/s` at 1024.
  String unitFor(SystemMetrics m) => switch (this) {
        MetricKey.cpu => '%',
        MetricKey.ram => '%',
        MetricKey.gpu => '%',
        MetricKey.netDown => m.networkIn > 1024 ? 'MB/s' : 'KB/s',
        MetricKey.netUp => m.networkOut > 1024 ? 'MB/s' : 'KB/s',
      };

  /// Small detail under the value; null when the metric has none.
  String? subValue(SystemMetrics m) => switch (this) {
        MetricKey.cpu => null,
        MetricKey.ram =>
            '${m.ramUsed.toStringAsFixed(1)}GB / ${m.ramTotal.toStringAsFixed(1)}GB',
        MetricKey.gpu =>
            '${m.gpuMemoryUsed.toStringAsFixed(1)}GB / ${m.gpuMemoryHas.toStringAsFixed(1)}GB • ${m.gpuTemp.toStringAsFixed(0)}°C',
        MetricKey.netDown => null,
        MetricKey.netUp => null,
      };

  /// Value appended to the metric's rolling history on each sample.
  double trendValue(SystemMetrics m) => switch (this) {
        MetricKey.cpu => m.cpuUsage,
        MetricKey.ram => m.ramUsage,
        MetricKey.gpu => m.gpuUsage,
        MetricKey.netDown => m.networkIn,
        MetricKey.netUp => m.networkOut,
      };

  /// Chart `maxY`: fixed 100 for percent metrics; `max(window) * 1.2 + 10`
  /// for network (ported).
  double maxY(List<double> window) => switch (this) {
        MetricKey.cpu => 100,
        MetricKey.ram => 100,
        MetricKey.gpu => 100,
        MetricKey.netDown => _netMaxY(window),
        MetricKey.netUp => _netMaxY(window),
      };
}

/// Card order in the panel body — the reference app's alphabetical title
/// sort: CPU Usage, GPU Usage, Network Down, Network Up, RAM Usage.
const List<MetricKey> kMetricDisplayOrder = [
  MetricKey.cpu,
  MetricKey.gpu,
  MetricKey.netDown,
  MetricKey.netUp,
  MetricKey.ram,
];

String _netValue(double kbPerSec) =>
    kbPerSec > 1024 ? (kbPerSec / 1024).toStringAsFixed(1) : kbPerSec.toStringAsFixed(1);

double _netMaxY(List<double> window) =>
    window.fold(0.0, (p, c) => c > p ? c : p) * 1.2 + 10;
