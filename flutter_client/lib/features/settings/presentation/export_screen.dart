/// 书籍导出页：导出为 epub / txt。
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/di/providers.dart';

class ExportScreen extends ConsumerStatefulWidget {
  const ExportScreen({super.key});

  @override
  ConsumerState<ExportScreen> createState() => _ExportScreenState();
}

class _ExportScreenState extends ConsumerState<ExportScreen> {
  final _bookKeyController = TextEditingController();
  final _outputPathController = TextEditingController();
  String _status = '';
  bool _exporting = false;

  @override
  void dispose() {
    _bookKeyController.dispose();
    _outputPathController.dispose();
    super.dispose();
  }

  Future<void> _exportEpub() async {
    final bookKey = _bookKeyController.text.trim();
    final outputPath = _outputPathController.text.trim();
    if (bookKey.isEmpty || outputPath.isEmpty) {
      setState(() => _status = '请填写书籍标识和输出路径');
      return;
    }

    setState(() {
      _exporting = true;
      _status = '正在导出 epub...';
    });

    try {
      final repo = ref.read(engineRepositoryProvider);
      await repo.exportEpub(bookKey, outputPath);
      setState(() {
        _exporting = false;
        _status = '导出成功';
      });
    } catch (e) {
      setState(() {
        _exporting = false;
        _status = '导出失败：$e';
      });
    }
  }

  Future<void> _exportTxt() async {
    final bookKey = _bookKeyController.text.trim();
    final outputPath = _outputPathController.text.trim();
    if (bookKey.isEmpty || outputPath.isEmpty) {
      setState(() => _status = '请填写书籍标识和输出路径');
      return;
    }

    setState(() {
      _exporting = true;
      _status = '正在导出 txt...';
    });

    try {
      final repo = ref.read(engineRepositoryProvider);
      await repo.exportTxt(bookKey, outputPath);
      setState(() {
        _exporting = false;
        _status = '导出成功';
      });
    } catch (e) {
      setState(() {
        _exporting = false;
        _status = '导出失败：$e';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('导出')),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            TextField(
              controller: _bookKeyController,
              decoration: const InputDecoration(
                labelText: '书籍标识',
                hintText: '书源URL + 书籍ID',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _outputPathController,
              decoration: const InputDecoration(
                labelText: '输出路径',
                hintText: '/path/to/output/file',
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 16),
            Row(
              children: [
                ElevatedButton.icon(
                  onPressed: _exporting ? null : _exportEpub,
                  icon: _exporting
                      ? const SizedBox(
                          width: 16,
                          height: 16,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.download_for_offline),
                  label: const Text('导出 epub'),
                ),
                const SizedBox(width: 8),
                OutlinedButton.icon(
                  onPressed: _exporting ? null : _exportTxt,
                  icon: const Icon(Icons.description),
                  label: const Text('导出 txt'),
                ),
              ],
            ),
            if (_status.isNotEmpty) ...[
              const SizedBox(height: 16),
              Text(_status, style: theme.textTheme.bodySmall),
            ],
          ],
        ),
      ),
    );
  }
}
