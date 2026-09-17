import 'package:flutter/material.dart';

import '../../api/models.dart';
import '../../catalog/workflow_def.dart';
import 'classic_encodings.dart';
import 'classic_palette.dart';

/// The glyph/motion description shown beside each state's encoding sample
/// (the non-color channel, FR-009).
const Map<ClassicMarker, String> kMarkerDescriptions = {
  ClassicMarker.none: 'static',
  ClassicMarker.pulse: 'pulsing border',
  ClassicMarker.countdown: 'retry countdown',
  ClassicMarker.check: 'check glyph',
  ClassicMarker.checkFb: 'check + fb chip',
  ClassicMarker.cross: 'cross glyph',
  ClassicMarker.dim: 'dimmed, no motion',
};

/// The on-canvas classic legend (FR-007): an on-canvas panel pinned
/// top-left in screen space (unaffected by pan/zoom), default visible,
/// collapsible, listing the six type markers and the seven state encodings
/// from the T004 tables, tinted from the theme's [ClassicPalette].
class ClassicLegend extends StatefulWidget {
  const ClassicLegend({
    super.key,
    required this.palette,
    this.initiallyOpen = true,
  });

  final ClassicPalette palette;
  final bool initiallyOpen;

  @override
  State<ClassicLegend> createState() => _ClassicLegendState();
}

class _ClassicLegendState extends State<ClassicLegend> {
  late bool _open = widget.initiallyOpen;

  @override
  Widget build(BuildContext context) {
    final p = widget.palette;
    final text = p.text;
    return Container(
      width: 232,
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
      decoration: BoxDecoration(
        color: p.surface.withValues(alpha: 0.92),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: text.withValues(alpha: 0.25)),
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Text(
                'Legend',
                style: TextStyle(
                  fontSize: 12,
                  fontWeight: FontWeight.w600,
                  color: text,
                ),
              ),
              const Spacer(),
              IconButton(
                tooltip: _open ? 'Collapse legend' : 'Expand legend',
                icon: Icon(
                  _open ? Icons.expand_less : Icons.expand_more,
                  size: 14,
                  color: text.withValues(alpha: 0.7),
                ),
                onPressed: () => setState(() => _open = !_open),
              ),
            ],
          ),
          if (_open) ...[
            const SizedBox(height: 4),
            Text(
              'Types',
              style: TextStyle(
                fontSize: 11,
                color: text.withValues(alpha: 0.6),
              ),
            ),
            const SizedBox(height: 2),
            for (final t in NodeType.values)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 1),
                child: Row(
                  children: [
                    Icon(
                      typeMarkerFor(t).icon,
                      size: 12,
                      color: text.withValues(alpha: 0.85),
                    ),
                    const SizedBox(width: 6),
                    Text(
                      typeMarkerFor(t).name,
                      style: TextStyle(fontSize: 11, color: text),
                    ),
                  ],
                ),
              ),
            const SizedBox(height: 6),
            Text(
              'States',
              style: TextStyle(
                fontSize: 11,
                color: text.withValues(alpha: 0.6),
              ),
            ),
            const SizedBox(height: 2),
            for (final s in NodeStatus.values)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 1),
                child: Row(
                  children: [
                    Container(
                      key: ValueKey('state-sample-${s.wire}'),
                      width: 14,
                      height: 14,
                      decoration: BoxDecoration(
                        color: p.cardFill,
                        borderRadius: BorderRadius.circular(3),
                        border: Border.all(color: p.colorFor(s), width: 2),
                      ),
                    ),
                    const SizedBox(width: 6),
                    Text(
                      kClassicStateEncodings[s]!.label,
                      style: TextStyle(fontSize: 11, color: text),
                    ),
                    const SizedBox(width: 6),
                    Expanded(
                      child: Text(
                        kMarkerDescriptions[kClassicStateEncodings[s]!.marker]!,
                        style: TextStyle(
                          fontSize: 10,
                          color: text.withValues(alpha: 0.6),
                        ),
                      ),
                    ),
                  ],
                ),
              ),
          ],
        ],
      ),
    );
  }
}
