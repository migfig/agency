import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'metric_meta.dart';
import 'system_metrics.dart';
import 'system_service.dart';

/// Per-metric visibility pref keys (FR-013).
const Map<MetricKey, String> _prefKeyFor = {
  MetricKey.cpu: 'monitor_metric_cpu',
  MetricKey.ram: 'monitor_metric_ram',
  MetricKey.gpu: 'monitor_metric_gpu',
  MetricKey.netDown: 'monitor_metric_net_down',
  MetricKey.netUp: 'monitor_metric_net_up',
};

/// First-launch visibility: cpu/ram/gpu on, the two network metrics off
/// (FR-014).
const Map<MetricKey, bool> _defaultVisible = {
  MetricKey.cpu: true,
  MetricKey.ram: true,
  MetricKey.gpu: true,
  MetricKey.netDown: false,
  MetricKey.netUp: false,
};

/// In-app state for the resource monitor: the [SystemService] sampler plus
/// the app-level state the panel renders from (data-model.md §3).
///
/// [prefs] is passed synchronously (the caller awaits
/// `SharedPreferences.getInstance()` first — main.dart does it after
/// `app.init()`), and [service] is injectable for tests.
class ResourceMonitor extends ChangeNotifier {
  ResourceMonitor(this._prefs, {SystemService? service})
      : _service = service ?? SystemService(),
        _visible = {
          for (final entry in _defaultVisible.entries)
            entry.key:
                _prefs.getBool(_prefKeyFor[entry.key]!) ?? entry.value,
        };

  final SharedPreferences _prefs;
  final SystemService _service;
  final Map<MetricKey, bool> _visible;

  /// The most recent sample, or null until the first one arrives.
  SystemMetrics? current;

  /// Per-metric trend windows, capped at 60 samples (SC-006).
  final Map<MetricKey, List<double>> histories = {
    for (final key in MetricKey.values) key: <double>[],
  };

  /// Monotonic sample counter; the x-axis of every trend window.
  int sampleOrdinal = 0;

  /// Panel collapsed flag — per-session only, never persisted.
  bool collapsed = false;

  StreamSubscription<SystemMetrics>? _sub;

  SystemService get service => _service;

  /// True when the latest sample reports a GPU with memory (FR-018).
  bool get hasGpu => (current?.gpuMemoryHas ?? 0.0) > 0.0;

  List<MetricKey> get enabledKeys =>
      MetricKey.values.where((k) => _visible[k] == true).toList();

  Map<MetricKey, bool> get visible => Map.unmodifiable(_visible);

  /// Subscribes to the sampler and starts its 1 Hz production timer.
  void start() {
    _sub ??= _service.metricsStream.listen(recordSample);
    _service.startMonitoring();
  }

  /// Records one sample: updates [current], appends to each metric's
  /// [histories] window (capped at 60), advances [sampleOrdinal], notifies.
  void recordSample(SystemMetrics m) {
    current = m;
    sampleOrdinal += 1;
    for (final key in MetricKey.values) {
      final h = histories[key]!;
      h.add(key.trendValue(m));
      if (h.length > 60) {
        h.removeRange(0, h.length - 60);
      }
    }
    notifyListeners();
  }

  void setCollapsed(bool value) {
    if (collapsed == value) return;
    collapsed = value;
    notifyListeners();
  }

  /// Updates [visible], persists the `monitor_metric_*` bool, notifies.
  void setMetricVisible(MetricKey key, bool visible) {
    _visible[key] = visible;
    _prefs.setBool(_prefKeyFor[key]!, visible);
    notifyListeners();
  }

  @override
  void dispose() {
    _sub?.cancel();
    _sub = null;
    _service.dispose();
    super.dispose();
  }
}
