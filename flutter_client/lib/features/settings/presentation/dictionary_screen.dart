/// 词典工具页：查词 + 词典规则管理。
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/di/providers.dart';

class DictionaryScreen extends ConsumerStatefulWidget {
  const DictionaryScreen({super.key});

  @override
  ConsumerState<DictionaryScreen> createState() => _DictionaryScreenState();
}

class _DictionaryScreenState extends ConsumerState<DictionaryScreen> {
  final _wordController = TextEditingController();
  String? _definition;
  String? _phonetic;
  bool _lookingUp = false;
  List<Map<String, Object?>> _rules = [];

  @override
  void dispose() {
    _wordController.dispose();
    super.dispose();
  }

  Future<void> _lookup() async {
    final word = _wordController.text.trim();
    if (word.isEmpty) return;

    setState(() {
      _lookingUp = true;
      _definition = null;
      _phonetic = null;
    });

    try {
      final repo = ref.read(engineRepositoryProvider);
      // TODO: 添加 dict.lookup 接口
      setState(() {
        _lookingUp = false;
        _definition = '查词功能待完善（需要服务端接口）';
      });
    } catch (e) {
      setState(() {
        _lookingUp = false;
        _definition = '查询失败：$e';
      });
    }
  }

  Future<void> _loadRules() async {
    try {
      final repo = ref.read(engineRepositoryProvider);
      // TODO: 添加 dict.rules 接口
      setState(() => _rules = []);
    } catch (e) {
      // 忽略
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('词典')),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: TextField(
                    controller: _wordController,
                    decoration: const InputDecoration(
                      labelText: '查词',
                      hintText: '输入单词或短语',
                    ),
                    onSubmitted: (_) => _lookup(),
                  ),
                ),
                const SizedBox(width: 8),
                ElevatedButton.icon(
                  onPressed: _lookingUp ? null : _lookup,
                  icon: _lookingUp
                      ? const SizedBox(
                          width: 16,
                          height: 16,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.search),
                  label: const Text('查询'),
                ),
              ],
            ),
            if (_definition != null) ...[
              const SizedBox(height: 16),
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      if (_phonetic != null && _phonetic!.isNotEmpty)
                        Text(_phonetic!,
                            style: theme.textTheme.bodySmall?.copyWith(
                              color: Colors.grey,
                            )),
                      const SizedBox(height: 4),
                      SelectableText(_definition!),
                    ],
                  ),
                ),
              ),
            ],
            const SizedBox(height: 24),
            Text('词典规则',
                style: theme.textTheme.titleMedium?.copyWith(
                  fontWeight: FontWeight.bold,
                )),
            const SizedBox(height: 8),
            if (_rules.isEmpty)
              Text('暂无规则（功能待完善）', style: theme.textTheme.bodySmall)
            else
              ..._rules.map((rule) => ListTile(
                    title: Text(rule['name']?.toString() ?? ''),
                    subtitle: Text(rule['url']?.toString() ?? ''),
                  )),
          ],
        ),
      ),
    );
  }
}
