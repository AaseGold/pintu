# 游客成绩与账号绑定

## 当前行为

- 用户打开页面即可游玩，游客名称和各难度 Top5 成绩保存在浏览器本地。
- 点击顶部“登录”或排行榜里的“绑定账号保住成绩”，进入绑定页。
- 手机验证码和 QQ 登录成功后，本机成绩会补传云端；`client_id` 保证重复登录不会重复插入。
- 微信入口当前只展示“暂未开通”。

## 手机号验证码

页面只会把验证码登录成功的手机号保存到当前浏览器的 `puzzle-phone-history-v1` 本地记录中，用于下次登录时提供下拉选择。最近使用的号码排在前面，最多保留 5 个；未登录成功的普通输入不会被记录。

服务端使用腾讯云短信 SDK，需要配置：

```ini
PINTU_SMS_SECRET_ID=腾讯云SecretId
PINTU_SMS_SECRET_KEY=腾讯云SecretKey
PINTU_SMS_SDK_APP_ID=短信应用SdkAppId
PINTU_SMS_SIGN_NAME=已审核短信签名
PINTU_SMS_TEMPLATE_ID=已审核验证码模板ID
PINTU_SMS_REGION=ap-guangzhou
```

短信模板参数顺序必须为“验证码、有效分钟数”，例如：

```text
{1}为您的登录验证码，请于{2}分钟内填写，如非本人操作，请忽略本短信。
```

当前限制：60 秒才能重发；同一手机号每天最多 10 次；同一 IP 每天最多 30 次；验证码 5 分钟过期，最多尝试 5 次。

本地联调可设置 `PINTU_SMS_MOCK=1`，接口会返回 `debugCode`，页面自动填入。线上禁止开启。

## QQ 登录

在 QQ 互联创建网站应用，将回调地址配置为：

```text
https://pintu.21times.com/api/auth/qq/callback
```

服务端配置：

```ini
PINTU_QQ_APP_ID=QQ互联AppID
PINTU_QQ_APP_KEY=QQ互联AppKey
PINTU_PUBLIC_BASE=https://pintu.21times.com
```

本地联调可设置 `PINTU_QQ_MOCK=1`。OAuth `state` 只允许使用一次，10 分钟过期。

## 测试

```powershell
python tmp/test_binding_auth.py
python tmp/test_identity_sync.py
```
