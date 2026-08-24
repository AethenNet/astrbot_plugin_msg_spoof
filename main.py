"""
QQ消息伪装插件 (astrbot_plugin_msg_spoof)
作者: SummerDew
版本: 1.0
功能: 伪装QQ用户消息，以聊天记录转发形式发送
"""

import asyncio
import os
import re
import shutil
import time
from pathlib import Path

import aiohttp
from astrbot.api.all import *
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.api.message_components import (
    Plain,
    Image,
    Node,
    Nodes,
    Face,
)


# ==================== 详细伪装会话状态 ====================
class DetailSession:
    """详细伪装的多轮会话状态"""

    STAGE_WAIT_QQ = "wait_qq"
    STAGE_WAIT_FIRST_MSG = "wait_first_msg"
    STAGE_WAIT_MORE = "wait_more"

    def __init__(self, user_id: str, group_id: str):
        self.user_id = user_id
        self.group_id = group_id
        self.stage = self.STAGE_WAIT_QQ
        self.current_qq = None
        self.messages = []
        self.timeout_task = None
        self.last_content_preview = ""

    def reset(self):
        if self.timeout_task:
            self.timeout_task.cancel()
            self.timeout_task = None
        self.stage = self.STAGE_WAIT_QQ
        self.current_qq = None
        self.messages = []
        self.last_content_preview = ""


