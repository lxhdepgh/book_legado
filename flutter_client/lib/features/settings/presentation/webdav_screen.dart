/// WebDAV 备份工具页（占位）：WebDAV 备份 / 恢复。
library;

import 'package:flutter/material.dart';

class WebDavScreen extends StatelessWidget {
  const WebDavScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('备份')),
      body: const Center(
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(Icons.upload_file, size: 64, color: Colors.grey),
            SizedBox(height: 16),
            Text(
              'WebDAV 备份 / 恢复',
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
