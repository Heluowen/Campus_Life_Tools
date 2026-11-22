# 变更同步说明（致 DeepSeek 协作方）

> **同步基准提交**：`e61e334`（`fix: pythonw-safe logging (no stdout) for web panel and keepalive daemon`）  
> **本次变更日期**：2026-11-22  
> **测试验证**：`pytest -v tests` — **10 passed**（全部离线单元测试通过）

本说明用于向协同开发的 DeepSeek 完整同步本次对 `Campus_Life_Tools` 仓库的结构调整、Bug 修复、功能增量与接口约定，方便后续在最新 `main` 上继续迭代（例如推进开源化重构或接入水费等新数据源）。

---

## 1. 变更总览（TL;DR）

1. **仓库清理与合规**：移除误提交的 2.7 MB 第三方港华前端打包文件 `tools/gas_js/`；去除 `start.bat`、`tools/extract.py` 中的本机硬编码路径；敏感/校区差异参数配置化。
2. **并发安全与防死锁**：修复 `state.json` / `.env` / `cookies.json` 多线程/多进程写入竞态；`keep_alive.py` 单实例锁升级为 OS 内核文件锁（Windows `msvcrt.locking` / Unix `fcntl.flock`）。
3. **DRY 去重重构**：将查询与状态落盘原语统一沉淀至 `main.py`（`fetch_and_update_elec` / `fetch_and_update_gas`），`web/server.py` 删除重复实现直接复用。
4. **核心功能升级**：
   - 新增一站式抓包/cURL 凭据提取工具 `tools/harvest_creds.py`（电费 + 燃气全套自动提取）；
   - Web 面板新增「更新凭据」弹窗与 `POST /api/import_curl` 接口支持热更新 `.env`；
   - 历史记录上限 `500 → 1200` 并加入同小时同余额智能合并，保证 7 天趋势图完整；
   - 新增燃气保活节流间隔 `GAS_QUERY_INTERVAL`（默认 1800s）；
   - 推送模块支持逗号分隔多渠道同时推送（如 `PUSH_CHANNEL=wecom,bark`）。
5. **工程化基线**：补齐离线单元测试套件 `tests/test_core.py`，新增 `.gitattributes` 锁定 `*.bat` 为 CRLF。

---

## 2. 逐文件修改明细

### 2.1 仓库卫生与跨环境兼容

