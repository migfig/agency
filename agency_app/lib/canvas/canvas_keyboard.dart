import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

/// Screen-pixel step per arrow-key press (doubled while Shift is held).
const double kPanStep = 48;

/// Zoom factor per `+`/`=` press (`1/kZoomStep` per `-` press).
const double kZoomStep = 1.25;

/// The standard keyboard viewport gestures shared by the Abyss and classic
/// canvas views. Wire it to the view's `Focus(onKeyEvent:)` and return its
/// result so handled events stop propagating (unhandled keys still reach
/// sidebar scrolling and text fields untouched).
///
/// - Arrow keys: pan the scene in the arrow's direction by [kPanStep] screen
///   pixels (×2 while Shift is held), at the current scale.
/// - `=`/`+` key, numpad `+`: zoom in by [kZoomStep], anchored at the
///   viewport center; `-`, numpad `-`: zoom out.
/// - `0`, numpad `0`: reset the zoom to 100%.
/// - `f`: zoom to fit the scene.
///
/// Key-down and key-repeat events are both handled, so holding a key
/// repeats the gesture.
KeyEventResult handleViewportKeys(
  FocusNode node,
  KeyEvent event, {
  required void Function(Offset screenDelta) panBy,
  required void Function(double factor) zoomBy,
  required void Function() resetZoom,
  required void Function() zoomToFit,
}) {
  if (event is! KeyDownEvent && event is! KeyRepeatEvent) {
    return KeyEventResult.ignored;
  }
  final key = event.logicalKey;
  if (key == LogicalKeyboardKey.arrowUp ||
      key == LogicalKeyboardKey.arrowDown ||
      key == LogicalKeyboardKey.arrowLeft ||
      key == LogicalKeyboardKey.arrowRight) {
    final step = HardwareKeyboard.instance.isShiftPressed
        ? 2 * kPanStep
        : kPanStep;
    final d = switch (key) {
      LogicalKeyboardKey.arrowUp => Offset(0, -step),
      LogicalKeyboardKey.arrowDown => Offset(0, step),
      LogicalKeyboardKey.arrowLeft => Offset(-step, 0),
      LogicalKeyboardKey.arrowRight => Offset(step, 0),
      _ => Offset.zero,
    };
    panBy(d);
    return KeyEventResult.handled;
  }
  if (key == LogicalKeyboardKey.equal ||
      key == LogicalKeyboardKey.numpadAdd) {
    zoomBy(kZoomStep);
    return KeyEventResult.handled;
  }
  if (key == LogicalKeyboardKey.minus ||
      key == LogicalKeyboardKey.numpadSubtract) {
    zoomBy(1 / kZoomStep);
    return KeyEventResult.handled;
  }
  if (key == LogicalKeyboardKey.digit0 || key == LogicalKeyboardKey.numpad0) {
    resetZoom();
    return KeyEventResult.handled;
  }
  if (key == LogicalKeyboardKey.keyF) {
    zoomToFit();
    return KeyEventResult.handled;
  }
  return KeyEventResult.ignored;
}
