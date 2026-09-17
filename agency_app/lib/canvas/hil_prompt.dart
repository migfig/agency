import 'package:flutter/material.dart';

import '../api/models.dart';

/// The human-in-the-loop prompt card (FR-017, research R12).
///
/// Driven entirely by [PendingInputView]: the waiting node id, the question
/// text, a deadline countdown when present, a text field, and a **Submit**
/// affordance. The card never talks to the network; [onSubmit] is called with
/// the trimmed answer and the parent owns the submit + resync flow.
class HilPromptCard extends StatefulWidget {
  const HilPromptCard({
    super.key,
    required this.prompt,
    this.note,
    this.submitting = false,
    required this.onSubmit,
  });

  final PendingInputView prompt;
  final String? note;
  final bool submitting;
  final void Function(String text) onSubmit;

  @override
  State<HilPromptCard> createState() => _HilPromptCardState();
}

class _HilPromptCardState extends State<HilPromptCard> {
  final TextEditingController _controller = TextEditingController();

  @override
  void initState() {
    super.initState();
    // Rebuild on keystrokes so the Submit button's enabled state tracks the
    // entered text (the button's onPressed is computed in build).
    _controller.addListener(_onTextChanged);
  }

  void _onTextChanged() {
    if (mounted) setState(() {});
  }

  @override
  void dispose() {
    _controller.removeListener(_onTextChanged);
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final text = _controller.text.trim();
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              widget.prompt.nodeId,
              style: const TextStyle(
                fontWeight: FontWeight.w600,
                fontSize: 16,
              ),
            ),
            const SizedBox(height: 4),
            Text(
              widget.prompt.prompt,
              style: const TextStyle(fontSize: 14),
            ),
            if (widget.prompt.deadline != null) ...[
              const SizedBox(height: 4),
              _deadlineLine(widget.prompt.deadline!, scheme),
            ],
            if (widget.note != null) ...[
              const SizedBox(height: 8),
              Text(
                widget.note!,
                style: TextStyle(color: scheme.error, fontSize: 13),
              ),
            ],
            const SizedBox(height: 8),
            TextField(
              controller: _controller,
              minLines: 1,
              maxLines: null,
              decoration: const InputDecoration(
                hintText: 'Answer…',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 8),
            Align(
              alignment: Alignment.centerRight,
              child: FilledButton(
                onPressed:
                    widget.submitting || text.isEmpty ? null : () => widget.onSubmit(text),
                child: Text(widget.submitting ? 'submitting…' : 'Submit'),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _deadlineLine(DateTime deadline, ColorScheme scheme) {
    final remaining = deadline.difference(DateTime.now());
    if (remaining.isNegative) {
      return Text(
        'deadline passed',
        style: TextStyle(color: scheme.error, fontSize: 12),
      );
    }
    final minutes = remaining.inMinutes;
    final seconds = remaining.inSeconds % 60;
    return Text(
      'deadline in ${minutes}m ${seconds}s',
      style: TextStyle(color: scheme.primary, fontSize: 12),
    );
  }
}
