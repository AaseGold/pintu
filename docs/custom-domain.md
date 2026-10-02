# 自定义域名接入说明（pintu.21times.com）

> 目标：让用户访问 `pintu.21times.com` 就能进到拼图游戏，且云登录 / 全球排行榜 / 云图库全部正常。

---

## 零、当前域名现状（2026-09-21 实测）

| 主机记录 | 解析到 | 说明 |
|---|---|---|
| `21times.com` | `49.233.91.28` | 腾讯云轻量应用服务器 |
| `www.21times.com` | `49.233.91.28` | 同上 |
| `pintu.21times.com` | `1.71.87.19` | 腾讯云 CDN / EdgeOne 节点，此前配过 CDN |

服务器当前直接访问返回 **502 Bad Gateway**——web 服务在跑，但后端进程没起来。走 DNS 转发方案时用不到这台服务器；走 Nginx 方案则要先把它修好。

> 注意：还有一个不带 s 的 `21time.com`，指向境外 IP 且挂的是"域名转让"停放页。**那是另一个域名**，别配错。

---

## 一、结论

**只能用「URL 转发」，不能用 CNAME。**

| 做法 | 页面能开 | 云功能 | 说明 |
|---|---|---|---|
| CNAME 指向 `pintu.app.workbuddy.host` | ❌ | ❌ | 见第二节 |
| A 记录指向某台服务器 + 把页面搬过去 | ✅ | ❌ | 搬过去就等于自托管，云服务用不了 |
| **URL 转发（跳转）** | ✅ | ✅ | **唯一可行方案** |
| 不做自定义域名，直接用官方域名 | ✅ | ✅ | 零配置 |

---

## 二、为什么 CNAME 不行

云服务的每个请求，服务端会校验 **Origin**——就是浏览器地址栏里那个域名，必须和发布时登记的域名**精确一致**。

关键在于 **CNAME 只在 DNS 层生效**：

```
用户输入 pintu.21times.com
    ↓ DNS 查询
CNAME → pintu.app.workbuddy.host        ← 这一步服务器之间发生，浏览器"看不见"
    ↓
浏览器地址栏仍然显示 pintu.21times.com   ← Origin 是这个
    ↓
云服务校验 Origin：pintu.21times.com ≠ pintu.app.workbuddy.host
    ↓
拒绝，登录/排行榜/上传全部失效
```

而 **URL 转发是 HTTP 层的跳转**：服务器返回一个 302，浏览器**真的导航过去**，地址栏变成 `pintu.app.workbuddy.host`，Origin 就对了。

一句话：**DNS 层的转发改不了 Origin，HTTP 层的跳转才能改。**

---

## 三、配置步骤

以腾讯云 DNSPod 为例（其他注册商逻辑相同，界面名称可能略有差异）：

1. 进入 **云解析 DNS → 域名解析 → `21times.com` → 解析设置**
2. 添加一条记录：

   | 项 | 值 |
   |---|---|
   | 主机记录 | `pintu` |
   | 记录类型 | **显性 URL** |
   | 线路类型 | **默认**（选其他会导致部分地区无法解析） |
   | 记录值 | `https://pintu.app.workbuddy.host/` |
   | 权重 / MX 优先级 | 不用填 |
   | TTL | 默认 600 即可 |

   注意：**记录值必须是完整地址**（带协议），且**不支持填写 IP 或 IP + 端口**。

3. 单击【保存】，等解析生效（通常几分钟，最长 48 小时）
4. 生效后访问 `http://pintu.21times.com/` 应自动跳到 `https://pintu.app.workbuddy.host/`

> ⚠️ **备案要求（官方硬性限制）**
> - 转发前域名和转发后域名**都必须已备案**，没备案的域名无法添加这条记录。
> - 转发**前**域名（`21times.com`）需在**腾讯云完成备案或接入备案**后才可正常使用。当初若是在其他接入商备的案，需要先做「接入备案」到腾讯云，否则转发加了也可能不生效。
> - 免费版套餐最多 2 条 URL 转发记录。
> - 云解析 DNS 不为 URL 转发提供攻击防护，遇攻击黑洞时转发会失效。重要场景官方建议改用**自建 Nginx** 实现转发（方案见第五节）。

