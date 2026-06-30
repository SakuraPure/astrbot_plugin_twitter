"""
Twitter API 交互模块
基于 twikit（Twitter GraphQL）获取 Twitter/X 推文数据，替代原 Nitter 方案。
"""

import re
from typing import Optional

import httpx
from twikit import Client
from twikit.errors import TwitterException
from astrbot.api import logger

# 有效的图片质量选项
IMAGE_QUALITY_OPTIONS = ("large", "orig")


class TwitterAPI:
    """Twitter API 交互类，基于 twikit 通过登录账号获取推文"""

    def __init__(
        self,
        proxy: Optional[str] = None,
        image_quality: str = "orig",
        cookies_path: str = "",
        username: str = "",
        email: str = "",
        password: str = "",
    ):
        self.proxy = proxy
        self.image_quality = (
            image_quality if image_quality in IMAGE_QUALITY_OPTIONS else "orig"
        )
        self.cookies_path = str(cookies_path or "").strip()
        self.username = str(username or "").strip()
        self.email = str(email or "").strip()
        self.password = str(password or "").strip()

        # twikit 客户端
        self.client: Optional[Client] = None
        # 账号是否登录可用
        self.available: bool = False

        # 独立的 httpx 客户端，用于探测远程媒体文件大小
        self._size_client: Optional[httpx.AsyncClient] = None

    # ========== 认证 ==========

    def _make_client(self) -> Client:
        return Client(language="en-US", proxy=self.proxy or None)

    async def ensure_login(self) -> bool:
        """登录或加载 cookie，确保账号可用。返回是否可用。"""
        self.client = self._make_client()

        # 1) 优先加载已有 cookie
        if self.cookies_path:
            try:
                self.client.load_cookies(self.cookies_path)
                if await self._verify_session():
                    self.available = True
                    logger.info("twikit 已通过 cookie 登录，账号可用")
                    return True
                logger.warning("twikit cookie 已失效，尝试重新登录")
            except FileNotFoundError:
                logger.info("twikit cookie 文件不存在，将尝试账号密码登录")
            except Exception as e:
                logger.warning(f"twikit 加载 cookie 失败: {e}")

        # 2) 回退：账号密码登录
        if self.username and self.password:
            try:
                await self.client.login(
                    auth_info_1=self.username,
                    auth_info_2=self.email or None,
                    password=self.password,
                )
                if self.cookies_path:
                    self.client.save_cookies(self.cookies_path)
                    logger.info(f"twikit 登录成功，cookie 已保存至 {self.cookies_path}")
                else:
                    logger.info("twikit 登录成功（未配置 cookie 路径，未持久化）")
                self.available = True
                return True
            except Exception as e:
                logger.error(f"twikit 账号密码登录失败: {e}")
                self.available = False
                return False

        logger.error(
            "twikit 登录失败：无可用 cookie，且未配置账号密码。"
            "请运行 twikit_login.py 生成 cookie，或填写 twikit 账号密码配置项。"
        )
        self.available = False
        return False

    async def _verify_session(self) -> bool:
        """发一次轻量请求验证 cookie 是否有效。"""
        try:
            await self.client.get_user_by_screen_name("x")
            return True
        except TwitterException as e:
            logger.debug(f"twikit 会话验证失败（TwitterException）: {e}")
            return False
        except Exception as e:
            logger.debug(f"twikit 会话验证失败: {e}")
            return False

    async def close(self):
        """关闭客户端"""
        if self._size_client and not self._size_client.is_closed:
            await self._size_client.aclose()
            self._size_client = None
        # twikit 内部 http 客户端
        if self.client is not None:
            try:
                http = getattr(self.client, "http", None)
                if http is not None and not http.is_closed:
                    await http.aclose()
            except Exception:
                pass
        self.available = False

    # ========== 远程文件大小探测（视频超限判断用） ==========

    async def _get_size_client(self) -> httpx.AsyncClient:
        if self._size_client is None or self._size_client.is_closed:
            self._size_client = httpx.AsyncClient(
                proxy=self.proxy or None,
                http2=True,
                timeout=15.0,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
            )
        return self._size_client

    @staticmethod
    def _parse_content_length(value: str) -> Optional[int]:
        try:
            length = int(str(value or "").strip())
        except (TypeError, ValueError):
            return None
        return length if length > 0 else None

    @staticmethod
    def _parse_content_range_total(value: str) -> Optional[int]:
        match = re.search(r"/(\d+)\s*$", str(value or ""))
        if not match:
            return None
        try:
            total = int(match.group(1))
        except ValueError:
            return None
        return total if total > 0 else None

    async def get_remote_file_size(self, url: str) -> Optional[int]:
        """尽量在不下载正文的情况下探测远程文件大小。"""
        url = str(url or "").strip()
        if not url:
            return None

        client = await self._get_size_client()
        try:
            resp = await client.head(url, timeout=15.0)
            if resp.status_code < 400:
                size = self._parse_content_length(resp.headers.get("content-length", ""))
                if size is not None:
                    return size
        except Exception as e:
            logger.debug(f"HEAD 探测远程文件大小失败: {url}, {e}")

        try:
            async with client.stream(
                "GET",
                url,
                headers={"Range": "bytes=0-0"},
                timeout=15.0,
            ) as resp:
                if resp.status_code >= 400:
                    return None
                size = self._parse_content_range_total(
                    resp.headers.get("content-range", "")
                )
                if size is not None:
                    return size
                if resp.status_code == 206:
                    return None
                return self._parse_content_length(resp.headers.get("content-length", ""))
        except Exception as e:
            logger.debug(f"Range 探测远程文件大小失败: {url}, {e}")
            return None

    # ========== 数据获取 ==========

    async def _get_user(self, username: str):
        """按用户名获取 twikit User 对象，失败返回 None。"""
        if not self.available or self.client is None:
            return None
        try:
            return await self.client.get_user_by_screen_name(username.strip("@"))
        except Exception as e:
            logger.error(f"获取用户信息失败 {username}: {e}")
            return None

    async def get_user_info(self, username: str) -> dict:
        """获取 Twitter 用户信息

        返回:
            {"status": bool, "screen_name": str, "bio": str, "user_name": str}
        """
        user = await self._get_user(username)
        if user is None:
            return {"status": False, "screen_name": "", "bio": "", "user_name": username}

        return {
            "status": True,
            "screen_name": str(getattr(user, "name", "") or username),
            "bio": str(getattr(user, "description", "") or ""),
            "user_name": str(getattr(user, "screen_name", "") or username),
        }

    async def get_user_newtimeline(self, username: str, since_id: str = "") -> list[str]:
        """获取用户比 since_id 更新的推文 ID 列表

        无 since_id 时仅返回最新一条推文 ID（用于首次订阅定位）。
        返回结果按时间正序（最旧在前）。
        """
        items = await self.get_user_timeline_items(
            username,
            since_id=since_id,
            limit=1 if not since_id else 0,
        )
        return [str(item.get("tweet_id") or "") for item in items if item.get("tweet_id")]

    async def get_user_timeline_items(
        self, username: str, since_id: str = "", limit: int = 0
    ) -> list[dict]:
        """获取用户时间线条目，包含转帖元数据与媒体类型。

        有 since_id 时返回时间正序（最旧在前）；无 since_id 时保持最新在前，
        便于测试指令向后寻找下一条非转帖。
        """
        if not self.available or self.client is None:
            return []

        user = await self._get_user(username)
        if user is None:
            return []

        try:
            result = await self.client.get_user_tweets(
                user.id, "Tweets", count=20
            )
        except Exception as e:
            logger.error(f"获取用户时间线失败 {username}: {e}")
            return []

        parsed_items: list[dict] = []
        for tweet in result:
            is_retweet = getattr(tweet, "retweeted_tweet", None) is not None
            src = tweet.retweeted_tweet or tweet

            tweet_id = str(getattr(src, "id", "") or "")
            if not tweet_id:
                continue

            src_user = getattr(src, "user", None)
            author_username = (
                getattr(src_user, "screen_name", "") or username
                if src_user is not None
                else username
            )

            # since_id 过滤（Twitter id 为 snowflake 数值，可比较）
            if since_id:
                try:
                    if int(tweet_id) <= int(since_id):
                        if is_retweet:
                            continue
                        break
                except ValueError:
                    continue

            retweeter_username = ""
            retweeter_screen_name = ""
            if is_retweet:
                rt_user = getattr(tweet, "user", None)
                retweeter_username = (
                    getattr(rt_user, "screen_name", "") or username
                    if rt_user is not None
                    else username
                )
                retweeter_screen_name = (
                    getattr(rt_user, "name", "") or retweeter_username
                    if rt_user is not None
                    else retweeter_username
                )

            parsed_items.append(
                {
                    "tweet_id": tweet_id,
                    "username": author_username,
                    "is_retweet": is_retweet,
                    "retweeter_username": retweeter_username,
                    "retweeter_screen_name": retweeter_screen_name,
                    "media_type": self._detect_media_type(getattr(src, "media", None)),
                }
            )

            if limit > 0 and len(parsed_items) >= limit:
                break

        if since_id:
            parsed_items.reverse()
        return parsed_items

    async def get_tweet(self, username: str, tweet_id: str) -> dict:
        """获取推文详细信息

        返回:
            推文信息字典，包含 text, images, videos, video_previews, quote,
            is_r18, screen_name, retweet 等
        """
        if not self.available or self.client is None:
            return self._empty_tweet(tweet_id, username)

        try:
            tweet = await self.client.get_tweet_by_id(str(tweet_id))
        except Exception as e:
            logger.error(f"获取推文详情失败 {username}/{tweet_id}: {e}")
            return self._empty_tweet(tweet_id, username)

        return self._tweet_to_info(tweet, fallback_username=username)

    # ========== 推文数据映射 ==========

    @staticmethod
    def _empty_tweet(tweet_id: str, username: str) -> dict:
        return {
            "tweet_id": str(tweet_id or ""),
            "username": username,
            "screen_name": username,
            "avatar": "",
            "verified": False,
            "date": "",
            "stats": {},
            "text": "",
            "images": [],
            "videos": [],
            "video_previews": [],
            "quote": None,
            "retweet": None,
            "is_r18": False,
        }

    @staticmethod
    def _format_count(value) -> str:
        if value is None:
            return ""
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return str(value or "")

    @staticmethod
    def _format_duration(duration_millis) -> str:
        try:
            total = int(duration_millis or 0) // 1000
        except (TypeError, ValueError):
            return ""
        if total <= 0:
            return ""
        minutes, seconds = divmod(total, 60)
        return f"{minutes}:{seconds:02d}"

    def _build_image_url(self, url: str) -> str:
        """根据图片质量配置构建图片 URL（twimg 直链追加 name 参数）。"""
        url = str(url or "").strip()
        if not url:
            return ""
        quality = "orig" if self.image_quality == "orig" else "large"
        # 已带查询参数则避免重复（如格式后缀）
        if "?" in url:
            if "name=" in url:
                return url
            return f"{url}&name={quality}&format=jpg"
        return f"{url}?name={quality}&format=jpg"

    @staticmethod
    def _detect_media_type(media) -> Optional[str]:
        """根据 twikit 媒体列表判定媒体类型：image / video / None。

        GIF 在 twikit 中为 AnimatedGif，归入 video。
        """
        if not media:
            return None
        for m in media:
            # Video / AnimatedGif 拥有 video_info 属性
            if hasattr(m, "video_info"):
                return "video"
        for m in media:
            if hasattr(m, "media_url"):
                return "image"
        return None

    def _extract_media(self, media) -> tuple[list[str], list[str], list[dict]]:
        """从 twikit 媒体列表提取 (images, videos, video_previews)。"""
        images: list[str] = []
        videos: list[str] = []
        video_previews: list[dict] = []

        for m in media or []:
            if hasattr(m, "video_info"):
                url = self._pick_video_url(m)
                if url:
                    videos.append(url)
                poster = str(getattr(m, "media_url", "") or "").strip()
                preview = {"poster": poster, "duration": self._format_duration(getattr(m, "duration_millis", 0))}
                if poster:
                    video_previews.append(preview)
            elif hasattr(m, "media_url"):
                url = self._build_image_url(getattr(m, "media_url", ""))
                if url:
                    images.append(url)

        return images, videos, video_previews

    @staticmethod
    def _pick_video_url(video_media) -> str:
        """从 video_info.variants 中选取最高码率的 mp4 直链。

        跳过 m3u8/vmap 流媒体清单（无法直发）。
        """
        video_info = getattr(video_media, "video_info", None) or {}
        variants = video_info.get("variants") or []

        mp4_candidates = []
        for v in variants:
            url = str(v.get("url", "") or "")
            content_type = str(v.get("content_type", "") or "")
            if not url:
                continue
            # 跳过流媒体清单
            lower = url.lower()
            if ".m3u8" in lower or "vmap" in lower:
                continue
            if content_type == "video/mp4":
                try:
                    bitrate = int(v.get("bitrate") or 0)
                except (TypeError, ValueError):
                    bitrate = 0
                mp4_candidates.append((bitrate, url))

        if mp4_candidates:
            mp4_candidates.sort(key=lambda x: x[0], reverse=True)
            return mp4_candidates[0][1]

        # 回退：任一非流媒体变体
        for v in variants:
            url = str(v.get("url", "") or "")
            if url and ".m3u8" not in url.lower() and "vmap" not in url.lower():
                return url
        return ""

    def _quote_to_dict(self, quote) -> Optional[dict]:
        if quote is None:
            return None
        q_user = getattr(quote, "user", None)
        images, videos, video_previews = self._extract_media(getattr(quote, "media", None))
        return {
            "author": str(getattr(q_user, "name", "") or "") if q_user else "",
            "username": str(getattr(q_user, "screen_name", "") or "") if q_user else "",
            "avatar": str(getattr(q_user, "profile_image_url", "") or "") if q_user else "",
            "verified": bool(getattr(q_user, "verified", False)) if q_user else False,
            "date": str(getattr(quote, "created_at", "") or ""),
            "tweet_id": str(getattr(quote, "id", "") or ""),
            "text": str(getattr(quote, "full_text", None) or getattr(quote, "text", "") or ""),
            "images": images,
            "videos": videos,
            "video_previews": video_previews,
        }

    def _tweet_to_info(self, tweet, fallback_username: str = "") -> dict:
        user = getattr(tweet, "user", None)
        username = str(getattr(user, "screen_name", "") or fallback_username) if user else fallback_username
        screen_name = str(getattr(user, "name", "") or username) if user else username

        images, videos, video_previews = self._extract_media(getattr(tweet, "media", None))

        text = str(getattr(tweet, "full_text", None) or getattr(tweet, "text", "") or "")

        stats = {
            "comments": self._format_count(getattr(tweet, "reply_count", None)),
            "retweets": self._format_count(getattr(tweet, "retweet_count", None)),
            "likes": self._format_count(getattr(tweet, "favorite_count", None)),
            "views": self._format_count(getattr(tweet, "view_count", None)),
        }

        return {
            "tweet_id": str(getattr(tweet, "id", "") or ""),
            "username": username,
            "screen_name": screen_name,
            "avatar": str(getattr(user, "profile_image_url", "") or "") if user else "",
            "verified": bool(getattr(user, "verified", False)) if user else False,
            "date": str(getattr(tweet, "created_at", "") or ""),
            "stats": stats,
            "text": text,
            "images": images,
            "videos": videos,
            "video_previews": video_previews,
            "quote": self._quote_to_dict(getattr(tweet, "quote", None)),
            "retweet": None,
            "is_r18": False,
        }
