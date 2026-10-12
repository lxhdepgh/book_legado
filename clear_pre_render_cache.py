"""清理预渲染缓存文件。"""
from pathlib import Path
import shutil

cache_dir = Path(r"C:\Users\73517\AppData\Local\LegadoBookSourceCheck\cache\pre_render")

if cache_dir.exists():
    count = 0
    for json_file in cache_dir.glob("*.json"):
        json_file.unlink()
        count += 1
    print(f"已清理 {count} 个缓存文件")
else:
    print("缓存目录不存在")
