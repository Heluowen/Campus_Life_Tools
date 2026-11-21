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
