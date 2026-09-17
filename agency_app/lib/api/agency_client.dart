import 'dart:async';
import 'dart:convert';
import 'package:http/http.dart' as http;
import 'models.dart';

/// A network-level failure (DNS / connect / timeout). Never a crash.
class NetworkError implements Exception {
  const NetworkError(this.message);
  final String message;
  @override
  String toString() => 'NetworkError: $message';
}

/// A non-2xx response (or unparseable body) mapped to an [ErrorBody].
class ApiError implements Exception {
  const ApiError(this.body, this.statusCode);
  final ErrorBody body;
  final int statusCode;
  @override
  String toString() => 'ApiError($statusCode, ${body.code}): ${body.message}';
}

/// HTTP client for the Agency REST API.
class AgencyClient {
  AgencyClient(this.baseUrl, {http.Client? httpClient})
      : _http = httpClient ?? http.Client(),
        _timeout = const Duration(seconds: 10);

  /// The configured http(s) base address; retargeted by T047 without a
  /// client restart.
  String baseUrl;
  final http.Client _http;
  final Duration _timeout;

  Uri _uri(String path) => Uri.parse('$baseUrl$path');

  Future<HealthResponse> health() async {
    final body = await _request(() => _http.get(_uri('/health')).timeout(_timeout));
    return HealthResponse.fromJson(body);
  }

  /// [workflowPath] is resolved against the server host, so the caller must
  /// pass a path the server can see (absolute when the app and server share
  /// a machine, as in the default local setup).
  Future<RunAccepted> startRun(String workflowPath) async {
    final body = await _request(
      () => _http.post(_uri('/runs'), headers: _json, body: jsonEncode({'workflow': workflowPath})).timeout(_timeout),
    );
    return RunAccepted.fromJson(body);
  }

  Future<List<RunHistoryEntry>> listRuns() async {
    final body = await _request(() => _http.get(_uri('/runs')).timeout(_timeout));
    final list = (body as List<dynamic>?) ?? const <dynamic>[];
    return list.map((e) => RunHistoryEntry.fromJson((e as Map).cast<String, dynamic>())).toList();
  }

  Future<RunStatusView> runStatus(String runId) async {
    final body = await _request(() => _http.get(_uri('/runs/${_escape(runId)}')).timeout(_timeout));
    return RunStatusView.fromJson(body);
  }

  Future<RunAccepted> submitInput(String runId, String nodeId, String input) async {
    final path = '/runs/${_escape(runId)}/nodes/${_escape(nodeId)}/input';
    final body = await _request(
      () => _http.post(_uri(path), headers: _json, body: jsonEncode({'value': input})).timeout(_timeout),
    );
    return RunAccepted.fromJson(body);
  }

  Future<RunResultView> runResult(String runId) async {
    final body = await _request(() => _http.get(_uri('/runs/${_escape(runId)}/result')).timeout(_timeout));
    return RunResultView.fromJson(body);
  }

  /// Execute [send], parse success → decoded JSON; failure → typed error.
  Future<dynamic> _request(Future<http.Response> Function() send) async {
    http.Response response;
    try {
      response = await send();
    } on TimeoutException {
      throw const NetworkError('request timed out');
    } on http.ClientException catch (e) {
      throw NetworkError(e.message);
    } catch (e) {
      throw NetworkError('$e');
    }
    if (response.statusCode >= 200 && response.statusCode < 300) {
      try {
        return jsonDecode(response.body);
      } catch (_) {
        throw ApiError(
          const ErrorBody(code: ErrorCodes.invalidRequest, message: 'invalid response body'),
          response.statusCode,
        );
      }
    }
    throw ApiError(_errorBody(response), response.statusCode);
  }

  static final Map<String, String> _json = {'Content-Type': 'application/json'};

  String _escape(String s) => Uri.encodeComponent(s);

  ErrorBody _errorBody(http.Response r) {
    dynamic decoded;
    try {
      decoded = jsonDecode(r.body);
    } catch (_) {
      decoded = null;
    }
    final body = ErrorBody.parseResponse(decoded);
    if (body != null) return body;
    return const ErrorBody(code: ErrorCodes.invalidRequest, message: 'The server returned an error.');
  }
}
