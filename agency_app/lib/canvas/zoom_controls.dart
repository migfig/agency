import 'package:flutter/material.dart';

/// The on-canvas zoom controls: a compact `- 100% + fit` bar pinned
/// bottom-right in screen space (unaffected by pan/zoom). Shared by the
/// Abyss and classic canvas views; styled like the on-canvas panels
/// (legend). Taps never steal keyboard focus, so the view's viewport
/// key handling keeps working after clicking a control.
class ZoomControls extends StatelessWidget {
  const ZoomControls({
    super.key,
    required this.surface,
    required this.text,
    required this.scale,
    required this.onZoomIn,
    required this.onZoomOut,
    required this.onReset,
    required this.onFit,
  });

  /// The panel surface color (tinted to 0.92 alpha, as in the legend).
  final Color surface;

  /// The text/ink color used for icons, border, and labels.
  final Color text;

  /// The current view scale (shown as a rounded percentage).
  final double scale;

  final VoidCallback onZoomIn;
  final VoidCallback onZoomOut;
  final VoidCallback onReset;
  final VoidCallback onFit;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 2, vertical: 2),
      decoration: BoxDecoration(
        color: surface.withValues(alpha: 0.92),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: text.withValues(alpha: 0.25)),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          _icon(Icons.remove, 'Zoom out (−)', text, onZoomOut),
          Tooltip(
            message: 'Reset to 100% (0)',
            child: GestureDetector(
              behavior: HitTestBehavior.opaque,
              onTap: onReset,
              child: SizedBox(
                width: 44,
                child: Padding(
                  padding: const EdgeInsets.symmetric(vertical: 8),
                  child: Center(
                    child: Text(
                      '${(scale * 100).round()}%',
                      style: TextStyle(fontSize: 12, color: text),
                    ),
                  ),
                ),
              ),
            ),
          ),
          _icon(Icons.add, 'Zoom in (+)', text, onZoomIn),
          _icon(Icons.fit_screen, 'Zoom to fit (F)', text, onFit),
        ],
      ),
    );
  }

  Widget _icon(IconData icon, String tooltip, Color text, VoidCallback onTap) {
    return Tooltip(
      message: tooltip,
      child: GestureDetector(
        behavior: HitTestBehavior.opaque,
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.all(6),
          child: Icon(icon, size: 16, color: text.withValues(alpha: 0.85)),
        ),
      ),
    );
  }
}
