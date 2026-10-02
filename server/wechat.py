"""微信登录 OAuth 封装

两条入口，共用一套 code → openid → 用户信息的交换逻辑：

1. 微信内：跳公众号网页授权 URL（snsapi_userinfo），用户点「同意」即登录。
2. 微信外：把同一条授权 URL 画成二维码，用户用微信扫后在手机里完成授权，
   网页侧轮询等待结果（state 做关联）。

AppSecret 只出现在本进程里，绝不返回给前端。

Mock 模式：没有 AppID / Secret 时用，用于把整条链路跑通验收。
设置 PINTU_WX_MOCK=1 后，任意 code 都能换出一个稳定的假 openid。
"""

import hashlib
import json
import os
import secrets
import time
import urllib.parse
import urllib.request

APPID = os.environ.get("PINTU_WX_APPID", "").strip()
SECRET = os.environ.get("PINTU_WX_SECRET", "").strip()
MOCK = os.environ.get("PINTU_WX_MOCK", "").strip().lower() in ("1", "true", "yes")
HTTP_TIMEOUT = 8

OAUTH_AUTHORIZE = "https://open.weixin.qq.com/connect/oauth2/authorize"
OAUTH_TOKEN = "https://api.weixin.qq.com/sns/oauth2/access_token"
OAUTH_USERINFO = "https://api.weixin.qq.com/sns/userinfo"

# state → 登录结果，仅存活几分钟，进程重启即失效（可接受的短态数据）
_poll_store: dict[str, dict] = {}
POLL_TTL = 300


def enabled() -> bool:
    """是否配置了真实凭据。Mock 模式下也算可用。"""
    return MOCK or bool(APPID and SECRET)


def is_mock() -> bool:
    return MOCK or not (APPID and SECRET)


def new_state() -> str:
    return secrets.token_urlsafe(24)


def build_authorize_url(callback_url: str, state: str) -> str:
    """公众号网页授权地址。微信内直跳，微信外画成二维码后由手机扫码打开。"""
    query = urllib.parse.urlencode({
        "appid": APPID,
        "redirect_uri": callback_url,
        "response_type": "code",
        "scope": "snsapi_userinfo",
        "state": state,
    })
    return f"{OAUTH_AUTHORIZE}?{query}#wechat_redirect"


def _http_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def exchange_code(code: str) -> dict:
    """code → openid。返回 {"openid": ..., "access_token": ...}，失败抛 WeChatError。"""
    query = urllib.parse.urlencode({
        "appid": APPID,
        "secret": SECRET,
        "code": code,
        "grant_type": "authorization_code",
    })
    data = _http_json(f"{OAUTH_TOKEN}?{query}")
    if data.get("errcode"):
        raise WeChatError(data.get("errmsg") or f"微信返回 {data.get('errcode')}")
    if not data.get("openid"):
        raise WeChatError("微信未返回 openid")
    return data


def fetch_profile(access_token: str, openid: str) -> dict:
    """拉取昵称与头像。头像去掉尾部的 /132，改成我们想要的大小。"""
    query = urllib.parse.urlencode({
        "access_token": access_token,
        "openid": openid,
        "lang": "zh_CN",
    })
    data = _http_json(f"{OAUTH_USERINFO}?{query}")
    if data.get("errcode"):
        raise WeChatError(data.get("errmsg") or f"微信返回 {data.get('errcode')}")

    avatar = (data.get("headimgurl") or "").strip()
    if avatar.endswith("/132"):
        avatar = avatar[: -len("/132")] + "/96"
    return {
        "nickname": (data.get("nickname") or "").strip(),
        "avatar_url": avatar,
        "unionid": (data.get("unionid") or "").strip(),
    }


def _mock_avatar(digest: str, text: str) -> str:
    """Mock 模式下造一张圆形头像，方便前端验证头像渲染链路。"""
    hue = int(digest[:2], 16) % 360
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' width='96' height='96'>"
        f"<rect width='96' height='96' rx='48' fill='hsl({hue},55%,52%)'/>"
        "<text x='48' y='50' font-size='38' fill='#fff' text-anchor='middle'"
        f" dominant-baseline='central' font-family='sans-serif'>{text}</text>"
        "</svg>"
    )
    return "data:image/svg+xml;utf8," + urllib.parse.quote(svg, safe="")


def _mock_profile(code: str) -> tuple[str, dict]:
    """给一个稳定的假身份，方便没有凭据时验收整条链路。"""
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    nickname = f"微信玩家{digest[:4]}"
    return f"mock_{digest[:10]}", {
        "nickname": nickname,
        "avatar_url": _mock_avatar(digest, nickname[-1:]),
        "unionid": "",
    }


def login_by_code(code: str) -> tuple[str, dict]:
    """入口。返回 (openid, profile)。profile 含 nickname / avatar_url / unionid。"""
    if is_mock():
        return _mock_profile(code)
    token = exchange_code(code)
    return token["openid"], fetch_profile(token.get("access_token", ""), token["openid"])


# ---------------------------------------------------------------- 扫码轮询

def save_poll_state(state: str) -> None:
    _poll_store[state] = {"ok": False, "created_at": time.time()}
    _drop_expired()


def mark_poll_done(state: str, **extra) -> None:
    """手机端授权成功后回调。extra 里可以塞给网页端的东西（如一次性凭证）。"""
    item = _poll_store.get(state)
    if item is not None:
        item["ok"] = True
        item.update(extra)


def read_poll_state(state: str) -> dict | None:
    """返回 {"ok": bool, ...}；None = state 不存在或已过期。"""
    _drop_expired()
    item = _poll_store.get(state)
    return dict(item) if item is not None else None


def _now() -> float:
    return time.time()


def _drop_expired() -> None:
    deadline = _now() - POLL_TTL
    for key in [k for k, v in _poll_store.items() if v.get("created_at", 0) < deadline]:
        _poll_store.pop(key, None)


class WeChatError(Exception):
    """微信接口返回错误，或凭据缺失导致无法换取身份。"""
