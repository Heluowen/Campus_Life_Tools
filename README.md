# 校园生活监控（电费 + 燃气）

基于微信生态后端接口的宿舍**电费**（校园e码通 / 建行电控电表）与**燃气**（港华 vcc-cbs，广州东永港华）余额定时查询与推送工具，附本地 Web 面板。
完整项目背景与后续阶段见根目录 `campus_electricity_workplan.md`；
接口协议细节见根目录 `campus_electricity_capture_spec.md` 与 `docs/REVERSE_GAS.md`。

## 协议摘要

**电费（建行 B2C 网关）**

- **网关**：`POST https://app.xiaoyuan.ccb.com/LHECISM/B2CMainPlat_00`，表单编码 `application/x-www-form-urlencoded`，无请求加密、无动态签名，可直接重放
- **业务路由**：`TXCODE=YJF004` 查询电控余额；表单参数 `USERID` / `SKEY` / `roomid` 来自 `.env`，`PayType=elec`、`aid=""` 为固定值
- **响应**：`status=="0"` 表示成功；剩余电量在 `data.bal`（字符串，如 `1001.20`），单位在 `data.meterflag`（如 `度`），`data.roomid` 可用于校对
- **会话**：服务端每次响应通过 `Set-Cookie` 更新 `CCBIBS1` / `IN001`，程序使用 `httpx.Client` 会话自动维护，无需手工更新 Cookie
- **鉴权失效判定**：HTTP 401/403、返回登录页 HTML、或 `msg` 提示登录失效 → 触发提醒重新抓包

**燃气（港华 vcc-cbs）**

- **接口**：`GET https://weixin.towngasvcc.com/nv1/vcc-cbs/charge/preCheck?subsId=<subsId>`，**`sign` 参数实测不校验**，带 `Authorization: Bearer <token>` 即可
- **响应**：`datas.savingSum` 为气费余额（元），`datas.readingRptList[0].currReading` 为表读数
- **鉴权**：OAuth token 来自微信（无法离线生成），失效后需重新抓包更新 `GAS_TOKEN`
- 详见 `docs/REVERSE_GAS.md`

## 目录结构

```text
campus-elec-monitor/
├── .env.example      # 环境变量模板（提交仓库）
├── .env              # 真实凭据与宿舍参数（禁止提交）
├── .gitignore
├── requirements.txt  # httpx / python-dotenv / tzdata
├── config.py         # 配置加载与校验（电费 + 燃气）
├── query_elec.py     # YJF004 查询与响应解析
├── query_gas.py      # 燃气余额查询与解析
├── notifier.py       # 推送：企业微信 / PushPlus / Server酱 / Bark
├── state.json        # 本地状态（上次电量、告警日期，防重复推送）
├── cookies.json      # 会话 Cookie 轮换持久化（自动生成，gitignore）
├── main.py           # 主入口：查询（电费+燃气）→ 阈值判断 → 防骚扰 → 推送
├── keep_alive.py     # 会话保活守护进程（高频折中方案）
├── web/
│   ├── server.py     # 本地网页面板服务（仅 127.0.0.1）
│   └── static/
│       └── index.html # 自包含前端页面（无外部依赖）
└── tools/
    ├── chls_dump.py  # Charles .chls（Java 序列化）解析器
    ├── extract.py    # 抓包 → 结构化事务提取
    └── ttl_probe.py  # 会话空闲超时（TTL）精确校准
```

## 快速开始

```powershell
cd campus-elec-monitor
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env   # 然后编辑 .env
python main.py --dry-run      # 联调：只查询与判断，不推送、不写状态
python main.py                # 正式运行
```

## 本地网页面板

```powershell
python web/server.py --port 8877
```

浏览器打开 **http://127.0.0.1:8877** 即可查看：

- **电费**：剩余电量大数字 + 状态标签（充足 / 偏低 / 告急）、阈值刻度条（告急 <5 / 预警 <20 可调）
- **燃气**：气费余额大数字（元）+ 状态标签 + 阈值刻度条（低于 15 元提醒，可调）+ 户号/表读数
- 上次查询时间、今日消耗、按趋势估算的预计可用天数
- 近 24 小时趋势图（电费青色 / 燃气琥珀色，数据来自保活任务，每 10 分钟一个点）
- 「立即刷新」按钮：实时查询电费+燃气一次（不触发推送；保活任务仍负责告警）
- 页面每 30 秒自动同步一次；超过 32 分钟无更新会提示「可能已过期」

服务只监听 `127.0.0.1`，不暴露到局域网。Ctrl+C 停止。

## `.env` 填写清单（抓包 → 配置映射）

