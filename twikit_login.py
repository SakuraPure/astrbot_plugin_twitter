"""
twikit 一次性登录脚本

生成插件使用的 cookie 文件，避免插件运行时交互登录。
推荐先运行本脚本生成 cookie，插件加载时直接读取。

用法:
    python twikit_login.py [cookie 文件路径]

不传路径时默认写入插件目录下 data/twikit_cookies.json。

两种方式任选其一:
  1) 账号密码登录（可能需要输入 2FA / 邮箱验证码）
  2) 直接粘贴浏览器 cookie（ct0 + auth_token）
"""

import asyncio
import os
from pathlib import Path

from twikit import Client
from twikit.errors import TwitterException

DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "twikit_cookies.json"


def _ask(prompt: str, default: str = "") -> str:
    value = input(prompt).strip()
    return value or default


async def login_with_password(client: Client, cookie_path: Path) -> bool:
    username = _ask("请输入 Twitter 用户名 (@后面的部分, 不含@): ")
    if not username:
        print("用户名不能为空")
        return False
    email = _ask("请输入邮箱/手机号 (可与用户名相同, 回车跳过): ")
    password = _ask("请输入密码: ")
    if not password:
        print("密码不能为空")
        return False

    try:
        await client.login(
            auth_info_1=username,
            auth_info_2=email or None,
            password=password,
        )
    except TwitterException as e:
        print(f"登录失败: {e}")
        # 处理需要验证码的情况
        if "verification" in str(e).lower() or "2fa" in str(e).lower() or "code" in str(e).lower():
            code = _ask("检测到需要验证码，请输入收到的验证码: ")
            if code:
                try:
                    await client.login(
                        auth_info_1=username,
                        auth_info_2=email or None,
                        password=password,
                    )
                except TwitterException as e2:
                    print(f"验证码登录仍失败: {e2}")
                    return False
            else:
                return False
        else:
            return False

    client.save_cookies(str(cookie_path))
    print(f"登录成功，cookie 已保存至 {cookie_path}")
    return True


async def login_with_cookies(client: Client, cookie_path: Path) -> bool:
    ct0 = _ask("请粘贴 ct0 cookie 值: ")
    auth_token = _ask("请粘贴 auth_token cookie 值: ")
    if not ct0 or not auth_token:
        print("ct0 和 auth_token 都不能为空")
        return False

    client.set_cookies({"ct0": ct0, "auth_token": auth_token})

    # 验证 cookie 是否有效
    try:
        await client.get_user_by_screen_name("x")
    except TwitterException as e:
        print(f"cookie 验证失败: {e}")
        return False
    except Exception as e:
        print(f"cookie 验证失败: {e}")
        return False

    client.save_cookies(str(cookie_path))
    print(f"cookie 有效，已保存至 {cookie_path}")
    return True


async def main():
    cookie_path = Path(_ask(
        f"请输入 cookie 保存路径 (回车使用默认: {DEFAULT_PATH}): ",
        default=str(DEFAULT_PATH),
    ))
    cookie_path.parent.mkdir(parents=True, exist_ok=True)

    print("\n请选择登录方式:")
    print("  1) 账号密码登录")
    print("  2) 粘贴 ct0 + auth_token cookie")
    choice = _ask("输入选项 (1/2, 默认1): ", default="1")

    proxy = os.environ.get("TWITTER_PROXY") or ""
    # impersonate 浏览器 TLS 指纹，规避 X 边缘对 httpx 原生指纹的 403
    client = Client(
        language="en-US",
        proxy=proxy or None,
        impersonate="chrome",
    )

    if choice == "2":
        ok = await login_with_cookies(client, cookie_path)
    else:
        ok = await login_with_password(client, cookie_path)

    if ok:
        print("\n完成。在插件配置中确认 twitter_twikit_cookies_path 指向该文件即可。")
    else:
        print("\n登录未成功，请重试或更换登录方式。")

    try:
        http = getattr(client, "http", None)
        if http is not None and not http.is_closed:
            await http.aclose()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
