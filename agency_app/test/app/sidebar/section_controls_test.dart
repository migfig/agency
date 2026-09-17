import 'package:agencyapp/app/sidebar/section_controls.dart';
import 'package:agencyapp/theme/theme_data.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// The shared sidebar section chrome (WORKFLOWS, HISTORY): a tappable
/// section header with an optional count, a dense single-line filter field
/// with a clear button and an optional trailing control, and a dimmed
/// empty hint.
void main() {
  Future<void> pump(WidgetTester tester, Widget child) =>
      tester.pumpWidget(MaterialApp(
        theme: themeDataFor(ThemeMode.dark),
        home: Scaffold(body: child),
      ));

  group('SidebarSectionHeader', () {
    testWidgets('renders the title and count, and tapping fires onToggle',
        (tester) async {
      var toggles = 0;
      await pump(
        tester,
        SidebarSectionHeader(
          title: 'WORKFLOWS',
          expanded: true,
          onToggle: () => toggles++,
          count: '3',
        ),
      );

      expect(find.text('WORKFLOWS'), findsOneWidget);
      expect(find.text('3'), findsOneWidget,
          reason: 'the count sits between the title and the chevron');
      expect(find.byTooltip('Collapse WORKFLOWS'), findsOneWidget,
          reason: 'the expanded header offers to collapse');

      await tester.tap(find.text('WORKFLOWS'));
      expect(toggles, 1, reason: 'tapping the header invokes the toggle');
    });

    testWidgets('collapsed state offers to expand; count may be omitted',
        (tester) async {
      await pump(
        tester,
        SidebarSectionHeader(
          title: 'HISTORY',
          expanded: false,
          onToggle: () {},
        ),
      );

      expect(find.text('HISTORY'), findsOneWidget);
      expect(find.byTooltip('Expand HISTORY'), findsOneWidget,
          reason: 'the collapsed header offers to expand');
    });

    testWidgets(
        'the title band carries the themed surface background (both modes)',
        (tester) async {
      for (final mode in [ThemeMode.dark, ThemeMode.light]) {
        await tester.pumpWidget(MaterialApp(
          theme: themeDataFor(mode),
          home: Scaffold(
            body: SidebarSectionHeader(
              title: 'WORKFLOWS',
              expanded: true,
              onToggle: () {},
            ),
          ),
        ));
        // Let MaterialApp's theme animation settle on the new theme.
        await tester.pump(const Duration(milliseconds: 500));
        final scheme = themeDataFor(mode).colorScheme;
        final container = tester.widget<Container>(find.descendant(
            of: find.byType(SidebarSectionHeader),
            matching: find.byType(Container)).first);
        expect(
          container.color,
          scheme.surfaceContainer,
          reason: '${mode.name}: the title band uses the theme palette',
        );
        expect(container.color, isNot(Colors.transparent),
            reason: '${mode.name}: the band is visually present');
      }
    });
  });

  group('SidebarFilterField', () {
    testWidgets(
        'shows its hint and search icon, reports changes, and clears on demand',
        (tester) async {
      final changes = <String>[];
      await pump(
        tester,
        FieldProbe(
          onChanged: (q) {
            changes.add(q);
          },
        ),
      );

      expect(find.text('Filter runs…'), findsOneWidget);
      expect(find.byIcon(Icons.search), findsOneWidget);
      expect(find.byIcon(Icons.close), findsNothing,
          reason: 'no clear button while the field is empty');

      await tester.enterText(find.byType(TextField), 'demo');
      expect(changes, ['demo']);
      await tester.pump();
      expect(find.byIcon(Icons.close), findsOneWidget,
          reason: 'a non-empty field offers a clear button');

      await tester.tap(find.byIcon(Icons.close));
      expect(changes, ['demo', ''],
          reason: 'clearing reports an empty query (unfiltered)');
      await tester.pump();
      expect(find.text('demo'), findsNothing,
          reason: 'the field is emptied');
    });

    testWidgets('renders the trailing control beside the field',
        (tester) async {
      await pump(
        tester,
        SidebarFilterField(
          hint: 'Filter runs…',
          onChanged: (_) {},
          trailing: const Icon(Icons.filter_list),
        ),
      );

      expect(find.byType(TextField), findsOneWidget);
      expect(find.byIcon(Icons.filter_list), findsOneWidget);
    });
  });

  group('SidebarEmptyHint', () {
    testWidgets('renders the hint text', (tester) async {
      await pump(tester, const SidebarEmptyHint(text: 'No matches.'));
      expect(find.text('No matches.'), findsOneWidget);
    });
  });
}

/// Mirrors the sections' real usage: the parent keeps the query in state and
/// rebuilds on change, which is what re-runs [SidebarFilterField]'s build and
/// reveals the clear button.
class FieldProbe extends StatefulWidget {
  const FieldProbe({super.key, required this.onChanged});

  final ValueChanged<String> onChanged;

  @override
  State<FieldProbe> createState() => _FieldProbeState();
}

class _FieldProbeState extends State<FieldProbe> {
  @override
  Widget build(BuildContext context) => SidebarFilterField(
        hint: 'Filter runs…',
        onChanged: (q) {
          widget.onChanged(q);
          setState(() {});
        },
      );
}
