#!/usr/bin/env python3
"""为所有 ft.IconButton 添加 key 属性的辅助脚本。

用法：
    python scripts/add_icon_button_keys.py

功能：
- 扫描 legodo_flet/app/views/ 下所有 .py 文件
- 查找 ft.IconButton(...) 调用
- 如果缺少 key 参数，则自动添加语义化 key
- 生成修改报告

注意：此脚本只做静态分析，不执行实际修改（避免破坏代码）。
"""
import re
from pathlib import Path


def scan_icon_buttons(file_path: Path) -> list[dict]:
    """扫描文件中的 IconButton 调用，返回缺失 key 的位置列表。"""
    results = []
    
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    for i, line in enumerate(lines, 1):
        # 匹配 ft.IconButton( 模式
        if 'ft.IconButton(' in line or 'IconButton(' in line:
            # 检查是否已有 key 参数
            has_key = 'key=' in line or 'key =' in line
            
            # 提取图标名称（用于生成语义化 key）
            icon_match = re.search(r'icon=ft\.Icons\.(\w+)', line)
            icon_name = icon_match.group(1) if icon_match else None
            
            # 提取 tooltip（用于更好地理解按钮用途）
            tooltip_match = re.search(r'tooltip="([^"]+)"', line)
            tooltip = tooltip_match.group(1) if tooltip_match else None
            
            results.append({
                'line_num': i,
                'has_key': has_key,
                'icon': icon_name,
                'tooltip': tooltip,
                'line_content': line.strip(),
            })
    
    return results


def suggest_key(icon_name: str, tooltip: str | None, context: str = '') -> str:
    """根据图标和提示生成语义化 key。"""
    if tooltip:
        # 从 tooltip 提取关键词
        keywords = {
            '刷新': 'refresh',
            '扫描': 'scan',
            '添加': 'add',
            '删除': 'delete',
            '移除': 'remove',
            '编辑': 'edit',
            '保存': 'save',
            '取消': 'cancel',
            '关闭': 'close',
            '搜索': 'search',
            '导入': 'import',
            '导出': 'export',
            '设置': 'settings',
            '更多': 'more',
            '帮助': 'help',
        }
        
        for cn, en in keywords.items():
            if cn in tooltip:
                return en
    
    #  fallback 到图标名称
    if icon_name:
        return icon_name.lower()
    
    return 'action'


def main():
    views_dir = Path(__file__).parent.parent / 'legodo_flet' / 'app' / 'views'
    
    print(f"扫描目录: {views_dir}")
    print("=" * 80)
    
    total_files = 0
    total_buttons = 0
    missing_keys = 0
    
    for py_file in sorted(views_dir.rglob('*.py')):
        buttons = scan_icon_buttons(py_file)
        if not buttons:
            continue
        
        total_files += 1
        file_missing = sum(1 for b in buttons if not b['has_key'])
        total_buttons += len(buttons)
        missing_keys += file_missing
        
        if file_missing > 0:
            print(f"\n{py_file.relative_to(views_dir.parent)}:")
            for btn in buttons:
                if not btn['has_key']:
                    suggested_key = suggest_key(btn['icon'], btn['tooltip'])
                    print(f"  行 {btn['line_num']}: 建议添加 key=\"{suggested_key}\"")
                    print(f"    内容: {btn['line_content'][:80]}...")
    
    print("\n" + "=" * 80)
    print(f"统计结果:")
    print(f"  扫描文件数: {total_files}")
    print(f"  IconButton 总数: {total_buttons}")
    print(f"  缺失 key 数量: {missing_keys}")
    print(f"  覆盖率: {(total_buttons - missing_keys) / total_buttons * 100:.1f}%")


if __name__ == '__main__':
    main()
