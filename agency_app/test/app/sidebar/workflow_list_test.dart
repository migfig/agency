import 'package:agencyapp/app/sidebar/workflow_list.dart';
import 'package:agencyapp/catalog/catalog_entry.dart';
import 'package:agencyapp/theme/theme_data.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// The WORKFLOWS sidebar section (US1): the live catalog, each readable row
/// with a Start action. The section is collapsible from its header and
/// filters rows by name or path as the catalog grows, with a live count in
/// the header (`total`, or `matched/total` while filtering).
void main() {
  CatalogEntry entry(String name, String path) =>
      CatalogEntry(path: path, name: name);

  final entries = [
    entry('draft', 'flows/alpha.yaml'),
    entry('review', 'flows/beta.yaml'),
    entry('publish', 'flows/gamma.yaml'),
  ];

  Future<void> pump(
    WidgetTester tester,
    List<CatalogEntry> entries, {
    List<CatalogEntry>? started,
  }) =>
      tester.pumpWidget(MaterialApp(
        theme: themeDataFor(ThemeMode.dark),
        home: Scaffold(
          body: WorkflowList(
            entries: entries,
            startingPaths: const {},
            startErrors: const {},
            onStart: (e) => started?.add(e),
          ),
        ),
      ));

  group('workflow list section', () {
    testWidgets('lists every entry expanded with a total count',
        (tester) async {
      await pump(tester, entries);

      expect(find.text('WORKFLOWS'), findsOneWidget);
      expect(find.text('3'), findsOneWidget,
          reason: 'the header counts the entries');
      for (final name in ['draft', 'review', 'publish']) {
        expect(find.text(name), findsOneWidget);
      }
      expect(find.text('Start'), findsNWidgets(3));
    });

    testWidgets('collapses on header tap and re-expands on the next tap',
        (tester) async {
      await pump(tester, entries);

      await tester.tap(find.text('WORKFLOWS'));
      await tester.pumpAndSettle();

      expect(find.text('Start'), findsNothing,
          reason: 'rows hide with the section body');
      expect(find.byType(TextField), findsNothing,
          reason: 'the filter field hides with the section body');
      expect(find.text('WORKFLOWS'), findsOneWidget,
          reason: 'the header stays visible to re-expand');
      expect(find.byTooltip('Expand WORKFLOWS'), findsOneWidget);

      await tester.tap(find.text('WORKFLOWS'));
      await tester.pumpAndSettle();

      expect(find.text('Start'), findsNWidgets(3),
          reason: 're-expanding restores every row');
      expect(find.byType(TextField), findsOneWidget);
      expect(find.byTooltip('Collapse WORKFLOWS'), findsOneWidget);
    });

    testWidgets('filters by name, case-insensitively, with a matched/total count',
        (tester) async {
      await pump(tester, entries);

      await tester.enterText(find.byType(TextField), 'REVIEW');
      await tester.pump();

      expect(find.text('review'), findsOneWidget);
      expect(find.text('draft'), findsNothing);
      expect(find.text('publish'), findsNothing);
      expect(find.text('1/3'), findsOneWidget,
          reason: 'the count shows matches over total while filtering');
    });

    testWidgets('filters by path when the name does not match',
        (tester) async {
      await pump(tester, entries);

      await tester.enterText(find.byType(TextField), 'beta.yaml');
      await tester.pump();

      expect(find.text('review'), findsOneWidget,
          reason: 'the query matches the row path');
      expect(find.text('draft'), findsNothing);
      expect(find.text('1/3'), findsOneWidget);
    });

    testWidgets('shows the no-match hint when nothing matches',
        (tester) async {
      await pump(tester, entries);

      await tester.enterText(find.byType(TextField), 'zzz');
      await tester.pump();

      expect(find.text('No matches.'), findsOneWidget);
      expect(find.text('Start'), findsNothing);
      expect(find.text('0/3'), findsOneWidget);
    });

    testWidgets('the clear button restores the unfiltered list',
        (tester) async {
      await pump(tester, entries);

      await tester.enterText(find.byType(TextField), 'draft');
      await tester.pump();
      expect(find.text('1/3'), findsOneWidget);

      await tester.tap(find.byIcon(Icons.close));
      await tester.pump();

      expect(find.text('Start'), findsNWidgets(3),
          reason: 'clearing the query restores every row');
      expect(find.text('3'), findsOneWidget);
    });

    testWidgets('an empty catalog shows its own hint', (tester) async {
      await pump(tester, const []);

      expect(find.text('No workflows found.'), findsOneWidget);
      expect(find.text('0'), findsOneWidget,
          reason: 'the header counts an empty catalog');
    });

    testWidgets('start still fires on a visible row; unreadable rows stay without Start',
        (tester) async {
      final broken = CatalogEntry(
        path: 'flows/broken.yaml',
        name: 'broken',
        parseError: 'bad yaml',
      );
      final started = <CatalogEntry>[];
      await pump(tester, [entries.first, broken], started: started);

      expect(find.text('Start'), findsOneWidget,
          reason: 'unreadable rows offer no Start action');

      await tester.tap(find.text('Start'));
      expect(started, [entries.first],
          reason: 'Start still fires with the catalog entry');
    });
  });
}
