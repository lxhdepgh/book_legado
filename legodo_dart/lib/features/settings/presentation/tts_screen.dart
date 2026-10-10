/// TTS 朗读管理页（占位）：语音配置功能待完善。
///
/// 当前为说明页面，完整功能需要：
/// 1. Python 后端 TTS 服务接口（pyttsx3/SAPI5）
/// 2. 语音列表加载 API
/// 3. 语速/音调调节 UI
/// 4. 试听功能
library;

import 'package:flutter/material.dart';

class TtsScreen extends StatelessWidget {
  const TtsScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('朗读')),
      body: Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(Icons.settings_voice, size: 64, color: Colors.grey),
            const SizedBox(height: 16),
            Text('TTS 朗读配置',
                style: theme.textTheme.headlineSmall?.copyWith(
                  fontWeight: FontWeight.bold,
                )),
            const SizedBox(height: 8),
            Text('语音配置功能待完善', style: theme.textTheme.bodyMedium),
            const SizedBox(height: 16),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 32),
              child: Text(
                '此功能需要完整的 TTS 服务实现：\n'
                '- Python 侧 pyttsx3/SAPI5 集成\n'
                '- 语音列表加载\n'
                '- 语速/音调调节\n'
                '- 试听功能\n\n'
                '当前为占位页面，后续完善。',
                textAlign: TextAlign.center,
                style: theme.textTheme.bodySmall?.copyWith(
                  color: Colors.grey,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