| 抓包中的位置 | `.env` 变量 | 说明 |
|---|---|---|
| 请求体 `USERID` | `CCB_USERID` | 建行校园一卡通用户编号 |
| 请求体 `SKEY` | `CCB_SKEY` | 当前会话校验密钥（失效后需重新抓包） |
| 请求体 `roomid` | `CCB_ROOMID` | 固化后的房间号，如 `134-33--250-501` |
| 请求头 `Cookie` | `CCB_COOKIE` | 完整 Cookie 串（可留空，失败时补全） |
| — | `CCB_ROOM_NAME` | 推送展示名（可选，如 `40栋-501`） |
| — | `WARN_THRESHOLD` / `CRITICAL_THRESHOLD` | 电费两级阈值，单位与 `meterflag` 一致（如度） |
| 燃气 `preCheck` 的 `subsId` 参数 | `GAS_SUBSID` | 燃气户号内部 ID（填了它和 TOKEN 才启用燃气查询） |
| 燃气请求头 `Authorization: Bearer x` | `GAS_TOKEN` | 微信 OAuth token（失效后需重新抓包） |
| 燃气请求头 `Cookie` | `GAS_COOKIE` | 可选，`__h5_oauth_ticket__` 等 |
| — | `GAS_WARN_THRESHOLD` | 燃气提醒阈值（元，默认 15） |
| — | `WECOM_WEBHOOK` / `PUSHPLUS_TOKEN` / `SERVERCHAN_KEY` / `BARK_URL` | 推送凭据，留空则自动推断渠道；`none` 表示只打印不推送 |

## 安全边界（必须遵守）

- 敏感信息（`USERID` / `SKEY` / `Cookie` / Webhook）只放 `.env`，禁止硬编码、禁止提交仓库；
- 常规定时查询每天 2–4 次；因会话 TTL（实测 < 30 分钟）无法覆盖定时窗口时，允许保活维持会话（默认 10 分钟/次 ≈144 次/天，间隔 ≥ 60 秒，按实测 TTL/2 调整），禁止无间隔死循环（`MAX_RETRIES` 上限 2）；
- 只查询本人目标宿舍，禁止遍历楼栋或批量枚举房间号。

## 定时调度（Phase 4 参考）

- **公网**（GitHub Actions 私有仓库）：`.github/workflows/check.yml` 配置
  `schedule.cron`（UTC 时间；北京时间 08:00 / 20:00 对应 `0 0 * * *` 和 `12 0 * * *`），
  把 `.env` 内容放入仓库 Secrets，并设置 `ALERT_TZ: Asia/Shanghai`。
- **仅校园网**：宿舍路由器 / 闲置电脑 / 旧手机 Termux，用系统计划任务（crontab）每天 2–4 次；如需保活维持会话，见下方「保活模式」。

> ⚠️ 注意：CCB B2C 会话空闲超时 **< 30 分钟**（实测 2026-11-21）。纯定时方案（两次运行间隔
> > 30 分钟）会因会话过期而失败。若需无人值守，请使用下方「保活模式」。

## 保活模式（高频折中方案，用户已知情选择）

会话空闲超时 < 30 分钟，因此「每日 2–4 次」的定时窗口内会话必死。
`keep_alive.py` 以固定间隔（默认 10 分钟，≈144 次/天）持续查询来维持会话，
余额告警仍按 main.py 相同规则发送（每日一次 + 失败提醒 1 小时冷却）。

```powershell
# 前台运行（测试）
python keep_alive.py --interval 600
# 试运行 8 小时后退出（测试）
python keep_alive.py --max-hours 8
```

**风险提示**：144 次/天可能触发接口风控；间隔低于 60s 会被强制为 60s。
电脑休眠/断网/重启后会话仍会失效，需重新抓包更新 `.env`（`main.py` 会推送提醒）。

**Windows 计划任务（登录自启，隐藏窗口）**：

```powershell
schtasks /Create /TN "ElecKeepAlive" /SC ONLOGON /RL LIMITED /TR "cmd /c cd /d H:\Deepseek-Harness\Zone_2\campus-elec-monitor && python keep_alive.py --interval 600 >> keepalive.log 2>&1" /F
```

删除任务：`schtasks /Delete /TN "ElecKeepAlive" /F`

**TTL 精确校准**（拿到新鲜凭据后立即运行，约 40 分钟出结果）：

```powershell
python tools/ttl_probe.py --interval 120 --max-minutes 40
```

输出「空闲超时 TTL ≈ X 分钟」，据此选择保活间隔（推荐 TTL/2）。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 查询成功（含无需告警） |
| 1 | 运行时错误（如推送失败） |
| 2 | 配置错误 |
| 3 | 登录失效，需重新抓包更新凭据 |
| 4 | 查询失败 |

## 排错提示

- **接口返回登录页 HTML / SKEY 失效**：重新抓包更新 `CCB_SKEY`（Cookie 会话每次响应会自动刷新，一般无需手改 `CCB_COOKIE`）。
- **Windows 终端中文乱码**：执行 `chcp 65001` 或设置环境变量 `PYTHONIOENCODING=utf-8` 后重试。
- **代理干扰**：本机若开启系统代理（Clash/VPN 等），httpx 会经代理访问接口；
  若接口仅限校园网直连，请在 `.env` 设置 `TRUST_ENV=0`，或系统环境变量设置
  `NO_PROXY`（包含目标域名）后重跑 `--dry-run` 验证。
- **接口迁移/字段变化**：修改 `query_elec.py` 顶部的 `GATEWAY_URL`、`TXCODE_BALANCE`、`REFERER`、`DEFAULT_USER_AGENT` 常量。
