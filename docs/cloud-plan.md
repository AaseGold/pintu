# 「拼了个图」数据上云方案

> 状态：**方案已定，代码未改**。云服务环境已开通，数据库表尚未创建。

---

## 一、现状盘点

| 数据 | 存储位置 | 代码位置（`index.html`） | 问题 |
|---|---|---|---|
| 玩家身份 `{id, name}` | `localStorage` key `puzzle-user-v1` | L184–L186 `createUser()` / `getOrCreateUser()` | 随机生成，清缓存即丢 |
| 各难度排行榜 Top5 | `localStorage` key `puzzle-rankings-v1` | L192 / L231 `readRanks()` | 只有本机成绩 |
| 上次难度 | `localStorage` key `puzzle-last-difficulty-v1` | L188 / L229 | 换设备丢失 |
| 内置图库 | 仓库 `assets/*.webp` + 根目录 4 张 | L202 / L206 | 随包发布，游客可见 |
| 用户上传的图 | **内存**，转 dataURL 存 `state.image` | L224 `#fileInput.onchange` | **刷新即丢失** |

上传流程现状：选图 → 校验 ≤15MB → 居中裁正方 → 缩放至 ≤1600px → `canvas.toDataURL('image/jpeg', .88)` → 存进 `state.image`。
**这里产出的 dataURL 正好是要上云的素材**，不需要另外保存原图。

---

## 二、目标架构

```
浏览器 index.html（单文件，无构建）
   │
   ├── CDN 引入 @tencent-ai/workbuddy-cloud-sdk@dev
   │      一次 createWorkBuddyCloud({ endpoint, publishableKey })
   │
   ├── Auth     邮箱密码 / 邮箱验证码登录  ──► 拿到 session.user.id
   │
   ├── Database scores 表
   │      ├── 写：登录后每局成绩 insert（RLS 只允许写自己的）
   │      └── 读：公开读，按难度取 Top5 = 全球榜
   │
   └── Storage  runtime bucket
          ├── 上传：users/<uid>/uploads/<uuid>.jpg（裁剪后的图）
          └── 读取：createSignedUrl（1 小时有效）做缩略图
```

---

## 三、已开通的云环境

| 项 | 值 |
|---|---|
| 应用名 | 拼了个图 |
| applicationId | `wbapp_mW9ip48i9aC3K6w7CuarAn` |
| resourceId | `wbcs_rZ4BvuVD2QYw4oMiimOt9t` |
| endpoint | `https://pintu.app.workbuddy.host` |
| publishableKey | `wbpk_mW9ip48i9aC3K6w7CuarAn_uJBitR9Sv3amQV4jldJPNTUmWY07sQxp` |
| 计费状态 | normal / provisionStatus assigned |

`endpoint` 与 `publishableKey` 是**唯二**可以写进前端代码的凭据，不含任何权限，靠服务端强校验 Origin。
底层 envId、provider key 全部留在服务端，前端拿不到也不需要。

---

## 四、建表（按顺序逐条执行，一条一条来）

`scores` —— 成绩表，公开读 + 本人写。

```sql
CREATE TABLE scores (
  id           BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  owner_id     TEXT        NOT NULL DEFAULT auth.uid(),
  owner_name   TEXT,
  size         INT         NOT NULL,
  seconds      INT         NOT NULL,
  duration_ms  INT         NOT NULL,
  moves        INT         NOT NULL,
  image_name   TEXT,
  played_at    TIMESTAMPTZ NOT NULL DEFAULT now()
)
```

```sql
COMMENT ON TABLE scores IS '拼图成绩：公开读取的全球排行榜，仅本人可写'
```

```sql
ALTER TABLE scores ENABLE ROW LEVEL SECURITY
```

```sql
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.scores TO authenticated, anon
```

```sql
DROP POLICY IF EXISTS scores_read_all ON scores
CREATE POLICY scores_read_all ON scores FOR SELECT TO authenticated, anon USING (true)
```

```sql
DROP POLICY IF EXISTS scores_insert_own ON scores
CREATE POLICY scores_insert_own ON scores FOR INSERT TO authenticated, anon WITH CHECK (owner_id = auth.uid())
```

```sql
DROP POLICY IF EXISTS scores_update_own ON scores
CREATE POLICY scores_update_own ON scores FOR UPDATE TO authenticated, anon USING (owner_id = auth.uid()) WITH CHECK (owner_id = auth.uid())
```

```sql
DROP POLICY IF EXISTS scores_delete_own ON scores
CREATE POLICY scores_delete_own ON scores FOR DELETE TO authenticated, anon USING (owner_id = auth.uid())
```

设计要点：

- `owner_id` 必须是 **TEXT**（`auth.uid()` 返回文本），声明成 uuid 会在建表时类型不匹配报错。
- 前端 **绝不传 `owner_id`**，由 `DEFAULT auth.uid()` 服务端填充；`WITH CHECK` 挡住伪造。
- `owner_name` 只是显示用昵称，改名时批量 update，不参与鉴权。
- "上次难度"建议**留在 localStorage**（纯偏好，不值得多一张表；阶段二再考虑 `profiles` 表）。

---

## 五、云存储设计

| 项 | 方案 |
|---|---|
| 目录 | `users/<uid>/uploads/<uuid>.jpg`（SDK 用 `cloud.storage.userPath(uid, 'uploads/xxx.jpg')` 生成） |
| 内容 | L224 裁剪后的 1600px 正方 JPEG，单张约 300–600KB |
| 权限 | 仅本人读写，无公开直链 |
| 读取 | `createSignedUrl(path, 3600)`，签名 URL 有效期 1–3600 秒 |

