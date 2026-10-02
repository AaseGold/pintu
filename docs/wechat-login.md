# 微信登录（云端部分）

> 状态：**服务端已实现并通过 26 项 Mock 链路验证**，等待真实 AppID 上线。

---

## 一、为什么跑在自托管，而不是 Serverless

这不是偏好问题，是三条硬约束叠加的结果：

| 约束 | 说明 |
|---|---|
| Serverless Auth 只有邮箱 | 不支持第三方登录、自定义令牌、二维码登录，微信用户在里面没有身份；数据库 RLS 全靠 `auth.uid()`，微信用户拿不到 |
| `code → openid` 必须有服务端 | AppSecret 绝不能进前端，Serverless **没有云函数** |
| 微信要求在授权域名根目录放校验文件 | `pintu.21times.com` 已备案且 nginx 在我们手里，可以放 `MP_verify_*.txt`；托管的云域名做不到 |

结论：**微信登录这件事必须有服务端**，项目已有的那台腾讯云 + FastAPI 正好满足全部前置条件。

---

## 二、数据表

不用新建表，老库会自动迁移（幂等，重复执行无害）：

| 列 | 作用 |
|---|---|
| `provider` | `'local'` 邮箱注册 / `'wechat'` 微信 |
| `provider_uid` | 微信的 openid |
| `avatar_url` | 微信头像 |
| `updated_at` | 最近一次登录时间 |
| `idx_users_identity` | `(provider, provider_uid)` 唯一索引，一个微信号只对应一个账号 |

微信用户的 `email` 填 `<openid>@wechat.local`（只为满足原有的 NOT NULL UNIQUE），`password_hash` 为空，永远不能密码登录。

**改名的权威值是本地侧的**：每次登录只刷新头像，**不覆盖** `name`，这样玩家在游戏里改过的名字会一直保留，二次登录时拉回来的就是它。

---

## 三、环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `PINTU_WX_APPID` | 上线必填 | 公众号 AppID |
| `PINTU_WX_SECRET` | 上线必填 | 公众号 AppSecret |
| `PINTU_PUBLIC_BASE` | 建议填 | 站点对外根地址，如 `https://pintu.21times.com`。不填则取请求 origin |
| `PINTU_WX_MOCK` | 调试用 | `1` 时任意 code 都能换出稳定的假身份，用于没凭据时验收链路 |
| `PINTU_COOKIE_SECURE` | 调试用 | 本地 http 调试设 `0`，线上保持 `1` |

systemd 里加（`/etc/systemd/system/pintu.service` 的 `[Service]`）：

```ini
Environment=PINTU_WX_APPID=wxXXXXXXXXXXXXXXXX
Environment=PINTU_WX_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
Environment=PINTU_PUBLIC_BASE=https://pintu.21times.com
```

改完 `sudo systemctl daemon-reload && sudo systemctl restart pintu`。

---

## 四、接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/auth/wechat/start` | 取授权地址 + state（微信内直跳，微信外画成二维码） |
| GET | `/api/auth/wechat/callback` | 微信回调落点，建号/更新并下发会话 Cookie |
| GET | `/api/auth/wechat/poll?state=` | 网页端轮询扫码结果，成功后返回一次性 `exchange` |
| POST | `/api/auth/wechat/claim` | 扫码的浏览器用 `exchange` 换自己的会话 |
| GET | `/api/auth/me` | 返回 `id / name / avatar_url / provider` |
| PUT | `/api/me/name` | 改名，同步历史成绩的显示名 |

扫码那条链路为什么要多一步 `claim`：授权发生在**手机微信**里，会话 Cookie 落在手机上，电脑浏览器的轮询只能拿到一次性凭证，再换自己的会话。凭证一次性，用完即焚。

---

## 五、微信后台还要配什么（上线前必做）

1. **公众号 → 设置 → 公众号设置 → 功能设置 → 网页授权域名**，填 `pintu.21times.com`。
2. 下载校验文件 `MP_verify_xxxxxxxx.txt`，上传到 nginx 静态根目录，确保
   `https://pintu.21times.com/MP_verify_xxxxxxxx.txt` 能直接访问到文件内容。
3. 未认证订阅号拿不到昵称头像，只能拿到 openid——要有完整头像昵称，需要**已认证服务号**，或先用**测试号**验收。
4. 想做电脑端原生扫码而不用 QR 二维码兜底，需要开放平台网站应用（付费认证 + 审核），届时只需替换 `/api/auth/wechat/start` 返回的地址。

---

## 六、验证

```bash
# Mock 模式起服务
cd server
PINTU_BASE=/tmp/pintu-data PINTU_WX_MOCK=1 PINTU_COOKIE_SECURE=0 PINTU_SECRET=xxx \
  python -m uvicorn app:app --port 8111

# 端到端链路（首次登录 / 改名 / 二次登录拉回名字 / 扫码轮询 / 凭证一次性）
python tmp/test_wechat_login.py      # 17 项

# 头像与昵称同步规则（头像刷新 / 空头像不清库 / 名字不被覆盖）
python tmp/test_identity_sync.py     # 9 项
```

两个脚本都返回 0 即通过。

---

## 七、踩过的坑

1. FastAPI 里显式 `return Response 实例`时，注入的 `response` 参数上设的 Cookie **会被丢弃**，必须在真正返回的那个响应对象上调 `set_cookie`。
2. `ALTER TABLE ADD COLUMN` 没有 `IF NOT EXISTS`，迁移要先查 `PRAGMA table_info` 再决定是否加列。
3. 微信头像 URL 尾部的 `/132` 是尺寸，替换成 `/96` 更省流量；接口偶尔返回空头像，更新时要保留旧值。
