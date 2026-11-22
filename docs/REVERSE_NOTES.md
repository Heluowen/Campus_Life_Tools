# 登录链路逆向记录与结论（静默刷新可行性评估）

> 日期：2026-11-20 · 分析对象：`campus_electricity_capture_spec.md` + H5 静态资源 + 本机抓包
> 目的：评估「纯 HTTP 静默刷新登录态」的可行性（计划书 Option ②）

## 1. 已完成的取证

### 1.1 抓包文件解析（自研 Java 序列化解析器，见 `tools/chls_dump.py` / `tools/extract.py`）

| 文件 | 内容 | 结论 |
|---|---|---|
| `test_one.chls`（325KB，21:37） | 11 个事务：app.xiaoyuan.ccb.com、res.yunbusiness.ccb.com、touch.ccb.com 等**仅 CONNECT 隧道**记录 | 内层 HTTPS 未被 Charles MITM 解密（只有 TLS 握手字节），**不含任何明文 CCB 请求** |
| `test_two/test_three.chls`（73KB，23:17） | 22 个事务全部是微信 mmtls 加密通道（183.47.x 腾讯节点、`X-Online-Host` 头） | 微信小程序内部网络通道，加密二进制，**不含 CCB 页面请求** |

> 说明书里引用的「test_two 明文 YJF004 请求」在现有文件中不存在——那份会话要么是手机端抓的、要么已被覆盖。

### 1.2 H5 静态资源分析（公开可下载，已存档 `tools/h5/`）

- 业务 SPA：`app.xiaoyuan.ccb.com/EMTSTATIC/DZK2026111301/index2026111301.html`（控制台页面，非登录页）
- 全部 JS（app/vendor/24 个懒加载 chunk）已抓取
- 302 个 TXCODE 枚举完成（DZ0001~DZ0632、JJ、LIB、SLZF、TCJF、XK、YFK、**YJF001/003/004/006**、YXGM、YXNT、YYUF、ZF、ZFWG、YXZF、YXGM 等）

### 1.3 关键协议发现

1. **业务请求格式**（与我们的 `query_elec.py` 一致，已验证）：
   ```
   POST /LHECISM/B2CMainPlat_00  (form-urlencoded)
   公共字段: BRANCHID=555000000, SERVLET_NAME=B2CMainPlat_00, CCB_IBSVersion=V6, PT_STYLE=10
   业务字段: TXCODE=YJF004, SKEY, USERID, roomid, PayType=elec
   ```
2. **会话凭据来源**：`SESSION_STORAGE.resdata` 中的 `SKEY` + `USERID`；`resdata` 由登录流程写入
3. **登录链路（全部依赖微信客户端）**：
   - **H5 微信 OAuth**（web-view 内）：
     `open.weixin.qq.com/connect/oauth2/authorize?appid=wx3b8bb3fad101bb07&redirect_uri=/LHECISM/CCBYXCallBackService&scope=snsapi_base` → 回调落 `resdata`
   - **小程序原生登录**：`CCBYXxcxLgServiceReqServlet` + `ccbParam`（微信态加密串，源自 `wx.login`/`wx.getUserInfo(encryptedData,iv)`，服务端用小程序 appsecret 验证）
   - **短信验证码登录**：仅存在于**访客系统**（`loginVisit`，手机号+短信码，且需图形验证码/trcNo/dac），**不能用于本校学生电费账号**
   - 无任何「账号密码」类可离线登录入口
4. **ccbParam 的用途**：主要是页面间 SSO 传递（DZ0037 换取 eCard 页面的 ccbParam），不是登录凭据本身

### 1.4 wxapkg 小程序包

- 本机微信缓存 `H:\heluo\Documents\WeChat Files\Applet` 下 47 个包**全部为 `V1MMWX` 加密格式**（微信 4.x）
- 解密依赖微信内部密钥，本地无法离线解包 → **无法反编译小程序 JS 查看 ccbParam 生成逻辑**

## 2. 结论：纯 HTTP 静默刷新不可行

链路图（判定阻断点）：

```
Python(httpx) ──✗──> 微信 OAuth 授权 (open.weixin.qq.com)   ← 必须真实微信 WebView
              ──✗──> wx.login code / encryptedData          ← 必须微信 JS 环境 + 小程序 appid 校验
              ──✗──> CCBYXCallBackService 换取 resdata      ← 仅微信回调
wxapkg 反编译 ──✗──> ccbParam 算法                        ← V1MMWX 加密，无密钥
无微信环境 ──✗──> 账号密码登录                              ← 本 SPA 不存在该入口
```

**根本原因**：该平台的用户身份完全由微信态承载（openid/openIdFlag + 会话绑定的 CCBIBS1/TC Cookie），会话空闲超时（本次实测 0130Z1108912「较长时间未操作自动退出」）由服务端强制，Python 侧没有任何合法的续期手段。

## 3. 替代方案矩阵与建议