---

## 四、显性转发 vs 隐性转发 —— 选显性

| 类型 | 原理 | 地址栏 | 云功能 |
|---|---|---|---|
| **显性转发（301 重定向）** | 服务器返回 301，浏览器导航过去 | 变成官方域名 | ✅ 完全正常 |
| 隐性转发（frame） | 用 `<iframe>` 把目标页嵌在当前页面里 | 仍是 `pintu.21times.com` | ⚠️ 有风险 |

隐性转发虽然地址栏好看，但页面实际跑在 iframe 里**跨域**环境。Safari、Firefox 以及开启"阻止跨站跟踪"的 Chrome 会拦截第三方 Cookie，导致**登录态存不住**（每次刷新都掉线）。

**所以选显性转发。** 代价是地址栏会变成官方域名——这是托管类平台的通病，无法绕过。

---

## 五、怎么加 HTTPS（关键）

### 先搞清楚限制

DNSPod 官方文档原话：

> URL 转发记录，**转发前地址仅支持 HTTP、不支持 HTTPS**；转发后地址支持 HTTP 及 HTTPS。转发前地址的支持，DNSPod 控制台提供了**一键申请免费 SSL 证书**功能。

对应到本项目：

| 访问方式 | 结果 |
|---|---|
| `http://pintu.21times.com` | ✅ 正常跳转到官方域名 |
| `https://pintu.21times.com` | ❌ **默认打不开**（转发前地址不支持 HTTPS） |

第二条是隐患——现在浏览器地址栏输入域名多数会自动尝试 HTTPS，Chrome 还有 HSTS 预加载，用户很可能根本走不到 HTTP。**所以这一步必须做。**

### 方案 A：DNSPod 一键申请免费 SSL 证书（推荐，省事）

1. 登录 **DNSPod 控制台 → 我的域名 → `21times.com` → 记录管理**
2. 找到 SSL 证书入口（控制台里叫「一键申请免费 SSL 证书」），按引导申请
3. 验证域名所有权，通常走 **DNS 验证**：控制台会要求在解析里加一条 TXT 记录，按提示加就行（部分情况可自动完成）
4. 签发完成后绑定到 `pintu.21times.com`
5. 等几分钟，访问 `https://pintu.21times.com/` 应不再报证书错误，并跳转到官方域名

> 官方文档入口：腾讯云《一键申请免费 SSL 证书》https://cloud.tencent.com/document/product/400/54336

注意事项：

- 免费 DV 证书**有有效期**，到期会失效。留意控制台提示，提前续期，别等用户报"打不开"才发现。
- 证书是给**转发前地址**用的，只负责让用户能安全地走到这一步；真正的游戏内容仍然由官方域名承载。
- 如果申请时提示域名未在腾讯云备案/接入备案，先解决备案问题。

### 方案 B：自建 Nginx 转发（更可控，适合长期用）

如果你手上有服务器（例如腾讯云轻量应用服务器），也可以不用 URL 转发：

1. 把 `pintu.21times.com` 的 **A 记录**指向服务器公网 IP
2. 服务器上用 nginx 做跳转：

   ```nginx
   server {
       listen 80;
       listen 443 ssl;
       server_name pintu.21times.com;

       # 证书用 certbot 签发，自动续期
       ssl_certificate     /etc/letsencrypt/live/pintu.21times.com/fullchain.pem;
       ssl_certificate_key /etc/letsencrypt/live/pintu.21times.com/privkey.pem;

       location / {
           return 301 https://pintu.app.workbuddy.host$request_uri;
       }
   }
   ```

3. `certbot --nginx -d pintu.21times.com` 申请 Let's Encrypt 证书，自动续期不用管

优点：HTTPS 完全可控、不受 URL 转发条数和攻击黑洞限制、到期自动续。
缺点：要维护一台服务器。

**怎么选**：只是想让自定义域名能用 → 方案 A；已经在跑服务器、且希望长期稳定 → 方案 B。

---

## 六、验收清单

