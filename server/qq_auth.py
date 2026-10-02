"""QQ 互联 OAuth 封装。AppSecret 只在服务端使用。"""

import hashlib
import json
import os
import urllib.parse
import urllib.request


APP_ID = os.environ.get("PINTU_QQ_APP_ID", "").strip()
APP_KEY = os.environ.get("PINTU_QQ_APP_KEY", "").strip()
MOCK = os.environ.get("PINTU_QQ_MOCK", "").strip().lower() in ("1", "true", "yes")
HTTP_TIMEOUT = 8

AUTHORIZE_URL = "https://graph.qq.com/oauth2.0/authorize"
TOKEN_URL = "https://graph.qq.com/oauth2.0/token"
ME_URL = "https://graph.qq.com/oauth2.0/me"
PROFILE_URL = "https://graph.qq.com/user/get_user_info"


def enabled() -> bool:
    return MOCK or bool(APP_ID and APP_KEY)


def is_mock() -> bool:
    return MOCK


def build_authorize_url(callback_url: str, state: str) -> str:
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": APP_ID,
        "redirect_uri": callback_url,
        "state": state,
        "scope": "get_user_info",
    })
    return f"{AUTHORIZE_URL}?{query}"


def _read(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "pintu/1.0"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return response.read().decode("utf-8")


def _json_or_callback(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("callback"):
        text = text[text.find("(") + 1:text.rfind(")")].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        query = urllib.parse.parse_qs(text)
        if query:
            return {key: values[0] for key, values in query.items()}
        raise QQError("QQ 返回了无法识别的数据")


def login_by_code(code: str, callback_url: str) -> tuple[str, dict]:
    if MOCK:
        digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
        return f"mock_{digest[:16]}", {
            "nickname": f"QQ玩家{digest[:4]}",
            "avatar_url": "",
        }

    try:
        token_query = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "client_id": APP_ID,
            "client_secret": APP_KEY,
            "code": code,
            "redirect_uri": callback_url,
            "fmt": "json",
        })
        token_data = _json_or_callback(_read(f"{TOKEN_URL}?{token_query}"))
        access_token = token_data.get("access_token")
        if not access_token:
            raise QQError(token_data.get("error_description") or "QQ 未返回 access_token")

        me_query = urllib.parse.urlencode({"access_token": access_token, "fmt": "json"})
        me_data = _json_or_callback(_read(f"{ME_URL}?{me_query}"))
        openid = me_data.get("openid")
        if not openid:
            raise QQError(me_data.get("error_description") or "QQ 未返回 openid")

        profile_query = urllib.parse.urlencode({
            "access_token": access_token,
            "oauth_consumer_key": APP_ID,
            "openid": openid,
            "fmt": "json",
        })
        profile = _json_or_callback(_read(f"{PROFILE_URL}?{profile_query}"))
        if profile.get("ret") not in (None, 0):
            raise QQError(profile.get("msg") or "QQ 用户资料获取失败")
        return openid, {
            "nickname": (profile.get("nickname") or "").strip(),
            "avatar_url": (profile.get("figureurl_qq_2") or profile.get("figureurl_qq_1") or "").strip(),
        }
    except QQError:
        raise
    except Exception as exc:
        raise QQError("QQ 服务连接失败，请稍后重试") from exc


class QQError(Exception):
    pass