| 方案 | 可行性 | 成本 | 说明 |
|---|---|---|---|
| **A. TTL 实测 + 保活策略** | ✅ 高 | 低 | 先测会话空闲超时到底多长；若每次查询能重置计时且 TTL ≥ 8h，则每日 2–4 次定时查询天然维持会话，根本无需静默刷新；若 TTL 很短则失败 |
| **B. 半自动（现状强化）** | ✅ 现成 | 低 | 失效时推送提醒，人工一次性重抓更新 SKEY；可再配一个「抓包助手」脚本降低操作成本 |
| **C. 手机端 UI 自动化**（计划书 Mode C） | ✅ 中 | 中高 | Auto.js/Tasker 无障碍脚本：定时打开小程序→电控电表→选房→读取余额；不触碰加密协议，但需要一台安卓手机常驻 |
| **D. 纯 HTTP 静默刷新**（本次 ②） | ❌ 不可行 | — | 见第 2 节证据链 |

**推荐路线**：先做 **A（TTL 实测）**——2 小时内可给出决定性结论：
- 若 TTL ≥ 8~24h → 定时方案直接成立，问题彻底解决
- 若 TTL < 8h（如 10~30 分钟）→ 常规定时不可行，转 **C**（UI 自动化）

## 4. 复现命令

```powershell
# 抓包解析（需要 Java 序列化 .chls）
python tools/extract.py <file.chls>
# H5 静态资源已存档于 tools/h5/（app.js、vendor.js、chunks/）
```

## 5. 下一步需要的材料（TTL 实测）

1. 今日重新抓包：小程序打开 → 电控电表预存 → 选房 → 显示余额（拿到新鲜 SKEY + Cookie）
2. 立即 `python main.py --dry-run` 验证通过
3. 之后分别隔 30 分钟 / 1 小时 / 2 小时 / 4 小时 / 8 小时各跑一次 `--dry-run`，记录哪次开始报「长时间未操作」
4. 把失效时间点告诉我 → 决定走定时方案还是 UI 自动化

## 6. TTL 实测结论（2026-11-21 完成）

| 时间 | 事件 | 结果 |
|---|---|---|
| 23:40~23:43 | 抓包 test_6.chls，`--dry-run` + 正式运行 | ✅ 成功 `bal=999.30` |
| 00:12:59 | 后台探测（+30min，期间无任何请求） | ❌ `status=1, ERRORCODE=0130Z1108912 您已较长时间未进行操作…` |
| 00:2x | 手动复测（YJF004 和 DZ0020 各一次） | ❌ 全部同样报错 → 确认为**会话级失效**，非风控 |

**结论：CCB B2C 会话空闲超时 < 30 分钟（推测 15~30 分钟，银行 B2C 常见值）**。

对方案的影响：
- ❌ 每日 2–4 次定时查询 + 抓包会话：**不可行**（两次运行之间会话必死）
- ❌ 保活：需要每 ~10-15 分钟发一次请求（96–144 次/天），超出原计划频率约束；2026-11-21 已按用户决定放宽并采用（见第 7 节），间隔下限 60 秒
- ✅ ~~纯 HTTP 静默刷新~~：已证实不可行（见第 2 节）
- ✅ **UI 自动化（计划书 Mode C）**：仅剩的无人值守路线——定时在微信里打开小程序 → 电控电表 → 选房 → 截图 OCR 读数

**实际运行方式（当前版本）**：每次需要查询时重新抓包更新 `.env`（`main.py` 本身工作正常），或转为 UI 自动化方案。

## 7. 最终路线（用户决定：保活折中方案）

2026-11-21 用户确认选择「保活折中」：不追求低频轮询，而是用高频轻量请求维持会话，换取后续查询可用。

- 实现：`keep_alive.py`（默认 10 分钟/次 ≈144 次/天，`--interval` 可调，下限 60s）
- Cookie 轮换：`query_elec.py` 自动把服务端 Set-Cookie 轮换结果持久化到 `cookies.json`，避免旧 Cookie 失效
- 提醒冷却：登录失效/查询失败提醒 1 小时内最多一次，防止保活模式刷屏
- 精确 TTL 校准：`tools/ttl_probe.py`（拿到新鲜凭据后运行，输出 TTL ≈ X 分钟，据此定 interval ≈ TTL/2）
- 已知风险：高频请求可能触发接口风控；电脑休眠/断网后会话失效需重新抓包。

## 8. 重新抓包要点（2026-11-22 实测教训）

- 电费 `B2CMainPlat_00`（TXCODE=YJF004）除 `SKEY`/`USERID`/`roomid` 外，**必须带会话 Cookie `CCBIBS1`**（登录后由 `CCBYXEncryptSerReqServlet` 页面种下）。
  只带 `IN001=1001|awLYx` 会返回 `status=1` + `ERRORCODE=0130Z1108006 该页面为历史页面，请关闭浏览器重新登录`。
- 抓包时 Charles 需对 `app.xiaoyuan.ccb.com:443`（及 IP `42.201.64.127:443`）开启 SSL Proxying；
  若 History 里该域名只有 `CONNECT` 隧道（内容为 `17 03 03` 开头的 TLS 字节），说明没解密成功。
- 微信自身流量（`szextshort.weixin.qq.com` 的 `POST /mmtls/...`，`Upgrade: mmtls`）是 WeChat 私有加密，抓包中大量出现属正常噪声，与电费无关。