# ==================== 插件主类 ====================
@register(
    "msg_spoof",
    "SummerDew",
    "QQ消息伪装插件，支持快捷/详细伪装，以聊天记录转发形式发送",
    "1.0.0",
    "https://github.com/AethenNet/astrbot_plugin_msg_spoof",
)
class MsgSpoofPlugin(Star):
    """QQ消息伪装插件主类"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.detail_sessions: dict[str, DetailSession] = {}
        self.TIMEOUT = 30
        # 临时图片存储目录（用于保存下载的图片，避免QQ临时URL失效）
        self.temp_image_dir = Path(os.getcwd()) / "data" / "msg_spoof" / "temp_images"
        self.temp_image_dir.mkdir(parents=True, exist_ok=True)
        logger.info("[msg_spoof] QQ消息伪装插件已初始化")

    # ==================== 配置辅助方法 ====================

    def _get_group_list(self, key: str) -> list[str]:
        """从 template_list 格式的配置中提取群号列表"""
        raw = self.config.get(key, [])
        if not isinstance(raw, list):
            return []
        result = []
        for item in raw:
            if isinstance(item, dict):
                gid = item.get("group_id", "").strip()
                if gid:
                    result.append(gid)
            elif isinstance(item, str) and item.strip():
                result.append(item.strip())
        return result

    def _set_group_list(self, key: str, groups: list[str]):
        """将群号列表写回 template_list 格式的配置"""
        self.config[key] = [
            {"__template_key": "group", "group_id": gid} for gid in groups
        ]
        self.config.save_config()

    @property
    def _blacklist_mode(self) -> bool:
        return bool(self.config.get("blacklist_mode", True))

    @_blacklist_mode.setter
    def _blacklist_mode(self, value: bool):
        self.config["blacklist_mode"] = value
        self.config.save_config()

    @property
    def _blacklist(self) -> list[str]:
        return self._get_group_list("blacklist")

    @property
    def _whitelist(self) -> list[str]:
        return self._get_group_list("whitelist")

    def _is_group_allowed(self, group_id: str) -> bool:
        if self._blacklist_mode:
            return group_id not in self._blacklist
        else:
            return group_id in self._whitelist

    # ==================== 工具方法 ====================

    async def get_qq_nickname(self, qq_number: str) -> str:
        """获取QQ用户昵称，失败则返回QQ号本身"""
        apis = [
            f"http://api.mmp.cc/api/qqname?qq={qq_number}",
            f"https://api.usuuu.com/qq/{qq_number}",
        ]
        for url in apis:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            nickname = self._extract_nickname(data)
                            if nickname:
                                return nickname
            except Exception as e:
                logger.debug(f"[msg_spoof] 获取昵称API失败 ({url}): {e}")
                continue
        return str(qq_number)

    def _extract_nickname(self, data: dict) -> str | None:
        if not isinstance(data, dict):
            return None
        if data.get("code") == 200 and isinstance(data.get("data"), dict):
            name = data["data"].get("name") or data["data"].get("nickname")
            if name:
                return str(name)
        if data.get("code") == 200 and data.get("name"):
            return str(data["name"])
        if isinstance(data.get("data"), dict):
            name = data["data"].get("nickname") or data["data"].get("name")
            if name:
                return str(name)
        return None

    def _is_qq_number(self, text: str) -> bool:
        return bool(re.match(r"^\d{5,11}$", text))

    def _get_content_preview(self, content_type: str, content) -> str:
        if content_type == "text":
            text = str(content)
            return text if len(text) <= 20 else text[:20] + "..."
        elif content_type == "image":
            return "图片1"
        elif content_type == "face":
            return "[表情]"
        return str(content)

    def _format_message_summary(self, messages: list) -> str:
        """
        格式化消息统计，按用户分组统计每种消息类型的数量
        输出格式:
        <QQ号> 图片*1 表情*3
        <QQ号> 文本*1
        """
        if not messages:
            return ""
        type_names = {"text": "文本", "image": "图片", "face": "表情"}
        user_stats = {}
        user_order = []
        for qq_number, content_type, _ in messages:
            if qq_number not in user_stats:
                user_stats[qq_number] = {}
                user_order.append(qq_number)
            user_stats[qq_number][content_type] = user_stats[qq_number].get(content_type, 0) + 1
        lines = []
        for qq_number in user_order:
            stats = user_stats[qq_number]
            type_order = ["text", "image", "face"]
            parts = []
            for t in type_order:
                if t in stats:
                    name = type_names.get(t, t)
                    parts.append(f"{name}*{stats[t]}")
            lines.append(f"<{qq_number}> {' '.join(parts)}")
        return "\n".join(lines)

    # 单次消息大小限制：10MB
    MAX_MESSAGE_SIZE = 10 * 1024 * 1024

    def _is_super_admin(self, event) -> bool:
        """
        判断是否为 AstrBot 管理员（超级管理员）
        AstrBot 标准方式：event.is_admin()
        参考多个开源插件的实现
        """
        # 优先使用 AstrBot 标准的 is_admin() 方法
        if hasattr(event, 'is_admin'):
            try:
                return bool(event.is_admin())
            except Exception as e:
                logger.debug(f"[msg_spoof] is_admin() 调用失败: {e}")
        # 兜底：从上下文配置中获取管理员 UID 列表
        try:
            user_id = str(event.get_sender_id())
            config = getattr(self.context, 'config', None)
            if config:
                for attr_name in [
                    'admin_qq', 'super_admin_qq', 'owner_qq', 'master_qq',
                    'admin', 'super_admin', 'owner', 'master',
                ]:
                    val = getattr(config, attr_name, None)
                    if val:
                        if isinstance(val, (list, tuple)):
                            if user_id in [str(x) for x in val]:
                                return True
                        elif str(val) == user_id:
                            return True
                for attr_name in [
                    'admin_qq_list', 'super_admins', 'admin_list', 'admins',
                    'super_admin_list', 'owner_list', 'owners', 'master_list', 'masters',
                ]:
                    val = getattr(config, attr_name, None)
                    if val and isinstance(val, (list, tuple)):
                        if user_id in [str(x) for x in val]:
                            return True
        except Exception:
            pass
        return False

    def _check_message_size(self, content_type: str, content) -> tuple[bool, str]:
        """
        检查消息大小是否超过限制
        返回 (是否通过, 错误信息)
        """
        try:
            if content_type == "text":
                size = len(str(content).encode('utf-8'))
                if size > self.MAX_MESSAGE_SIZE:
                    return False, f"❌ 文本消息过大（{size // 1024 // 1024}MB），超过10MB限制"
            elif content_type == "image":
                if isinstance(content, dict) and content.get("local_path"):
                    if os.path.exists(content["local_path"]):
                        size = os.path.getsize(content["local_path"])
                        if size > self.MAX_MESSAGE_SIZE:
                            # 删除过大的临时文件
                            try:
                                os.remove(content["local_path"])
                            except Exception:
                                pass
                            return False, f"❌ 图片过大（{size // 1024 // 1024}MB），超过10MB限制"
        except Exception as e:
            logger.debug(f"[msg_spoof] 检查消息大小失败: {e}")
        return True, ""

    def _extract_image_info(self, image_comp) -> dict:
        """从Image组件提取图片URL和file ID（仅使用URL创建组件，file ID仅用于API获取URL）"""
        info = {"url": None, "file": None, "local_path": None}
        try:
            url = getattr(image_comp, "url", None)
            if url and isinstance(url, str) and url.startswith(("http://", "https://")):
                info["url"] = url
                logger.info(f"[msg_spoof] 提取到图片URL: {url}")
            else:
                logger.warning(f"[msg_spoof] 图片URL无效或为空: url={url}")
            file = getattr(image_comp, "file", None)
            if file:
                info["file"] = str(file)
        except Exception as e:
            logger.debug(f"[msg_spoof] 提取图片信息失败: {e}")
        return info

    async def _download_image_to_local(self, image_comp) -> str | None:
        """
        将图片下载到本地，返回本地文件路径。
        优先使用 convert_to_file_path()，失败则用 URL 下载。
        """
        try:
            # 方式1: 使用 AstrBot 提供的 convert_to_file_path()
            if hasattr(image_comp, 'convert_to_file_path'):
                logger.info("[msg_spoof] 尝试使用 convert_to_file_path() 获取图片")
                temp_path = await image_comp.convert_to_file_path()
                if temp_path and os.path.exists(temp_path):
                    # 复制到插件目录，避免临时文件被清理
                    filename = f"img_{int(time.time() * 1000)}_{os.path.basename(temp_path)}"
                    dest_path = self.temp_image_dir / filename
                    shutil.copy2(temp_path, str(dest_path))
                    logger.info(f"[msg_spoof] 图片已保存到本地: {dest_path}")
                    return str(dest_path)

            # 方式2: 使用 URL 下载
            url = getattr(image_comp, "url", None)
            if url and isinstance(url, str) and url.startswith(("http://", "https://")):
                logger.info(f"[msg_spoof] 尝试通过URL下载图片: {url}")
                filename = f"img_{int(time.time() * 1000)}.jpg"
                dest_path = self.temp_image_dir / filename
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            with open(dest_path, 'wb') as f:
                                f.write(data)
                            logger.info(f"[msg_spoof] 图片已通过URL下载到本地: {dest_path}")
                            return str(dest_path)
        except Exception as e:
            logger.error(f"[msg_spoof] 下载图片失败: {e}", exc_info=True)
        return None

    def _extract_face_info(self, face_comp) -> dict:
        """从Face组件提取可持久化的表情信息"""
        info = {"id": None, "file": None}
        try:
            face_id = getattr(face_comp, "id", None)
            if face_id:
                info["id"] = str(face_id)
            file = getattr(face_comp, "file", None)
            if file:
                info["file"] = str(file)
        except Exception as e:
            logger.debug(f"[msg_spoof] 提取表情信息失败: {e}")
        return info

    def _build_image_component(self, image_info: dict):
        """从图片信息字典构建Image组件（优先本地文件，其次URL）"""
        # 优先使用本地文件路径（最可靠）
        if image_info.get("local_path") and os.path.exists(image_info["local_path"]):
            try:
                return Image.fromFileSystem(image_info["local_path"])
            except Exception as e:
                logger.debug(f"[msg_spoof] Image.fromFileSystem失败: {e}")
        # 其次使用URL
        if image_info.get("url"):
            try:
                return Image.fromURL(image_info["url"])
            except Exception as e:
                logger.debug(f"[msg_spoof] Image.fromURL失败: {e}")
        return None

    def _build_face_component(self, face_info: dict):
        """从表情信息字典构建Face组件"""
        if face_info.get("id"):
            try:
                return Face(id=face_info["id"])
            except Exception as e:
                logger.debug(f"[msg_spoof] Face(id=)失败: {e}")
        if face_info.get("file"):
            try:
                return Face(file=face_info["file"])
            except Exception as e:
                logger.debug(f"[msg_spoof] Face(file=)失败: {e}")
        return None

    # ==================== 消息解析 ====================

    async def _parse_message_components(self, event: AstrMessageEvent, command_prefix: str) -> list:
        components = []
        prefix_skipped = False
        try:
            if hasattr(event.message_obj, "message"):
                for comp in event.message_obj.message:
                    if isinstance(comp, Plain):
                        text = comp.text
                        if not prefix_skipped and command_prefix:
                            prefixes = [command_prefix] + command_prefix.split("|")
                            prefixes = sorted(set(prefixes), key=len, reverse=True)
                            for prefix in prefixes:
                                if prefix and text.startswith(prefix):
                                    text = text[len(prefix):].lstrip()
                                    prefix_skipped = True
                                    break
                        if text.strip():
                            components.append({"type": "text", "content": text})
                    elif isinstance(comp, Image):
                        # 提取图片信息并下载到本地，避免QQ临时URL失效
                        img_info = self._extract_image_info(comp)
                        local_path = await self._download_image_to_local(comp)
                        if local_path:
                            img_info["local_path"] = local_path
                            logger.info(f"[msg_spoof] 图片本地路径: {local_path}")
                        components.append({"type": "image", "content": img_info})
                    elif isinstance(comp, Face):
                        # 提取可持久化的表情信息
                        face_info = self._extract_face_info(comp)
                        components.append({"type": "face", "content": face_info})
        except Exception as e:
            logger.error(f"[msg_spoof] 解析消息组件失败: {e}", exc_info=True)
        return components

    def _parse_quick_spoof(self, components: list) -> tuple[list, str]:
        """
        解析快捷伪装消息，返回 (消息列表, 错误信息)
        错误信息为空表示成功
        """
        result = []
        current_qq = None
        flat_tokens = []
        for comp in components:
            if comp["type"] == "text":
                for w in comp["content"].split():
                    flat_tokens.append({"type": "text", "content": w})
            else:
                flat_tokens.append(comp)
        for token in flat_tokens:
            if token["type"] == "text":
                text = token["content"]
                if self._is_qq_number(text):
                    if current_qq is None or any(m[0] == current_qq for m in result):
                        current_qq = text
                        continue
                if current_qq:
                    # 检查消息大小
                    ok, err = self._check_message_size("text", text)
                    if not ok:
                        return [], err
                    result.append((current_qq, "text", text))
            else:
                if current_qq:
                    # 检查消息大小
                    ok, err = self._check_message_size(token["type"], token["content"])
                    if not ok:
                        return [], err
                    result.append((current_qq, token["type"], token["content"]))
        return result, ""

    # ==================== 生成转发消息 ====================

    async def _build_forward_message(self, messages: list) -> Nodes | None:
        if not messages:
            return None
        nodes_list = []
        nickname_cache = {}
        for qq_number, content_type, content in messages:
            if qq_number not in nickname_cache:
                nickname_cache[qq_number] = await self.get_qq_nickname(qq_number)
            nickname = nickname_cache[qq_number]
            node_content = []
            if content_type == "text":
                node_content.append(Plain(str(content)))
            elif content_type == "image":
                try:
                    if isinstance(content, dict):
                        logger.info(f"[msg_spoof] 处理图片: url={content.get('url')}, local_path={content.get('local_path')}")
                        img_comp = self._build_image_component(content)
                        if img_comp:
                            node_content.append(img_comp)
                        else:
                            logger.warning(f"[msg_spoof] 图片组件创建失败，降级为[图片]占位符")
                            node_content.append(Plain("[图片]"))
                    elif isinstance(content, Image):
                        node_content.append(content)
                    else:
                        node_content.append(Image.fromURL(str(content)))
                except Exception as e:
                    logger.error(f"[msg_spoof] 添加图片失败: {e}", exc_info=True)
                    node_content.append(Plain("[图片]"))
            elif content_type == "face":
                try:
                    if isinstance(content, dict):
                        face_comp = self._build_face_component(content)
                        if face_comp:
                            node_content.append(face_comp)
                        else:
                            node_content.append(Plain("[表情]"))
                    elif isinstance(content, Face):
                        node_content.append(content)
                    else:
                        node_content.append(Face(id=str(content)))
                except Exception:
                    node_content.append(Plain("[表情]"))
            if not node_content:
                continue
            try:
                node = Node(uin=int(qq_number), name=nickname, content=node_content)
                nodes_list.append(node)
            except Exception as e:
                logger.error(f"[msg_spoof] 创建Node失败: {e}")
        if not nodes_list:
            return None
        try:
            return Nodes(nodes=nodes_list)
        except Exception as e:
            logger.error(f"[msg_spoof] 创建Nodes失败: {e}")
            return None

    # ==================== 权限检查 ====================

    def _check_group_permission(self, event: AstrMessageEvent) -> tuple[bool, str]:
        try:
            group_id = event.get_group_id()
        except Exception:
            return True, ""
        if not group_id:
            return True, ""
        if self._is_group_allowed(group_id):
            return True, ""
        if self._blacklist_mode:
            return False, f"❌ 本群({group_id})已被拉黑，无法使用该插件"
        else:
            return False, f"❌ 本群({group_id})不在白名单中，无法使用该插件"

    # ==================== 指令：快捷伪装 ====================

    @filter.command("伪装")
    async def quick_spoof(self, event: AstrMessageEvent):
        try:
            allowed, msg = self._check_group_permission(event)
            if not allowed:
                yield event.plain_result(msg)
                return
            components = await self._parse_message_components(event, "伪装|伪装消息")
            if not components:
                yield event.plain_result(
                    "❌ 格式错误！\n"
                    "使用方法：/伪装 <QQ号> <文本|图片|表情> <QQ号> <文本|图片|表情> ...\n"
                    "示例：/伪装 12345678 你好 123456789 你好！"
                )
                return
            messages, parse_error = self._parse_quick_spoof(components)
            if parse_error:
                yield event.plain_result(parse_error)
                return
            if not messages:
                yield event.plain_result(
                    "❌ 未能解析出有效的消息节点\n"
                    "请确保格式正确：/伪装 <QQ号> <消息内容> ..."
                )
                return
            forward_msg = await self._build_forward_message(messages)
            if forward_msg:
                yield event.chain_result([forward_msg])
            else:
                yield event.plain_result("❌ 生成转发消息失败，请检查消息内容是否有效")
        except Exception as e:
            logger.error(f"[msg_spoof] 快捷伪装出错: {e}", exc_info=True)
            yield event.plain_result(f"❌ 生成转发消息时发生错误：{str(e)[:200]}")

    @filter.command("伪装消息")
    async def quick_spoof_alias(self, event: AstrMessageEvent):
        async for result in self.quick_spoof(event):
            yield result

    # ==================== 指令：详细伪装 ====================

    @filter.command("伪装x")
    async def detail_spoof_start(self, event: AstrMessageEvent):
        allowed, msg = self._check_group_permission(event)
        if not allowed:
            yield event.plain_result(msg)
            return
        user_id = event.get_sender_id()
        if user_id in self.detail_sessions:
            self.detail_sessions[user_id].reset()
        session = DetailSession(user_id, event.get_group_id() or "")
        self.detail_sessions[user_id] = session
        session.timeout_task = asyncio.create_task(self._detail_timeout(user_id, event))
        reply = (
            "消息详细伪装，请发送伪装QQ号\n"
            f"⏱剩余时间：{self.TIMEOUT}秒"
        )
        yield event.plain_result(reply)

    @filter.command("伪装x消息")
    async def detail_spoof_alias1(self, event: AstrMessageEvent):
        async for result in self.detail_spoof_start(event):
            yield result

    @filter.command("伪装消息x")
    async def detail_spoof_alias2(self, event: AstrMessageEvent):
        async for result in self.detail_spoof_start(event):
            yield result

    async def _detail_timeout(self, user_id: str, event: AstrMessageEvent):
        try:
            await asyncio.sleep(self.TIMEOUT)
            session = self.detail_sessions.get(user_id)
            if session:
                session.reset()
                del self.detail_sessions[user_id]
                await event.send(event.plain_result("⌛指令使用超时！"))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[msg_spoof] 超时任务出错: {e}")

    def _reset_detail_timeout(self, user_id: str, event: AstrMessageEvent):
        session = self.detail_sessions.get(user_id)
        if not session:
            return
        if session.timeout_task:
            session.timeout_task.cancel()
        session.timeout_task = asyncio.create_task(self._detail_timeout(user_id, event))

    async def _handle_detail_message(self, event: AstrMessageEvent):
        try:
            user_id = event.get_sender_id()
            session = self.detail_sessions.get(user_id)
            if not session:
                return False
            components = await self._parse_message_components(event, "")
            text_content = " ".join(
                c["content"] for c in components if c["type"] == "text"
            ).strip()
            if text_content in ("结束", "完成", "生成", "ok", "OK"):
                await self._finish_detail_spoof(event, session)
                return True
            self._reset_detail_timeout(user_id, event)
            if session.stage == DetailSession.STAGE_WAIT_QQ:
                await self._detail_stage_wait_qq(event, session, components)
            elif session.stage == DetailSession.STAGE_WAIT_FIRST_MSG:
                await self._detail_stage_wait_first_msg(event, session, components)
            elif session.stage == DetailSession.STAGE_WAIT_MORE:
                await self._detail_stage_wait_more(event, session, components)
            return True
        except Exception as e:
            logger.error(f"[msg_spoof] 处理详细伪装消息时出错: {e}", exc_info=True)
            try:
                await event.send(event.plain_result(f"❌ 处理消息时发生错误：{str(e)[:200]}"))
            except Exception:
                pass
            return False

    async def _detail_stage_wait_qq(self, event, session: DetailSession, components: list):
        qq_number = None
        for comp in components:
            if comp["type"] == "text":
                tokens = comp["content"].split()
                if tokens and self._is_qq_number(tokens[0]):
                    qq_number = tokens[0]
                    break
        if not qq_number:
            reply = (
                "❌ 请输入有效的QQ号（纯数字，5-11位）\n"
                "消息详细伪装，请发送伪装QQ号\n"
                f"⏱剩余时间：{self.TIMEOUT}秒"
            )
            await event.send(event.plain_result(reply))
            return
        session.current_qq = qq_number
        session.stage = DetailSession.STAGE_WAIT_FIRST_MSG
        reply = (
            "消息详细伪装\n"
            f"<用户{qq_number}>\n"
            "请发送：文本|图片|表情\n"
            f"⏱剩余时间：{self.TIMEOUT}秒"
        )
        await event.send(event.plain_result(reply))

    async def _detail_stage_wait_first_msg(self, event, session: DetailSession, components: list):
        # 如果没有有效组件，直接忽略（避免空事件误报）
        if not components:
            return
        msg_added = False
        for comp in components:
            if comp["type"] == "text":
                for token in comp["content"].split():
                    if token.strip():
                        # 检查消息大小
                        ok, err = self._check_message_size("text", token)
                        if not ok:
                            await event.send(event.plain_result(err))
                            return
                        session.messages.append((session.current_qq, "text", token))
                        session.last_content_preview = self._get_content_preview("text", token)
                        msg_added = True
            else:
                # 检查消息大小
                ok, err = self._check_message_size(comp["type"], comp["content"])
                if not ok:
                    await event.send(event.plain_result(err))
                    return
                session.messages.append((session.current_qq, comp["type"], comp["content"]))
                session.last_content_preview = self._get_content_preview(comp["type"], comp["content"])
                msg_added = True
        if not msg_added:
            # 没有有效消息内容，直接忽略（不回复错误，避免干扰）
            return
        session.stage = DetailSession.STAGE_WAIT_MORE
        summary = self._format_message_summary(session.messages)
        reply = (
            f"已收集{len(session.messages)}条消息\n"
            f"{summary}\n"
            "请发送：QQ号|文本|图片|表情\n\n"
            "(新QQ号可添加伪装用户，发送「结束」生成转发消息)\n"
            f"⏱剩余时间：{self.TIMEOUT}秒"
        )
        await event.send(event.plain_result(reply))

    async def _detail_stage_wait_more(self, event, session: DetailSession, components: list):
        # 如果没有有效组件，直接忽略（避免空事件误报）
        if not components:
            return
        for comp in components:
            if comp["type"] == "text":
                for token in comp["content"].split():
                    if not token.strip():
                        continue
                    if self._is_qq_number(token):
                        session.current_qq = token
                        session.last_content_preview = f"切换到用户{token}"
                    else:
                        # 检查消息大小
                        ok, err = self._check_message_size("text", token)
                        if not ok:
                            await event.send(event.plain_result(err))
                            return
                        session.messages.append((session.current_qq, "text", token))
                        session.last_content_preview = self._get_content_preview("text", token)
            else:
                # 检查消息大小
                ok, err = self._check_message_size(comp["type"], comp["content"])
                if not ok:
                    await event.send(event.plain_result(err))
                    return
                session.messages.append((session.current_qq, comp["type"], comp["content"]))
                session.last_content_preview = self._get_content_preview(comp["type"], comp["content"])
        summary = self._format_message_summary(session.messages)
        reply = (
            f"已收集{len(session.messages)}条消息\n"
            f"{summary}\n"
            "请发送：QQ号|文本|图片|表情\n\n"
            "(新QQ号可添加伪装用户，发送「结束」生成转发消息)\n"
            f"⏱剩余时间：{self.TIMEOUT}秒"
        )
        await event.send(event.plain_result(reply))

    async def _finish_detail_spoof(self, event: AstrMessageEvent, session: DetailSession):
        try:
            if session.timeout_task:
                session.timeout_task.cancel()
            if session.user_id in self.detail_sessions:
                del self.detail_sessions[session.user_id]
            if not session.messages:
                await event.send(event.plain_result("❌ 没有收集到任何消息，已取消"))
                return
            forward_msg = await self._build_forward_message(session.messages)
            if forward_msg:
                await event.send(event.chain_result([forward_msg]))
            else:
                await event.send(event.plain_result("❌ 生成转发消息失败，请检查消息内容是否有效"))
        except Exception as e:
            logger.error(f"[msg_spoof] 完成详细伪装时出错: {e}", exc_info=True)
            try:
                await event.send(event.plain_result(f"❌ 生成转发消息时发生错误：{str(e)[:50]}"))
            except Exception:
                pass

    # ==================== 指令：帮助 ====================

    @filter.command("伪装消息帮助")
    async def help_command(self, event: AstrMessageEvent):
        help_text = (
            "1️⃣  快捷伪装\n"
            "    ▸ 命令: /伪装 <用户1QQ号> <文本|图片|表情> <用户2QQ号> <文本|图片|表情>\n"
            "    ▸ 作用: 快速伪装消息\n"
            "    ▸ 示例: /伪装 12345678 你好 123456789 你好！\n\n"
            "2️⃣  详细伪装\n"
            "    ▸ 命令: /伪装x \n"
            "    ▸ 作用: 启用详细伪装，支持多轮对话添加消息\n\n"
            "3️⃣  查看帮助 \n"
            "    ▸ 命令: /伪装消息帮助\n"
            "    ▸ 作用: 查看本菜单\n\n"
            "4️⃣  群黑名单 \n"
            "    ▸ 命令: /群黑名单\n"
            "    ▸ 作用: 查看被拉黑的群聊\n\n"
            "5️⃣  群白名单 \n"
            "    ▸ 命令: /群白名单\n"
            "    ▸ 作用: 查看白名单群聊\n\n"
            "6️⃣  黑白名单群切换\n"
            "    ▸ 命令: /伪装群名单 黑名单|白名单\n"
            "    ▸ 作用: 切换拉黑模式"
        )
        try:
            bot_qq = event.get_self_id()
            node = Node(
                uin=int(bot_qq) if bot_qq else 0,
                name="消息伪装助手",
                content=[Plain(help_text)],
            )
            nodes = Nodes(nodes=[node])
            yield event.chain_result([nodes])
        except Exception as e:
            logger.error(f"[msg_spoof] 帮助转发消息失败: {e}")
            yield event.plain_result(help_text)

    # ==================== 指令：群黑名单 ====================

    @filter.command("群黑名单")
    async def show_blacklist(self, event: AstrMessageEvent):
        if not self._is_super_admin(event):
            yield event.plain_result("❌ 您没有权限使用此指令，仅超级管理员可以操作")
            return
        blacklist = self._blacklist
        if not blacklist:
            reply = "📋 当前黑名单为空\n（没有群被拉黑）"
        else:
            groups = "\n".join(f"  • {gid}" for gid in blacklist)
            mode = "黑名单模式" if self._blacklist_mode else "白名单模式"
            reply = f"📋 黑名单群聊（当前模式：{mode}）\n{groups}"
        yield event.plain_result(reply)

    # ==================== 指令：群白名单 ====================

    @filter.command("群白名单")
    async def show_whitelist(self, event: AstrMessageEvent):
        if not self._is_super_admin(event):
            yield event.plain_result("❌ 您没有权限使用此指令，仅超级管理员可以操作")
            return
        whitelist = self._whitelist
        if not whitelist:
            reply = "📋 当前白名单为空\n（没有群被加入白名单）"
        else:
            groups = "\n".join(f"  • {gid}" for gid in whitelist)
            mode = "黑名单模式" if self._blacklist_mode else "白名单模式"
            reply = f"📋 白名单群聊（当前模式：{mode}）\n{groups}"
        yield event.plain_result(reply)

    # ==================== 指令：切换黑白名单模式 ====================

    @filter.command("伪装群名单")
    async def switch_group_mode(self, event: AstrMessageEvent):
        if not self._is_super_admin(event):
            yield event.plain_result("❌ 您没有权限使用此指令，仅超级管理员可以操作")
            return
        message_str = event.message_str
        params = ""
        for prefix in ["/伪装群名单", "伪装群名单", "/伪装群设置", "伪装群设置"]:
            if message_str.startswith(prefix):
                params = message_str[len(prefix):].strip()
                break
        if not params:
            current_mode = "黑名单" if self._blacklist_mode else "白名单"
            reply = (
                f"📋 当前模式：{current_mode}模式\n\n"
                "使用方法：\n"
                "  /伪装群名单 黑名单  — 切换为黑名单模式\n"
                "  /伪装群名单 白名单  — 切换为白名单模式\n\n"
                "说明：\n"
                "  黑名单模式：除黑名单中的群外，其他群均可使用\n"
                "  白名单模式：仅白名单中的群可以使用"
            )
            yield event.plain_result(reply)
            return
        if params in ("黑名单", "blacklist", "黑"):
            self._blacklist_mode = True
            yield event.plain_result("✅ 已切换为【黑名单模式】\n（除黑名单中的群外，其他群均可使用）")
        elif params in ("白名单", "whitelist", "白"):
            self._blacklist_mode = False
            yield event.plain_result("✅ 已切换为【白名单模式】\n（仅白名单中的群可以使用）")
        else:
            yield event.plain_result("❌ 参数错误！请使用：/伪装群名单 黑名单|白名单")

    @filter.command("伪装群设置")
    async def switch_group_mode_alias(self, event: AstrMessageEvent):
        async for result in self.switch_group_mode(event):
            yield result

    # ==================== 事件监听器 ====================

    @event_message_type(EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        try:
            user_id = event.get_sender_id()
            if user_id in self.detail_sessions:
                message_str = event.message_str.strip()
                command_prefixes = [
                    "/伪装", "伪装", "/伪装x", "伪装x",
                    "/伪装消息帮助", "伪装消息帮助",
                    "/群黑名单", "群黑名单",
                    "/群白名单", "群白名单",
                    "/伪装群名单", "伪装群名单",
                ]
                is_command = any(message_str.startswith(p) for p in command_prefixes)
                if not is_command:
                    handled = await self._handle_detail_message(event)
                    if handled:
                        return
        except Exception as e:
            logger.error(f"[msg_spoof] 事件监听处理出错: {e}")

    # ==================== 插件终止 ====================

    async def terminate(self):
        for session in self.detail_sessions.values():
            if session.timeout_task:
                session.timeout_task.cancel()
        self.detail_sessions.clear()
        # 清理临时图片目录
        try:
            if self.temp_image_dir.exists():
                shutil.rmtree(str(self.temp_image_dir))
                logger.info("[msg_spoof] 临时图片目录已清理")
        except Exception as e:
            logger.error(f"[msg_spoof] 清理临时图片目录失败: {e}")
        logger.info("[msg_spoof] QQ消息伪装插件已终止")