| # | 检查项 | 预期 |
|---|---|---|
| 1 | 浏览器访问 `http://pintu.21times.com/` | 自动跳转到 `https://pintu.app.workbuddy.host/` |
| 2 | 地址栏最终显示 | 官方域名（显性转发的正常表现） |
| 3 | 点「登录」→ 注册 → 登录 | 成功，顶栏变「退出」，刷新不掉线 |
| 4 | 通关一局 | 提示「成绩已上榜」 |
| 5 | 排行榜 | 显示「全球排行」，自己那条高亮 |
| 6 | 上传图片 | 提示「图片已存入云端」 |
| 7 | 用手机 4G 访问 | 同样能跳转、能登录 |

任何一项失败，先看是跳转没生效（DNS 问题）还是云功能报错（Origin 问题）——前者查解析，后者看地址栏是不是官方域名。

---

## 七、已知限制

1. **地址栏会变成官方域名**，跳转后无法保持 `pintu.21times.com`。
2. **不能把应用托管在自己的域名下还同时用云服务**——Origin 不匹配是硬约束，没有配置项可以放开。
3. 想要"域名是自己的 + 数据也在自己手里"，只有一条路：**完全自托管**（服务器 + 自建数据库/存储），但那就意味着放弃 WorkBuddy 云服务，后端要自己写。

---

## 八、相关文档

- `docs/cloud-plan.md` —— 云服务的表结构、RLS、代码改动点
- 应用管理入口：WorkBuddy **设置 — 数据管理 — 应用**

---

## 九、将来接自己的服务端后怎么切

现在走跳转是为了让域名**立刻可用**。等你自己的服务端上线后，可以平滑接管，不用推倒重来：

**切换步骤**

1. 服务端实现三个能力（对照现在云服务的分工）：

   | 能力 | 现在由谁提供 | 自己实现要点 |
   |---|---|---|
   | 注册 / 登录 / 会话 / 改密码 | 云 Auth | 密码需加盐哈希存储，会话用 httpOnly Cookie 或 JWT |
   | 成绩读写 + 排行榜查询 | 云 Database | **表结构可直接照搬** `cloud-plan.md` 第四节，字段都对齐好了 |
   | 图片上传 / 下载 | 云 Storage | 按用户 ID 分目录，做好类型和大小校验 |

2. 前端把云调用换成对自己服务端的请求。改动是**集中**的，就这几处：`createWorkBuddyCloud` 初始化、`initCloudAuth`、`afterSignIn`、`loadCloudRanks`、`pushScoreToCloud`、`pushImageToCloud`、`syncCloudName`。

3. DNS 从「URL 转发」改回 **A 记录**指向服务器（Nginx 方案则把 301 改成直接托管静态文件）。

4. 证书继续用 certbot，此后**地址栏全程保持** `pintu.21times.com`。

**一个要提前想清楚的坑**

云服务的现有成绩数据**没有导出 API**——一旦决定迁移，只能通过对话让我把表数据读出来给你，属于一次性人工操作，表多了会很麻烦。

所以：

- 如果确定将来要自建，**越早切越好**，别等积累了大量用户数据再迁。
- 过渡期内如果积累了数据，迁移前先列一下 `scores` 表有多少行，评估导出成本。

---

## 十、实际部署记录（2026-09-21 完成）

走的是**第五节方案 B（自建 Nginx 转发）**，已全部实施并验证通过。

### 环境

| 项 | 值 |
|---|---|
| 服务器 | 腾讯云轻量应用服务器，公网 `49.233.91.28`，内网 `10.2.0.16` |
| 系统 | Ubuntu 24.04.4 LTS（内核 6.8.0） |
| 配置 | 2 GB 内存 / 50 GB 磁盘 |
| 登录 | 用户 `ubuntu` + SSH 私钥，sudo 免密 |

### 装了什么

```bash
sudo apt update
sudo apt install -y nginx                          # 1.24.0
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d 21times.com -d www.21times.com \
     --non-interactive --agree-tos --register-unsafely-without-email
```

