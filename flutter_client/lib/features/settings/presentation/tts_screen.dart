/// TTS 朗读管理页：语音档列表 + 新建/删除。
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/di/providers.dart';

class TtsScreen extends ConsumerStatefulWidget {
  const TtsScreen({super.key});

  @override
  ConsumerState<TtsScreen> createState() => _TtsScreenState();
}

class _TtsScreenState extends ConsumerState<TtsScreen> {
  List<Map<String, Object?>> _profiles = [];
  bool _loading = false;

  @override
  void initState() {
    super.initState();
    _loadProfiles();
  }

  Future<void> _loadProfiles() async {
    setState(() => _loading = true);
    try {
      final repo = ref.read(engineRepositoryProvider);
      // TODO: 添加 listProfiles 接口
      setState(() {
        _profiles = [];
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

  Future<void> _addProfile() async {
    // TODO: 需要表单对话框
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('新建语音档功能待实现')),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(
        title: const Text('朗读'),
        actions: [
          IconButton(
            icon: const Icon(Icons.add),
            tooltip: '新建语音档',
            onPressed: _addProfile,
          ),
        ],
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _profiles.isEmpty
              ? Center(
                  child: Column(
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      Icon(Icons.settings_voice, size: 64, color: Colors.grey),
                      const SizedBox(height: 16),
                      Text('暂无语音档', style: theme.textTheme.bodyLarge),
                      const SizedBox(height: 8),
                      Text('语音档管理功能待完善',
                          style: theme.textTheme.bodySmall?.copyWith(
                            color: Colors.grey,
                          )),
                    ],
                  ),
                )
              : ListView.builder(
                  itemCount: _profiles.length,
                  itemBuilder: (ctx, i) {
                    final profile = _profiles[i];
                    final name = profile['name']?.toString() ?? '';
                    return ListTile(
                      title: Text(name),
                      subtitle: Text(profile.toString()),
                    );
                  },
                ),
    );
  }
}
