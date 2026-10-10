/// 网络桥管理页：启动/停止 HTTP/WebSocket 书源管理服务。
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/di/providers.dart';

class WebBridgeScreen extends ConsumerStatefulWidget {
  const WebBridgeScreen({super.key});

  @override
  ConsumerState<WebBridgeScreen> createState() => _WebBridgeScreenState();
}

class _WebBridgeScreenState extends ConsumerState<WebBridgeScreen> {
  bool _running = false;
  Map<String, Object?> _status = {};
  bool _loading = false;
  final _portController = TextEditingController(text: '8080');

  @override
  void dispose() {
    _portController.dispose();
    super.dispose();
  }

  Future<void> _loadStatus() async {
    try {
      final repo = ref.read(engineRepositoryProvider);
      final status = await repo.webbridgeStatus();
      setState(() {
        _status = status;
        _running = status['running'] as bool? ?? false;
      });
    } catch (e) {
      // 忽略错误
    }
  }

  Future<void> _start() async {
    final port = int.tryParse(_portController.text.trim()) ?? 8080;
    setState(() => _loading = true);
    try {
      final repo = ref.read(engineRepositoryProvider);
      await repo.webbridgeStart(port);
      setState(() {
        _loading = false;
        _running = true;
      });
      await _loadStatus();
    } catch (e) {
      setState(() => _loading = false);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('启动失败：$e')),
        );
      }
    }
  }

  Future<void> _stop() async {
    setState(() => _loading = true);
    try {
      final repo = ref.read(engineRepositoryProvider);
      await repo.webbridgeStop();
      setState(() {
        _loading = false;
        _running = false;
      });
    } catch (e) {
      setState(() => _loading = false);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('停止失败：$e')),
        );
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('网络桥')),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('服务控制',
                        style: theme.textTheme.titleMedium?.copyWith(
                          fontWeight: FontWeight.bold,
                        )),
                    const SizedBox(height: 12),
                    Row(
                      children: [
                        Expanded(
                          child: TextField(
                            controller: _portController,
                            decoration: const InputDecoration(
                              labelText: '端口号',
                              hintText: '8080',
                            ),
                            keyboardType: TextInputType.number,
                            enabled: !_running,
                          ),
                        ),
                        const SizedBox(width: 12),
                        ElevatedButton.icon(
                          onPressed: (_loading || _running) ? null : _start,
                          icon: _loading
                              ? const SizedBox(
                                  width: 16,
                                  height: 16,
                                  child:
                                      CircularProgressIndicator(strokeWidth: 2),
                                )
                              : const Icon(Icons.play_arrow),
                          label: const Text('启动'),
                        ),
                        const SizedBox(width: 8),
                        OutlinedButton.icon(
                          onPressed: (_loading || !_running) ? null : _stop,
                          icon: const Icon(Icons.stop),
                          label: const Text('停止'),
                        ),
                      ],
                    ),
                    const SizedBox(height: 8),
                    Text(
                      _running ? '状态：运行中' : '状态：已停止',
                      style: theme.textTheme.bodySmall?.copyWith(
                        color: _running ? Colors.green : Colors.grey,
                      ),
                    ),
                  ],
                ),
              ),
            ),
            if (_status.isNotEmpty) ...[
              const SizedBox(height: 16),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text('详细信息',
                          style: theme.textTheme.titleMedium?.copyWith(
                            fontWeight: FontWeight.bold,
                          )),
                      const SizedBox(height: 8),
                      ..._status.entries.map((e) => Padding(
                            padding: const EdgeInsets.symmetric(vertical: 2),
                            child: Text('${e.key}: ${e.value}'),
                          )),
                    ],
                  ),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}
