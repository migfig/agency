import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'api/agency_client.dart';
import 'api/agency_events.dart';
import 'app/app_state.dart';
import 'app/home_page.dart';
import 'catalog/workflow_catalog.dart';
import 'monitor/resource_monitor.dart';
import 'theme/theme_data.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final app = AppState();
  await app.init();
  final prefs = await SharedPreferences.getInstance();
  final monitor = ResourceMonitor(prefs);
  monitor.start();
  final catalog = WorkflowCatalog(app.workflowsDir);
  final client = AgencyClient(app.serverAddress);
  final events = AgencyEvents(app.serverAddress);
  app.attachTransport(client, events);
  app.startMonitoring();
  runApp(AgencyApp(
    app: app,
    catalog: catalog,
    client: client,
    events: events,
    monitor: monitor,
  ));
}

class AgencyApp extends StatefulWidget {
  const AgencyApp({
    super.key,
    required this.app,
    required this.catalog,
    required this.client,
    required this.events,
    required this.monitor,
  });

  final AppState app;
  final WorkflowCatalog catalog;
  final AgencyClient client;
  final AgencyEvents events;
  final ResourceMonitor monitor;

  @override
  State<AgencyApp> createState() => _AgencyAppState();
}

class _AgencyAppState extends State<AgencyApp> {
  @override
  void initState() {
    super.initState();
    widget.app.addListener(_onThemeChanged);
    widget.catalog.addListener(_notify);
  }

  @override
  void dispose() {
    widget.app.removeListener(_onThemeChanged);
    widget.catalog.removeListener(_notify);
    widget.catalog.dispose();
    widget.events.close();
    widget.monitor.dispose();
    super.dispose();
  }

  void _onThemeChanged() {
    setState(() {});
  }

  void _notify() {
    setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Agency',
      debugShowCheckedModeBanner: false,
      theme: themeDataFor(widget.app.themeMode),
      home: ListenableBuilder(
        listenable: widget.catalog,
        builder: (context, child) {
          return HomePage(
            app: widget.app,
            client: widget.client,
            entries: widget.catalog.entries,
            eventStream: widget.events.frames,
            monitor: widget.monitor,
          );
        },
      ),
    );
  }
}