| 文件 | 变更类型 | 具体改动 |
|---|---|---|
| `tools/gas_js/{app.js,croods.js,vendors.js}` | **删除** | 从 Git 追踪和工作区彻底移除第三方商业前端 bundle（共约 2.7 MB），逆向结论已保留在 `docs/REVERSE_GAS.md`。 |
| `.gitignore` | 修改 | 新增忽略规则 `tools/gas_js/`。 |
| `.gitattributes` | **新增** | 声明 `*.bat text eol=crlf`，防止 Windows 批处理因 LF 换行符导致 `cmd.exe` 解析报错。 |
| `start.bat` | 修改 | ① 移除硬编码路径 `H:\AstrBot\backend\python\pythonw.exe`，改为按 `.venv → venv → %PYTHONW_EXE% → PATH` 自动探测；② 添加 `chcp 65001`；③ 移除不可靠的 `if exist keepalive.lock` 判断，交由 `keep_alive.py` 内核文件锁互斥；④ 扁平化 `if` 结构并统一 CRLF。 |
| `stop.bat` | 修改 | 添加 `chcp 65001`，`for /f` 改用 `usebackq` 安全读取带引号路径，统一 CRLF。 |
| `tools/extract.py` | 修改 | 移除硬编码 `H:\Deepseek-Harness\Zone_2\...`，改为 `Path(__file__).resolve().parent`。 |
| `tools/harvest_gas_token.py` | 修改 | 补齐 `tools/` 与项目根目录到 `sys.path`，支持任意工作目录或模块方式导入调用。 |
| `docs/REVERSE_GAS.md` | 修改 | 脱敏示例户号；修复文末受损的代码块标记（原文件含 `\x08` 退格符导致 `` ` ash ``）；补充 `harvest_creds.py` 用法。 |

---

### 2.2 核心模块重构与接口变化（协作重点）

#### `config.py`
- **`ApiSettings` 新增可选字段**：
  - `branch_id: str = ""`（对应 `.env` 的 `CCB_BRANCHID`，空则回退 `query_elec.BRANCHID`）
  - `referer: str = ""`（对应 `.env` 的 `CCB_REFERER`，空则回退 `query_elec.REFERER`）
- **`PushSettings` 升级多渠道支持**：
  - 新增 `channels: tuple[str, ...]` 与 `@property is_none`；
  - `PUSH_CHANNEL` 支持逗号分隔（如 `wecom,bark`）；留空时 `_infer_channels()` 会**自动启用全部已填凭据的渠道**（原逻辑只取第一个命中渠道）。
  - 向后兼容：`PushSettings(channel="wecom", ...)` 旧构造方式仍可通过 `__post_init__` 自动填充 `channels`。
- **`GasSettings` 新增字段**：
  - `query_interval: int = 1800`（对应 `.env` 的 `GAS_QUERY_INTERVAL`，下限 60 秒）。
- **新增原子批量写 `.env` 接口**：
  - `update_env_many(updates: dict[str, str], env_path: Path | None = None)`：受全局 `_ENV_LOCK` 保护，使用 `PID + TID` 临时文件写入后 `replace()` 原子替换；
  - `update_env(key, value, env_path=None)` 改为薄封装调用 `update_env_many`。
- **`load_settings(env_path: Path | None = None)`**：显式加载项目根目录 `ENV_FILE`（或指定路径），避免工作目录不在项目根目录时找不到 `.env`。

#### `query_elec.py`
- `_build_form(api)` 与 `_build_headers(api)` 优先使用 `api.branch_id` / `api.referer`，未配置时回退内置常量。
- `_save_cookie_store()` 升级为带 `PID + TID` 的临时文件原子替换，避免并发写入损坏 `cookies.json`。

#### `notifier.py`
- `send_notification(push, title, content)` 改为遍历 `push.channels` 逐个发送：
  - 只要至少一个渠道发送成功即返回 `True`（部分渠道失败会打印 `[WARN]`）；
  - 全部渠道均失败时聚合错误信息抛出 `NotifyError`。

#### `main.py`
- **新增共享查询与状态落盘原语**（供 CLI / `keep_alive.py` / `web/server.py` 三端统一调用）：
  - `fetch_and_update_elec(settings, *, dry_run=False, notify=True) -> tuple[int, dict | None]`
  - `fetch_and_update_gas(settings, *, dry_run=False, notify=True, respect_interval=False) -> tuple[int, dict | None]`
- **`run_once(settings, dry_run=False, respect_gas_interval=False) -> int`**：新增可选参数 `respect_gas_interval`；保活守护传 `True` 时，燃气查询受 `GAS_QUERY_INTERVAL` 节流（若 `token_issued_at` 距离签发已 ≥ 5400s 接近主动续期阈值，则忽略节流立即放行，确保续期链不断裂）。
- **并发与原子写盘加固**：
  - 引入全局线程锁 `_STATE_LOCK` 保护 `state.json` 读-改-写周期；
  - `_save_state()` 使用 `state.json.{pid}.{tid}.tmp` 写入并带 Windows `PermissionError` 短暂重试替换，结束后清理残留 `.tmp`。
- **历史曲线数据优化**：
  - `HISTORY_MAX` 由 `500` 调整为 `1200`（原 500 点在 10 分钟保活频率下仅够约 3.5 天，导致前端「近 7 天」图截断）；
  - 新增 `_append_history()`：同一小时内连续相同余额只保留首尾 2 个关键点，大幅延长实际保留天数而不损失变化精度。
- **补充落盘字段**：成功查询燃气时同步将 `result.subs_code` 写入 `state["gas_subs_code"]`（修复前端面板「户号」一直显示 `—` 的问题）。
- **燃气 token 刷新回调**：`_on_gas_token_refreshed` 改用 `update_env_many` 一次性原子写入 `GAS_TOKEN` / `GAS_REFRESH_TOKEN` / `GAS_TOKEN_ISSUED_AT`。

#### `keep_alive.py`
- **单实例锁重构**：废弃 `os.kill(pid, 0)`，改用跨平台 OS 内核文件锁（Windows `msvcrt.locking(..., LK_NBLCK, 1)` / Unix `fcntl.flock(..., LOCK_EX | LOCK_NB)`），并保持全局文件句柄 `_LOCK_FH` 直至退出释放。进程意外崩溃或断电时由内核自动解锁，不会遗留死锁阻塞重启。
- 调用 `m.run_once(settings, dry_run=False, respect_gas_interval=True)`，启用燃气保活节流。

#### `web/server.py` 与 `web/static/index.html`
- **删除重复代码**：移除原先冗余的 `_load_state` / `_set_env_value` / `_persist_gas_tokens` / `_apply_elec` / `_apply_gas`，统一复用 `main.py` 的 `fetch_and_update_elec` / `fetch_and_update_gas`（传 `notify=False`）及 `config.update_env`。
- **`GET /api/status`**：补充返回 `gas_subs_code`。
- **新增接口 `POST /api/import_curl`**：
  - 接收 JSON `{"curl": "<cURL 文本>"}`，限制请求体上限 `256 KB`；
  - 调用 `tools/harvest_creds.py:parse_curl()` 提取电费/燃气凭据并原子写入 `.env`；
  - 若更新了 `CCB_SKEY` 或 `CCB_COOKIE`，自动删除旧 `cookies.json` 以免旧会话覆盖新凭据。
- **安全加固**：`_send_static` 改用 `target.relative_to(root)` 替代 `str.startswith()` 校验路径穿越。
- **前端面板 `index.html`**：右上角新增「更新凭据」按钮及弹窗，支持直接粘贴 Charles 导出的 cURL 一键热更新本地 `.env`。

---

### 2.3 新增工具与测试

#### `tools/harvest_creds.py`（新增）
一站式凭据提取脚本，复用现有 `chls_dump.py` / `extract.py`，同时支持 `.chls` 抓包与 cURL（兼容 bash `\`、cmd `^`、PowerShell `` ` `` 续行符）：
- **电费匹配**：`xiaoyuan.ccb.com` / `B2CMainPlat_00`（`TXCODE=YJF004`）→ 提取 `CCB_USERID`、`CCB_SKEY`、`CCB_ROOMID`、`CCB_COOKIE`、`CCB_BRANCHID`、`CCB_REFERER`、`CCB_USER_AGENT`；
- **燃气匹配**：`towngasvcc.com` / `/vcc-cbs/` / `/vcc-oauth/` → 提取 `GAS_SUBSID`、`GAS_TOKEN`、`GAS_COOKIE`、`GAS_CLIENTID`、`GAS_REFRESH_TOKEN`，若抓到 `accessToekn`/`refreshToken` 响应 JSON 还会提取最新 token 对并写入 `GAS_TOKEN_ISSUED_AT`；
- 加 `--write` 自动原子写回 `.env` 并在更新电费凭据时自动清理旧 `cookies.json`。

