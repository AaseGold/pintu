"""手机号验证码发送适配层。

验证码的生成、频率限制和校验由 app.py 负责；本模块只负责调用腾讯云短信。
设置 PINTU_SMS_MOCK=1 可在本地测试，真实环境必须配置完整的腾讯云短信参数。
"""

import os


SECRET_ID = os.environ.get("PINTU_SMS_SECRET_ID", "").strip()
SECRET_KEY = os.environ.get("PINTU_SMS_SECRET_KEY", "").strip()
SDK_APP_ID = os.environ.get("PINTU_SMS_SDK_APP_ID", "").strip()
SIGN_NAME = os.environ.get("PINTU_SMS_SIGN_NAME", "").strip()
TEMPLATE_ID = os.environ.get("PINTU_SMS_TEMPLATE_ID", "").strip()
REGION = os.environ.get("PINTU_SMS_REGION", "ap-guangzhou").strip()
MOCK = os.environ.get("PINTU_SMS_MOCK", "").strip().lower() in ("1", "true", "yes")


def enabled() -> bool:
    return MOCK or all((SECRET_ID, SECRET_KEY, SDK_APP_ID, SIGN_NAME, TEMPLATE_ID))


def is_mock() -> bool:
    return MOCK


def send_code(phone: str, code: str, minutes: int = 5) -> None:
    if MOCK:
        return
    if not enabled():
        raise SmsError("服务端未配置短信验证码")

    try:
        from tencentcloud.common import credential
        from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
        from tencentcloud.sms.v20210111 import models, sms_client
    except ImportError as exc:
        raise SmsError("服务端缺少腾讯云短信 SDK") from exc

    try:
        client = sms_client.SmsClient(
            credential.Credential(SECRET_ID, SECRET_KEY),
            REGION,
        )
        request = models.SendSmsRequest()
        request.SmsSdkAppId = SDK_APP_ID
        request.SignName = SIGN_NAME
        request.TemplateId = TEMPLATE_ID
        request.TemplateParamSet = [code, str(minutes)]
        request.PhoneNumberSet = [f"+86{phone}"]
        response = client.SendSms(request)
        statuses = response.SendStatusSet or []
        if not statuses or statuses[0].Code != "Ok":
            message = statuses[0].Message if statuses else "短信服务未返回发送结果"
            raise SmsError(message)
    except SmsError:
        raise
    except TencentCloudSDKException as exc:
        raise SmsError(str(exc)) from exc
    except Exception as exc:
        raise SmsError("短信服务请求失败") from exc


class SmsError(Exception):
    pass