证书：`/etc/letsencrypt/live/21times.com/`，**有效期至 2026-12-20**，certbot 已配置自动续期（走 80 端口的 HTTP-01）。

### 改了什么

`/etc/nginx/sites-enabled/default` 的**第 121 行**（443 server 块内的 `location /`）：

```nginx
location / {
    return 301 https://pintu.app.workbuddy.host$request_uri;
}
```

原配置备份：`/root/default.bak-pintu`

> ⚠️ **两个坑**
> 1. 备份文件**不要**放在 `/etc/nginx/sites-enabled/` 下——nginx 会 include 整个目录，会报 `duplicate default server`。
> 2. 文件里有多处 `try_files`（80 端口块、被注释的示例块、443 块），必须只改 **443 块那一处**。80 端口由 certbot 管理的 http→https 规则保持原样，否则自动续期会被破坏。

### 云平台侧

腾讯云控制台 → 实例 → **防火墙**，需放行：

| 协议 | 端口 | 来源 |
|---|---|---|
| TCP | 80 | 0.0.0.0/0 |
| TCP | **443** | 0.0.0.0/0 |

（80 默认已放行，**443 必须手动加**，否则 https 超时。）

### 验证结果

```
https://21times.com/      -> 301 -> https://pintu.21times.com/
https://www.21times.com/  -> 301 -> https://pintu.21times.com/
http://21times.com/       -> 301 -> https://21times.com/  -> 301 -> https://pintu.21times.com/

（2026-09-21 更新：自托管上线后，主域跳转已从官方云版改为 pintu.21times.com，第十节其余记录保留作历史。）
```

排障命令：

```bash
sudo nginx -t                      # 改配置前必跑
sudo systemctl reload nginx        # 平滑重载
sudo ss -lntp | grep -E ':80|:443' # 看监听
curl -sk -D - https://127.0.0.1/ -H "Host: 21times.com" | grep -i location
sudo certbot renew --dry-run       # 验证自动续期能跑通
```


## 十一、自托管 pintu.21times.com 部署记录（2026-09-21 完成）

第十节的 301 跳转方案只是过渡。最终采用「一步到位自托管」：注册登录、排行榜、图片上传全部跑在自己的轻量服务器上，地址栏固定为 https://pintu.21times.com/ 。

### 架构

- 前端：/opt/pintu/static/（index.html 62.5KB 自托管版 + assets/）
- 后端：FastAPI + SQLite（/opt/pintu/app.py，uvicorn 127.0.0.1:8000，systemd 服务 pintu.service）
- 反代：nginx 站点 /etc/nginx/sites-enabled/pintu（443 + HTTP→HTTPS 跳转）
- 证书：Let's Encrypt pintu.21times.com（ECDSA，2026-12-20 到期，certbot 自动续期）
- DNS：DNSPod A 记录 pintu → 49.233.91.28（原 CNAME 指向 EdgeOne Pages，已删除）

### API 一览

POST /api/auth/register、/api/auth/login、/api/auth/logout；GET /api/auth/me；
PUT /api/me/name；POST /api/scores；GET /api/rankings?size=N；
POST /api/images、GET /api/images、DELETE /api/images/{name}；GET /api/health。

会话：JWT 写入 httpOnly Cookie（pintu_session，Secure + SameSite=Lax，30 天）。

### 踩过的坑

1. FastAPI Cookie 别名：Cookie(default=None) 按参数名找 cookie，参数名 session 但 cookie 叫 pintu_session，必须写 alias="pintu_session"，否则登录成功但后续请求全部当作未登录。
2. scores 请求体用下划线（duration_ms），前端 seconds 是 Math.floor 后的整数。
3. curl -F 上传文件默认 Content-Type 是 application/octet-stream，测试要加 ;type=image/webp，前端 fetch blob 上传不受影响。
4. pkill -f uvicorn 会误杀自己的 SSH 会话，重启服务一律用 systemctl restart pintu。

### 端到端验证（全部通过）

注册 → 登录 → me → 上榜 → 排行榜 → 改名 → 图片上传 → 图片列表 → 登出。
测试数据（test1@21times.com）验证后已清理，users/scores/uploads 均为 0。
