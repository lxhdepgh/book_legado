/// WebDAV 备份恢复页：备份/恢复/列出备份。
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/di/providers.dart';

class WebDavScreen extends ConsumerStatefulWidget {
  const WebDavScreen({super.key});

  @override
  ConsumerState<WebDavScreen> createState() => _WebDavScreenState();
}

class _WebDavScreenState extends ConsumerState<WebDavScreen> {
  List<Map<String, Object?>> _backups = [];
  bool _loading = false;
  String _status = '';

  @override
  void initState() {
    super.initState();
    _loadBackups();
  }

  Future<void> _loadBackups() async {
    setState(() => _loading = true);
    try {
      final repo = ref.read(engineRepositoryProvider);
      final backups = await repo.webdavListBackups();
      setState(() {
        _backups = backups;
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

  Future<void> _backup() async {
    setState(() {
      _loading = true;
      _status = '正在备份...';
    });
    try {
      final repo = ref.read(engineRepositoryProvider);
      await repo.webdavBackup();
      setState(() {
        _loading = false;
        _status = '备份成功';
      });
      await _loadBackups();
    } catch (e) {
      setState(() {
        _loading = false;
        _status = '备份失败：$e';
      });
    }
  }

  Future<void> _restore(String backupId) async {
    setState(() {
      _loading = true;
      _status = '正在恢复...';
    });
    try {
      final repo = ref.read(engineRepositoryProvider);
      await repo.webdavRestore(backupId);
      setState(() {
        _loading = false;
        _status = '恢复成功';
      });
    } catch (e) {
      setState(() {
        _loading = false;
        _status = '恢复失败：$e';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('备份')),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                ElevatedButton.icon(
                  onPressed: _loading ? null : _backup,
                  icon: _loading
                      ? const SizedBox(
                          width: 16,
                          height: 16,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.upload_file),
                  label: const Text('立即备份'),
                ),
                if (_status.isNotEmpty) ...[
                  const SizedBox(height: 8),
                  Text(_status, style: theme.textTheme.bodySmall),
                ],
              ],
            ),
          ),
          const Divider(height: 1),
          Expanded(
            child: _loading && _backups.isEmpty
                ? const Center(child: CircularProgressIndicator())
                : _backups.isEmpty
                    ? const Center(child: Text('暂无备份'))
                    : ListView.builder(
                        itemCount: _backups.length,
                        itemBuilder: (ctx, i) {
                          final backup = _backups[i];
                          final id = backup['id']?.toString() ?? '';
                          final name = backup['name']?.toString() ?? '备份 $id';
                          final date = backup['date']?.toString() ?? '';
                          return ListTile(
                            title: Text(name),
                            subtitle: Text(date),
                            trailing: ElevatedButton(
                              onPressed: () => _restore(id),
                              child: const Text('恢复'),
                            ),
                          );
                        },
                      ),
          ),
        ],
      ),
    );
  }
}
