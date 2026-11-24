import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:url_launcher/url_launcher.dart';

/// Matches the backend's RFC 9457 error body: {"code": "...", "detail": "..."}.
class ApiException implements Exception {
  ApiException(this.status, this.code, this.message);
  final int status;
  final String code;
  final String message;
  @override
  String toString() => 'ApiException($status, $code, $message)';
}

/// Minimal client for the endpoints the AR + AI flow needs (Parts 1–2 backend).
/// Token refresh/rotation lives in the auth layer and plugs in via [tokenProvider].
class Api {
  Api._();
  static final instance = Api._();

  String baseUrl = const String.fromEnvironment('API_URL', defaultValue: 'https://api.arwallpaint.app/v1');
  Future<String?> Function() tokenProvider = () async => null;
  final _http = http.Client();

  Future<dynamic> _send(String method, String path, {Object? body, Map<String, String>? headers}) async {
    final token = await tokenProvider();
    final req = http.Request(method, Uri.parse('$baseUrl$path'))
      ..headers.addAll({
        'Content-Type': 'application/json',
        if (token != null) 'Authorization': 'Bearer $token',
        ...?headers,
      });
    if (body != null) req.body = jsonEncode(body);
    final res = await http.Response.fromStream(await _http.send(req).timeout(const Duration(seconds: 30)));
    final json = res.body.isEmpty ? null : jsonDecode(res.body);
    if (res.statusCode >= 400) {
      final err = json is Map ? json : const {};
      throw ApiException(res.statusCode, err['code'] as String? ?? 'error', err['detail'] as String? ?? 'Something went wrong');
    }
    return json;
  }

  /// Starts an AI room analysis and waits for it (backend → SQS → GPU worker → callback).
  /// Idempotency key: a flaky mobile retry never charges the user's quota twice.
  Future<Map<String, dynamic>> recommendPalettes(String projectId, String assetId,
      {String? mood, List<String> styles = const []}) async {
    final key = '$projectId-$assetId-${DateTime.now().millisecondsSinceEpoch ~/ 60000}';
    var job = await _send('POST', '/projects/$projectId/ai/recommend-palettes',
        body: {'asset_id': assetId, 'mood': mood, 'style_preferences': styles},
        headers: {'Idempotency-Key': key}) as Map<String, dynamic>;
    final deadline = DateTime.now().add(const Duration(seconds: 90));
    var delay = const Duration(milliseconds: 800);
    while (job['status'] == 'queued' || job['status'] == 'running') {
      if (DateTime.now().isAfter(deadline)) throw ApiException(504, 'timeout', 'Analysis is taking longer than usual');
      await Future.delayed(delay);
      delay = Duration(milliseconds: (delay.inMilliseconds * 1.4).clamp(800, 4000).round());
      job = await _send('GET', '/ai/jobs/${job['id']}') as Map<String, dynamic>;
    }
    if (job['status'] != 'succeeded') throw ApiException(500, 'analysis_failed', job['error'] as String? ?? 'Analysis failed');
    return job['output'] as Map<String, dynamic>;
  }

  Future<void> openUpgrade() async {
    final plans = (await _send('GET', '/billing/plans') as List).cast<Map>();
    final pro = plans.firstWhere((p) => p['tier'] == 'pro', orElse: () => const {});
    if (pro.isEmpty) return;
    final r = await _send('POST', '/billing/checkout-session', body: {'plan_id': pro['id']});
    await launchUrl(Uri.parse(r['url'] as String), mode: LaunchMode.externalApplication);
  }
}
