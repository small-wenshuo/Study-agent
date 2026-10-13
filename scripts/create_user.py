"""服务器本地创建账号；密码通过无回显输入，不能作为命令参数。"""
from __future__ import annotations

import argparse
from getpass import getpass
from pathlib import Path

from app.auth import AuthStore
from app.config import Settings


def main():
    """初次使用 --admin 建管理员；随后普通账号不加此开关。"""
    parser = argparse.ArgumentParser(description='创建 Study Agent 本地账号')
    parser.add_argument('username')
    parser.add_argument('--admin', action='store_true', help='仅用于首次创建初始管理员')
    parser.add_argument('--reset', action='store_true', help='重置已有账号密码并退出其所有设备')
    args = parser.parse_args()
    if args.admin and args.reset:
        parser.error('--admin 与 --reset 不能同时使用。')
    password = getpass('新密码（至少 12 字符）：')
    if password != getpass('再输入一次：'):
        raise SystemExit('两次密码不一致。')
    root = Path(__file__).resolve().parents[1]
    directory = Path(Settings.from_env().data_dir)
    if not directory.is_absolute():
        directory = (root / directory).resolve()
    store = AuthStore(directory / 'auth.db')
    try:
        if args.reset:
            store.reset_password(args.username, password)
            print('密码已更新，旧登录已失效。')
        else:
            user = store.create_user(args.username, password, admin=args.admin)
            print(f"已创建账号：{user['username']}（{'管理员' if user['is_admin'] else '普通用户'}）")
    finally:
        store.close()


if __name__ == '__main__':
    main()
