import os
import time
import datetime
import subprocess
from dotenv import load_dotenv

# 1. 加载环境变量
# 确保脚本能找到 .env 文件 (假设脚本和 .env 同级)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))

# 2. 配置
BACKUP_DIR = os.path.join(BASE_DIR, "backups")  # 备份存放在 backups 文件夹
DB_CONN = os.environ.get("DB_CONNECTION_STRING")  # 从 .env 获取连接串
KEEP_DAYS = 30  # 保留多少天


def clean_old_backups():
    """清理旧备份"""
    print(f"🧹 开始清理 {KEEP_DAYS} 天前的旧备份...")
    now = time.time()
    deleted_count = 0

    # 遍历备份目录
    for filename in os.listdir(BACKUP_DIR):
        file_path = os.path.join(BACKUP_DIR, filename)

        # 只处理 .sql 文件
        if os.path.isfile(file_path) and filename.endswith(".sql"):
            # 获取文件修改时间
            file_mtime = os.path.getmtime(file_path)

            # 如果文件时间 < (当前时间 - 30天秒数)
            if file_mtime < (now - KEEP_DAYS * 86400):
                try:
                    os.remove(file_path)
                    print(f"   🗑️ 已删除过期文件: {filename}")
                    deleted_count += 1
                except Exception as e:
                    print(f"   ❌ 删除失败 {filename}: {e}")

    if deleted_count == 0:
        print("   ✅ 没有过期的备份需要清理。")
    else:
        print(f"   ✅ 清理完成，共删除 {deleted_count} 个文件。")


def backup():
    # 1. 确保目录存在
    if not os.path.exists(BACKUP_DIR):
        os.makedirs(BACKUP_DIR)

    # 2. 生成文件名 (family_paw_2025-12-30.sql)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    filename = f"{BACKUP_DIR}/family_paw_{today}.sql"

    print(f"🚀 开始备份: {today} ...")

    # 3. 执行 pg_dump
    # 这里的 DB_CONN 就是你刚才在 .env 里填的那串
    cmd = f"pg_dump '{DB_CONN}' -f '{filename}'"

    try:
        # 执行命令，如果出错会抛出异常
        subprocess.run(cmd, shell=True, check=True)
        print(f"✅ 备份成功！文件已保存至: {filename}")

        # 4. 备份成功后，执行清理
        clean_old_backups()

    except subprocess.CalledProcessError as e:
        print(f"❌ 备份失败 (pg_dump error): {e}")
    except Exception as e:
        print(f"❌ 备份失败 (其他错误): {e}")


if __name__ == "__main__":
    if not DB_CONN:
        print("❌ 错误: 未在 .env 找到 DB_CONNECTION_STRING")
        print("请在 .env 添加: DB_CONNECTION_STRING=postgresql://用户:密码@主机:端口/库名")
    else:
        backup()