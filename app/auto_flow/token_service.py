# -*- coding: utf-8 -*-
"""取图令牌：签发与校验（T-7）。

规格依据：`docs/spec/自动流程规格-v1.md`「Components · TokenService」「Data models ·
PhotoRecord」「Security」；任务合同 `docs/spec/自动流程规格-v1.md` T-7。

约束（逐字对齐规格）：
- 令牌恰好 22 个 URL-safe 字符（字母 / 数字 / ``-`` / ``_``），由安全随机源产生。
  实现取 ``secrets.token_urlsafe(16)``：16 字节 = 128 位熵，base64url 去掉填充后
  恒为 22 字符；``issue()`` 不接受任何输入，结构上就不存在「从编号、作业 ID 或
  个人信息推导」的入口；
- 显示编号 ``SM-YYYY-NNNN`` 永不作为访问凭据：``validate()`` 显式拒绝编号格式，
  即使把编号补零到 22 字符（如 ``SM-2026-00000000000001``）也一律 False，
  防止「把编号撑到令牌长度」的试探。

与既有代码的关系：``app/auto_flow/models.py`` 的 ``is_valid_photo_token`` 是共享的
格式工具，本模块在它之上叠加编号格式拒绝，供 ``PhotoService``（T-7）与路由接线
（T-16）使用。本模块不读环境变量、不写文件、不打印日志。
"""

from __future__ import annotations

import re
import secrets

from app.auto_flow.models import is_valid_photo_token

__all__ = [
    "TOKEN_LENGTH",
    "TokenService",
    "looks_like_display_number",
]

# 取图令牌长度（规格逐字：exactly 22 random URL-safe characters）
TOKEN_LENGTH = 22
# 随机字节数：16 字节 = 128 位熵 → token_urlsafe 恒为 22 字符，无需补/截
_TOKEN_BYTES = 16

# 显示编号 SM-YYYY-NNNN：大小写不敏感；序列号允许多位，覆盖被补齐成 22 字符的变体。
_DISPLAY_NUMBER_RE = re.compile(r"sm-\d{4}-\d+", re.IGNORECASE)


def looks_like_display_number(value: object) -> bool:
    """是否是显示编号 ``SM-YYYY-NNNN``（含大小写与补零变体；前后空白不计）。"""
    if not isinstance(value, str):
        return False
    return _DISPLAY_NUMBER_RE.fullmatch(value.strip()) is not None


class TokenService:
    """22 位取图令牌的签发与校验（规格 ``TokenService`` 接口）。

    无状态、线程安全，可跨请求共享一个实例。
    """

    def issue(self) -> str:
        """签发一个新令牌：恰好 22 字符、URL-safe，来自 ``secrets`` 安全随机源。"""
        while True:
            token = secrets.token_urlsafe(_TOKEN_BYTES)
            # token_urlsafe(16) 长度恒为 22；编号格式检查是兜底（随机撞上 SM-\d{4}-\d+ 的概率
            # 极低，但既然签发的令牌必须能通过 validate，这里就不让它发出去）。
            if len(token) == TOKEN_LENGTH and not looks_like_display_number(token):
                return token

    def validate(self, token: object) -> bool:
        """校验令牌格式；非字符串、长度不符、含非 URL-safe 字符、编号格式均 False。"""
        if looks_like_display_number(token):
            return False
        return is_valid_photo_token(token)
