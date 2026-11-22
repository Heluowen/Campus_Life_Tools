# 燃气余额查询逆向记录（港华 vcc-cbs，微信服务号）

> 日期：2026-11-21 · 来源：water_1.chls / water_2.chls（Charles 抓包）
> 服务号：广州东永港华燃气有限公司 → 燃气服务 → 我要缴费 → 气费预存

## 1. 接口结论

**余额接口**（页面上「我要缴费」即触发）：

```
GET https://weixin.towngasvcc.com/nv1/vcc-cbs/charge/preCheck?subsId=<subsId>
Authorization: Bearer <token>
```

- `subsId`：户号内部 ID（绑定微信号后可从 `queryBindList` 接口或抓包获得）
- `token`：微信 OAuth 发放的 Bearer token（32 位 hex，有效期未知，需实测）
- **`sign` 参数实测不校验**：原样重放、换新 timestamp、完全省略 sign 三种请求均返回 200 成功数据 → 重放极简
- Cookie（`__h5_oauth_ticket__` 等）非必需，带上更稳妥

**响应字段**（`datas` 内）：

| 字段 | 说明 |
|---|---|
| `savingSum` | **气费余额（元）**，本次抓包为 `96.35` |
| `chargeType` | `prepay`（预存） |
| `subsCode` | 户号（如 1800150223） |
| `readingRptList[0].currReading` | 燃气表读数（本次 1468） |
| `resultCode` | `"0"` 成功 |

**关联接口**（首次配置时可用）：
`GET /nv1/vcc-cbs/usersubs/queryBindList?oneSubsFlag=&isPay=N&orgId=<orgId>&subsId=&timestamp=<ms>&sign=<md5>`
返回 `subsId / subsCode / displayAddr` 等。

## 2. 鉴权与失效

- token 来自微信 OAuth（公众号 `wx7dcd909d3efd8043`），无法离线生成；
- 失效表现（待实测确认）：HTTP 401/403 或 `resultCode != 0` 且提示 token/登录/授权；
- 应对：重新打开燃气服务页抓包，更新 `.env` 的 `GAS_TOKEN`（与电费 SKEY 处理方式一致）。
- 保活：`keep_alive` 每 10 分钟同时查询电费+燃气，可维持/延后 token 过期（效果需实测）。

## 3. 复现命令

```powershell
# 只查燃气（重放）
python -c "from query_gas import query_gas; print(query_gas('<subsId>', '<token>', 10))"
```

## 4. 待验证

1. token 真实有效期（保活间隔 10 分钟下能存活多久）；
2. token 过期时的错误表现（用于分类 GasAuthExpiredError）；
3. 若 token 很短（<30 分钟），与电费一样需要配合保活。


## 5. Token 自动刷新（vcc-oauth）

H5 前端（`/h5-gas/js/app.js`、`vendors.js`）暴露的刷新逻辑：

| 接口 | 用途 |
|---|---|
| `GET /vcc-oauth/oauth/authorize2/union?clientid=<ch>&token=<ticket>` | 登录票据换 `access_token + refresh_token`（前端 `exchangeToken`） |
| `GET /vcc-oauth/oauth/authorize2/refreshToken?clientid=<ch>&token=<refresh_token>` | 刷新 access_token（前端 `refreshToken`，token 过期时自动调用） |
| `POST /vcc-oauth/oauth/authorize2/accessToekn?authCode=<code>` | 微信授权 code 换初始 token |

- `clientid`（channel）= `pe92a8wechat11301DY0105`（东永港华）
- access_token 有效期约 2 小时；refresh_token 有效期通常更长
- 本项目 `query_gas.py` 已支持：token 过期（20001）时自动调 refreshToken 换新 token 并写回 `.env`；
  需要 `.env` 里配置 `GAS_REFRESH_TOKEN` + `GAS_CLIENTID`（从完整登录/刷新抓包中获取）
- refresh 失败（如 refresh_token 也过期）→ 抛 GasAuthExpiredError，提示重新抓包


## 6. Token 自动刷新（已实测打通，2026-11-22）

### 登录（首次/过期后由 H5 触发）
```
GET  /vcc-oauth/oauth/authorize2/union?clientid=<ch>&redirectUri=<transitionPage>
     -> 302 oauthLogin?forwardTicket=... (微信内 OAuth)
GET  /h5-gas/pages/transitionPage/index?code=<code>
POST /vcc-oauth/oauth/authorize2/accessToekn?authCode=<code>&timestamp=<ms>&sign=<md5>
  响应: {"access_token":"32hex","refresh_token":"32hex","expires_in":7200}
```

### 刷新（token 过期时）
```
POST /vcc-oauth/oauth/authorize2/refreshToken?clientid=<ch>&timestamp=<ms>&sign=<md5>
  表单: refreshToken=<refresh_token>
  响应: {"access_token":"...","refresh_token":"...","expires_in":7200}   # 会轮换 refresh_token！
```

### 签名算法（vcc-oauth / vcc-cbs 通用）
`sign = MD5( 按 key 排序拼接 "key+value"（跳过 sign/空值） + "hbasesoft.com-prod" ).upper()`
（预检 preCheck 不校验 sign，但刷新接口建议带上）

### 工程实现
- `query_gas.py`：查询遇 `20001` 自动调刷新接口 → 新 token 对通过回调写回 `.env`
  （`GAS_TOKEN` + `GAS_REFRESH_TOKEN` 同步轮换）→ 重试查询
- 实测：故意放无效 token → 自动换新 → 查询成功 ✅

## 6. 应急：从抓包提取有效 token（工具）

如果抓包中**没有** token 交换请求（accessToekn / refreshToken），页面实际发往
`vcc-cbs` 的业务请求（如 `charge/preCheck`）的 `Authorization` 头就是它当时
正用的有效 access token，可以直接提取恢复查询：

`ash
python tools/harvest_gas_token.py water_5.chls            # 只打印
python tools/harvest_gas_token.py water_5.chls --write    # 直接写 .env
`

注意：这只是"应急恢复"，token 约 2 小时后仍会过期；长期自动续期需要抓到一次
`refreshToken` 或 `accessToekn` 请求（页面在 token 过期或重新登录时才会发出，
此时用 Charles 打开燃气页即可抓到）。
