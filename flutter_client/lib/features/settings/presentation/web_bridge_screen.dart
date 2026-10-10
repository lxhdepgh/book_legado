/// 网络桥工具页（占位）：本机 HTTP/WebSocket 书源管理 / 调试。
library;

import 'package:flutter/material.dart';

class WebBridgeScreen extends StatelessWidget {
  const WebBridgeScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('网络桥')),
      body: const Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(Icons.lan, size: 64, color: Colors.grey),
            SizedBox(height: 16),
            Text(
              'HTTP/WebSocket 书源管理',
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
