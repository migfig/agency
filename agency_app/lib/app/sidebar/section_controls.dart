import 'package:flutter/material.dart';

/// Shared chrome for the sidebar list sections (WORKFLOWS, HISTORY): a
/// tappable section header, a dense single-line filter field, and an empty
/// hint. Both sections use these so they share the collapse + filter
/// affordances as their lists grow.
class SidebarSectionHeader extends StatelessWidget {
  const SidebarSectionHeader({
    super.key,
    required this.title,
    required this.expanded,
    required this.onToggle,
    this.count,
  });

  final String title;

  /// Whether the section body is currently shown.
  final bool expanded;

  /// Invoked when the header is tapped.
  final VoidCallback onToggle;

  /// Optional count shown between the title and the chevron, e.g. `12`, or
  /// `3/12` while a filter is narrowing the list.
  final String? count;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Tooltip(
      message: expanded ? 'Collapse $title' : 'Expand $title',
      child: Container(
        // The themed band behind the title distinguishes the section header
        // from the (transparent) list items beneath it in both themes.
        color: scheme.surfaceContainer,
        child: InkWell(
          onTap: onToggle,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(16, 12, 12, 8),
            child: Row(
              children: [
                Expanded(
                  child: Text(
                    title,
                    style: TextStyle(
                      color: scheme.onSurfaceVariant,
                      fontSize: 11,
                      letterSpacing: 1.4,
                    ),
                  ),
                ),
                if (count != null)
                  Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 8),
                    child: Text(
                      count!,
                      style: TextStyle(
                        color: scheme.onSurfaceVariant,
                        fontSize: 11,
                      ),
                    ),
                  ),
                AnimatedRotation(
                  turns: expanded ? 0.5 : 0,
                  duration: const Duration(milliseconds: 150),
                  curve: Curves.easeInOut,
                  child: Icon(
                    Icons.expand_more,
                    size: 18,
                    color: scheme.onSurfaceVariant,
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// A dense single-line text filter bound through [onChanged] (an empty
/// string means unfiltered). Carries a search prefix icon, a clear button
/// while non-empty, and an optional [trailing] control (e.g. a state
/// filter menu). Owns its controller so the field keeps focus and cursor
/// position across the section's rebuilds.
class SidebarFilterField extends StatefulWidget {
  const SidebarFilterField({
    super.key,
    required this.hint,
    required this.onChanged,
    this.trailing,
  });

  final String hint;
  final ValueChanged<String> onChanged;
  final Widget? trailing;

  @override
  State<SidebarFilterField> createState() => _SidebarFilterFieldState();
}

class _SidebarFilterFieldState extends State<SidebarFilterField> {
  late final TextEditingController _controller = TextEditingController();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final hasText = _controller.text.isNotEmpty;
    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 0, 12, 6),
      child: Row(
        children: [
          Expanded(
            child: TextField(
              controller: _controller,
              onChanged: widget.onChanged,
              style: const TextStyle(fontSize: 12),
              decoration: InputDecoration(
                isDense: true,
                hintText: widget.hint,
                hintStyle: TextStyle(
                  color: scheme.onSurfaceVariant,
                  fontSize: 12,
                ),
                prefixIcon: Icon(
                  Icons.search,
                  size: 14,
                  color: scheme.onSurfaceVariant,
                ),
                suffixIcon: hasText
                    ? IconButton(
                        tooltip: 'Clear filter',
                        icon: const Icon(Icons.close, size: 14),
                        onPressed: () {
                          _controller.clear();
                          widget.onChanged('');
                        },
                      )
                    : null,
                contentPadding: const EdgeInsets.symmetric(vertical: 6),
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(6),
                  borderSide: BorderSide(color: scheme.outlineVariant),
                ),
              ),
            ),
          ),
          if (widget.trailing != null)
            Padding(
              padding: const EdgeInsets.only(left: 4),
              child: widget.trailing!,
            ),
        ],
      ),
    );
  }
}

/// A dimmed one-line hint for an empty section body (no items, or no
/// filter matches).
class SidebarEmptyHint extends StatelessWidget {
  const SidebarEmptyHint({super.key, required this.text});

  final String text;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      child: Text(
        text,
        style: TextStyle(color: scheme.onSurfaceVariant, fontSize: 12),
      ),
    );
  }
}
