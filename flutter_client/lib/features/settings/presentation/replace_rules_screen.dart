/// 替换规则工具页（占位）：全局正文替换规则管理。
library;

import 'package:flutter/material.dart';

class ReplaceRulesScreen extends StatelessWidget {
  const ReplaceRulesScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('替换规则')),
      body: const Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(Icons.find_replace, size: 64, color: Colors.grey),
            SizedBox(height: 16),
            Text(
              '全局正文替换规则',
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
