/// The flow orientation of the classic canvas: how the deterministic
/// `computeLayout` (col, row) grid is mapped onto world px. The 200×72 cards
/// and the painter are untouched by a topology — only the card positions and
/// edge anchors change, so the grid's flow axis (Kahn depth, `col`) can be
/// aligned with the screen's longer dimension.
enum CanvasTopology { leftRight, topDown, rightLeft, bottomUp }

/// The top-bar menu label for a [CanvasTopology].
String canvasTopologyLabel(CanvasTopology t) => switch (t) {
      CanvasTopology.leftRight => 'Left → right',
      CanvasTopology.topDown => 'Top ↓ down',
      CanvasTopology.rightLeft => 'Right ← left',
      CanvasTopology.bottomUp => 'Bottom ↑ up',
    };
