/// 书籍导出页：导出为 epub / txt。
library;

import 'package:file_picker/file_picker.dart';
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
  String? _selectedFormat;
  String _status = '';
  bool _exporting = false;

  @override
  void dispose() {
    _bookKeyController.dispose();
    _outputPathController.dispose();
    super.dispose();
  }

  Future<void> _selectOutputPath(String format) async {
    final result = await FilePicker.platform.getDirectoryPath(
      dialogTitle: '选择导出目录',
    );

    if (result != null) {
      final bookKey = _bookKeyController.text.trim();
      if (bookKey.isEmpty) {
        setState(() => _status = '请先填写书籍标识');
        return;
      }
      // 根据格式自动拼接文件名
      final fileName = '$bookKey.$format';
      setState(() {
        _outputPathController.text = '$result\\$fileName';
        _selectedFormat = format;
      });
    }
  }

  Future<void> _exportEpub() async {
    final bookKey = _bookKeyController.text.trim();
    final outputPath = _outputPathController.text.trim();
    if (bookKey.isEmpty || outputPath.isEmpty) {
      setState(() => _status = '请填写书籍标识和输出路径');
      return;
    }

    await _doExport('epub', bookKey, outputPath);
  }

  Future<void> _exportTxt() async {
    final bookKey = _bookKeyController.text.trim();
    final outputPath = _outputPathController.text.trim();
    if (bookKey.isEmpty || outputPath.isEmpty) {
      setState(() => _status = '请填写书籍标识和输出路径');
      return;
    }

    await _doExport('txt', bookKey, outputPath);
  }

  Future<void> _doExport(String format, String bookKey, String outputPath) async {
    setState(() {
      _exporting = true;
      _status = '正在导出 $format...';
    });

    try {
      final repo = ref.read(engineRepositoryProvider);
      if (format == 'epub') {
        await repo.exportEpub(bookKey, outputPath);
      } else {
        await repo.exportTxt(bookKey, outputPath);
      }
      setState(() {
        _exporting = false;
        _status = '导出成功：$outputPath';
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
              decoration: InputDecoration(
                labelText: '输出路径',
                hintText: '选择目录后自动填充',
                border: const OutlineInputBorder(),
                suffixIcon: IconButton(
                  icon: const Icon(Icons.folder_open),
                  tooltip: '选择目录',
                  onPressed: () async {
                    final format = _selectedFormat ?? 'epub';
                    await _selectOutputPath(format);
                  },
                ),
              ),
            ),
            const SizedBox(height: 16),
            Row(
              children: [
                ElevatedButton.icon(
                  onPressed: _exporting ? null : () => _selectOutputPath('epub'),
                  icon: const Icon(Icons.folder_open),
                  label: const Text('选 epub 目录'),
                ),
                const SizedBox(width: 8),
                OutlinedButton.icon(
                  onPressed: _exporting ? null : () => _selectOutputPath('txt'),
                  icon: const Icon(Icons.folder_open),
                  label: const Text('选 txt 目录'),
                ),
              ],
            ),
            const SizedBox(height: 12),
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