**关键工程细节**：签名 URL 会过期，不能拿来当游戏中的 `state.image` 背景（玩到一半过期会白屏）。
正确做法是**双轨**：

- 上传：把 dataURL 转 Blob 传到云端（持久化 + 跨设备）；
- 游玩：本地继续用 `state.image` 的 dataURL 渲染，不依赖网络；
- "我的图库"：列表时用 `createSignedUrls` 生成缩略图 URL，点选后再 `download()` 转 dataURL 开局。

---

## 六、代码改动清单

| # | 位置 | 改动 |
|---|---|---|
| 1 | `<head>` 末尾 | 加 CDN `<script src=".../index.global.js">`，全局 `WorkBuddyCloud` |
| 2 | 常量区（L184 前） | 初始化 `cloud = WorkBuddyCloud.createWorkBuddyCloud({ endpoint, publishableKey })`，全局唯一实例 |
| 3 | 新增 `#authScreen` | 登录页：密码登录 + 验证码登录 + 注册（验证码后带密码）+ 忘记密码，四件套都要有 |
| 4 | L186 `getOrCreateUser()` | 改 `resolveUser()`：先 `getSession()`，有 session 用 `session.user.id` + 云端昵称；无 session 走原随机逻辑（游客） |
| 5 | L231 `readRanks()` | 排行榜页读云端 `.eq('size', n).order('duration_ms').order('moves').limit(5)`；游客时显示本机榜并提示"登录后上榜" |
| 6 | L232 `writeRank()` | 双写：本地保留 + 登录后 `insert` 云端（不传 `owner_id`） |
| 7 | L224 `#fileInput.onchange` | dataURL 生成后，若已登录则异步上传云端，失败给 toast 不阻断游戏 |
| 8 | 新增「我的图库」 | `list('users/<uid>/uploads')` + 缩略图 + 删除（`remove([path])`） |
| 9 | L234 `renameUser()` | 登录后改名同步 `.update({ owner_name }).eq('owner_id', uid).select()`；**返回空数组代表被 RLS 拦了，要提示，不能当成功** |

核心片段（可直接落地）：

```js
// 2. 初始化（唯一一处，两个参数都必填）
const cloud = WorkBuddyCloud.createWorkBuddyCloud({
  endpoint: 'https://pintu.app.workbuddy.host',
  publishableKey: 'wbpk_mW9ip48i9aC3K6w7CuarAn_uJBitR9Sv3amQV4jldJPNTUmWY07sQxp',
})

// 4/6. 写成绩前的登录门禁
const { data: session } = await cloud.auth.getSession()
if (!session) { showAuthScreen(); return }

await cloud.database.from('scores').insert({
  size: state.size,
  seconds: state.seconds,
  duration_ms: Math.round(state.elapsedMs),
  moves: state.moves,
  image_name: currentImageName,   // 注意：不要写 owner_id
})

// 5. 读榜单
const { data, error } = await cloud.database.from('scores')
  .select('owner_id,owner_name,seconds,duration_ms,moves,played_at')
  .eq('size', state.size)
  .order('duration_ms', { ascending: true })
  .order('moves', { ascending: true })
  .limit(5)

// 7. 上传图片
const path = cloud.storage.userPath(session.user.id, `uploads/${crypto.randomUUID()}.jpg`)
const blob = await (await fetch(state.image)).blob()      // dataURL → Blob
const { error } = await cloud.storage.upload(path, blob, { contentType: 'image/jpeg', upsert: false })
```

---

## 七、游客态怎么处理

不做"静默降级"。两种身份在 UI 上必须可分辨：

- **游客**：照常玩，成绩只进本机榜，排行榜页顶部提示「登录后可上榜，与全球玩家比拼」。
- **登录用户**：成绩同时进云端，排行榜显示全球 Top5，可用「我的图库」。

匿名登录、假 session、localStorage 冒充身份一律不做——云服务的登录只认真实邮箱验证。

---

## 八、发布（必须做，否则登录跑不通）

云服务登录**只在已发布的 HTTPS 域名生效**，`file://` 打开或 localhost 都不行。

发布时用 `workbuddy_sites_deploy`，**必须复用** `appId = wbapp_mW9ip48i9aC3K6w7CuarAn`，
不能 `createNewApp` —— 新建应用会换域名，Origin 对不上，云登录直接失效。

---

## 九、验证清单

1. 注册（邮箱验证码 + 设密码）→ 登录成功 → 刷新页面 session 仍在。
2. 玩一局 3×3 → `scores` 表多一行 → 排行榜页能看到这条，且 `owner_id` 是自己。
3. 换一个账号登录 → 排行榜能看到上一个账号的成绩（公开读），改不了它（RLS）。
4. 上传图 → 云端 `users/<uid>/uploads/` 出现文件 → 图库缩略图能显示 → 删除成功。
5. 未登录上传图 → 弹出登录页，而不是静默失败。
6. 断网 / 未登录 / 额度不足 → 都有明确提示，不死循环、不白屏。

---

## 十、风险与成本

| 项 | 说明 |
|---|---|
| SDK 通道 | 固定用 `@dev`，不要锁版本也不要用 `@latest`（`latest` 未维护，指向旧构建） |
| 内置图库 | **建议继续留在仓库**。云存储没有公开直链，游客读不到，搬上去会导致未登录开局空白 |
| 图片体积 | 现逻辑已裁到 1600px / JPEG .88，单张约 300–600KB，存储压力很小 |
| 内置图仓库占比 | 根目录 4 张约 4.5MB + `assets/` 约 1MB，纯前端包可接受，不必上云 |
| 匿名写榜 | `anon` 角色有 `WITH CHECK (owner_id = auth.uid())` 兜底，未登录时 `auth.uid()` 为空，写入会被拒，前端需提前拦截 |
