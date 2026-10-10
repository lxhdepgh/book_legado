/// 朗读工具页（占位）：听书语音档（系统 / edge / HTTP-TTS）。
library;

import 'package:flutter/material.dart';

class TtsScreen extends StatelessWidget {
  const TtsScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('朗读')),
      body: const Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(Icons.settings_voice, size: 64, color: Colors.grey),
            SizedBox(height: 16),
            Text(
              '听书语音档管理',
              style: TextStyle(fontSize: 20, fontWeight: FontWeight.bold),
            ),
            SizedBox(height: 8),
            Text(
              '功能开发中',
              style: TextStyle(color: Colors.grey),
            ),
          ],
        ),
      ),
    );
  }
}
