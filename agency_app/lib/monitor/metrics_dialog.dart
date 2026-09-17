import 'package:flutter/material.dart';

import 'metric_meta.dart';

/// The per-metric visibility dialog (US3, FR-012): one checkbox per
/// [MetricKey] in display order, ported from the reference app's
/// `SettingsDialog` (research R1). Theme-adapted per research R2/R9: no
/// `google_fonts`, no hardcoded dark surface — text colors come from the
/// app theme, and each checkbox keeps its metric's fixed accent color
/// (the metric's visual identity, FR-008/SC-007).
class MetricsDialog extends StatefulWidget {
  const MetricsDialog({
    super.key,
    required this.visible,
    required this.onToggle,
  });

  /// The current per-metric visibility set, reflected in the checkboxes.
  final Map<MetricKey, bool> visible;

  /// Invoked with `(metric, newValue)` when a checkbox is toggled. The
  /// caller persists the choice (e.g. `ResourceMonitor.setMetricVisible`).
  final void Function(MetricKey, bool) onToggle;

  @override
  State<MetricsDialog> createState() => _MetricsDialogState();
}

class _MetricsDialogState extends State<MetricsDialog> {
  late final Map<MetricKey, bool> _visible = Map.of(widget.visible);

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return AlertDialog(
      title: Text(
        'Configure Monitors',
        style: TextStyle(
          fontWeight: FontWeight.bold,
          color: scheme.onSurface,
        ),
      ),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          for (final key in kMetricDisplayOrder)
            CheckboxListTile(
              title: Text(
                key.title,
                style: TextStyle(color: scheme.onSurfaceVariant),
              ),
              value: _visible[key] == true,
              activeColor: key.accent,
              checkColor: Colors.white,
              onChanged: (bool? value) {
                if (value == null) return;
                setState(() => _visible[key] = value);
                widget.onToggle(key, value);
              },
            ),
        ],
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: Text(
            'Close',
            style: TextStyle(color: scheme.primary),
          ),
        ),
      ],
    );
  }
}