#### `tests/test_core.py`（新增）
离线单元测试（不发起真实外网请求），共 10 个用例，覆盖：
1. `CCB_BRANCHID` / `CCB_REFERER` 与多渠道 `PUSH_CHANNEL` 解析；
2. `PUSH_CHANNEL` 留空时多渠道自动推断；
3. `update_env_many` 原子更新与注释保留；
4. `notifier` 多渠道部分失败容错与全部失败抛错；
5. `_append_history` 同小时同余额去重与变化保留；
6. 燃气保活节流跳过与手动强制刷新；
7. `parse_curl` 同时解析电费与燃气 cURL；
8. Web `_import_curl` 写 `.env` 并清理过期 `cookies.json`；
9. 6 线程并发读写 `state.json` 无竞争丢数据；
10. `keep_alive` `msvcrt` 内核文件锁互斥验证。

---

## 3. 新增 `.env` 可选配置项汇总（均向下兼容，旧 `.env` 无需修改即可直接运行）

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `CCB_BRANCHID` | `555000000` | 建行机构号覆盖（跨高校复用） |
| `CCB_REFERER` | 内置默认 URL | 建行 H5 Referer 覆盖 |
| `GAS_QUERY_INTERVAL` | `1800` | 保活模式下燃气最小查询间隔（秒，下限 60） |
| `PUSH_CHANNEL` | 自动推断 | 现支持逗号分隔多渠道（如 `wecom,bark`） |

---

## 4. 给 DeepSeek 后续协作的注意事项

1. **同步远端**：在 `Zone_2/campus-elec-monitor` 执行 `git pull` 前，请留意本地未被追踪的 `.env` / `state.json` / `cookies.json` 已在 `.gitignore` 中，不会被覆盖；但原已跟踪的 `tools/gas_js/` 会在 pull 后被 Git 从工作区移除（如需保留分析请先备份到仓库外）。
2. **新增数据源（如水费）建议**：后续按 `campus_life_opensource_plan.md` 拆分 `sources/` 插件架构时，可直接在现有 `main.py` 的 `fetch_and_update_*` 范式基础上抽象 `Source` 协议，并继续保留 `_STATE_LOCK`、`_append_history()` 和 `respect_interval` 节流能力。
3. **测试门禁**：修改核心逻辑后建议直接运行 `pytest -v tests` 验证离线回归。
