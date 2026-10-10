/// 字体管理页：系统字体扫描 + 用户字体添加/移除 + 当前选中。
library;

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/di/providers.dart';

class FontScreen extends ConsumerStatefulWidget {
  const FontScreen({super.key});

  @override
  ConsumerState<FontScreen> createState() => _FontScreenState();
}

class _FontScreenState extends ConsumerState<FontScreen> {
  List<Map<String, Object?>> _systemFonts = [];
  List<Map<String, Object?>> _userFonts = [];
  String? _currentFont;
  bool _loading = false;

  @override
  void initState() {
    super.initState();
    _loadFonts();
  }

  Future<void> _loadFonts() async {
    setState(() => _loading = true);
    try {
      final repo = ref.read(engineRepositoryProvider);
      final system = await repo.fontScanSystem();
      setState(() {
        _systemFonts = system;
        _loading = false;
      });
    } catch (e) {
      setState(() => _loading = false);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('加载失败：$e')),
        );
      }
    }
  }

  Future<void> _addUserFont() async {
    final result = await FilePicker.platform.pickFiles(
      type: FileType.custom,
      allowedExtensions: ['ttf', 'otf', 'ttc', 'woff', 'woff2'],
    );

    if (result == null || result.files.isEmpty) return;

    final path = result.files.single.path;
    if (path == null) return;

    try {
      final repo = ref.read(engineRepositoryProvider);
      await repo.fontAddUser(path);
      await _loadFonts();
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('添加失败：$e')),
        );
      }
    }
  }

  Future<void> _selectFont(String family) async {
    try {
      final repo = ref.read(engineRepositoryProvider);
      await repo.fontSelect(family);
      setState(() => _currentFont = family);
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('切换失败：$e')),
        );
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(
        title: const Text('字体'),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            tooltip: '扫描系统字体',
            onPressed: _loading ? null : _loadFonts,
          ),
          IconButton(
            icon: const Icon(Icons.add),
            tooltip: '添加字体文件',
            onPressed: _addUserFont,
          ),
        ],
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : SingleChildScrollView(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('当前字体：${_currentFont ?? "系统默认"}',
                      style: theme.textTheme.bodyLarge),
                  const SizedBox(height: 16),
                  if (_systemFonts.isNotEmpty) ...[
                    Text('系统字体（${_systemFonts.length}）',
                        style: theme.textTheme.titleMedium?.copyWith(
                          fontWeight: FontWeight.bold,
                        )),
                    const SizedBox(height: 8),
                    ..._systemFonts.map((font) {
                      final family = font['family']?.toString() ?? '';
                      final name = font['name']?.toString() ?? family;
                      return ListTile(
                        selected: _currentFont == family,
                        title: Text(name),
                        subtitle: Text(font['path']?.toString() ?? ''),
                        onTap: () => _selectFont(family),
                      );
                    }).toList(),
                  ],
                  if (_userFonts.isNotEmpty) ...[
                    const SizedBox(height: 24),
                    Text('用户字体（${_userFonts.length}）',
                        style: theme.textTheme.titleMedium?.copyWith(
                          fontWeight: FontWeight.bold,
                        )),
                    const SizedBox(height: 8),
                    ..._userFonts.map((font) {
                      final family = font['family']?.toString() ?? '';
                      final name = font['name']?.toString() ?? family;
                      final fontId = font['id']?.toString() ?? '';
                      return ListTile(
                        selected: _currentFont == family,
                        title: Text(name),
                        subtitle: Text(font['path']?.toString() ?? ''),
                        trailing: IconButton(
                          icon: const Icon(Icons.delete_outline),
                          tooltip: '移除',
                          onPressed: () async {
                            try {
                              final repo = ref.read(engineRepositoryProvider);
                              await repo.fontRemove(fontId);
                              await _loadFonts();
                            } catch (e) {
                              if (mounted) {
                                ScaffoldMessenger.of(context).showSnackBar(
                                  SnackBar(content: Text('删除失败：$e')),
                                );
                              }
                            }
                          },
                        ),
                        onTap: () => _selectFont(family),
                      );
                    }).toList(),
                  ],
                ],
              ),
            ),
    );
  }
}
