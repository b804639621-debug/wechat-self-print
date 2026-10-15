# 端口冲突解决方案（printshop 主控服务）

> 范围：主控 HTTP 服务端口（现役 `8000`）的配置化 + 占用检测 + 固件/文档同步。
> 不改任何接口契约（/screen、/api/screen/*、/admin 路由与语义全部不变）。

---

## 1. 问题与约束

**问题**：端口单一硬编码，易与常见开发端口冲突，冲突时表现为 uvicorn 启动失败
（GUI 模式 `main.py:172-180` 的 15s 超时兜底报"uvicorn 服务启动失败"，原因不直观）。

**现状链路（改动涉及面）**：

| 位置 | 内容 |
|---|---|
| `printshop/config.py:9` | `port: int = 8000`（唯一来源，无覆盖机制） |
| `printshop/main.py:212-217` | `uvicorn.Config(app, host=cfg.host, port=cfg.port, ...)` |
| `printshop/main.py:156/183/184` | tray/gui 用 `f"http://127.0.0.1:{cfg.port}"` 拼 URL（随 cfg 自动正确） |
| `firmware/print_screen/print_screen.ino`（现状 TRANSPORT_WIFI 分支） | `#define SERVER_IP 192.168.31.171`、`#define SERVER_PORT 8000` 编译期写死 |
| `firmware/README.md §三` | "编译前修改主控 IP（端口同样此处写死）" |
| `printshop/battery_track.py:224` | `--base-url http://127.0.0.1:8000` 默认值（工具侧，非服务端） |

**硬约束**：
- ESP32 小屏靠编译期写死的 IP+端口 HTTP 轮询，**端口必须稳定可预知**——运行期静默换端口会导致小屏永久失联且无人察觉。因此"冲突时自动随机换端口"不可作为默认行为。
- 管理端点仅本机访问（`main.py` loopback_guard）与端口无关，任何端口下语义一致。

---

## 2. 方案对比（已评估，选 C）

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| A. 仅把默认值改成冷门高位端口（如 18080） | 改动近乎为零 | 仍无覆盖手段，换端要重新打包 | 并入 C 作为默认值变更 |
| B. 冲突时自动探测空闲端口随机起 | 服务永不因端口挂 | **破坏固件编译期契约**，小屏永久失联 | 否决（可留 `--port auto` 显式参数仅调试用） |
| **C. 三层配置覆盖 + 启动预检 fail-fast**（推荐） | 端口由用户显式决定，冲突时报错清晰；固件契约稳定 | 需改 config/main + 文档 | ✅ 采用 |
| D. mDNS/zeroconf 服务发现（ESP32 动态找主控） | 端口随便换固件都能找到 | 引入 zeroconf 依赖 + ESPmDNS 固件改造，店内封闭网已有静态 IP 方案，收益低 | 备选二期，不纳入本次 |

---

## 3. 终版设计

### 3.1 默认端口改为 `18080`

理由：8000/8080/5000/3000 是 FastAPI、各类 dev server、webpack 等默认端口，高频撞车；
18080 冷门且为高位非特权端口（>1024，无需管理员）。作为新的唯一默认值。

### 3.2 配置优先级（高 → 低）

```
命令行参数 --port --host
  > 环境变量 PRINTSHOP_PORT / PRINTSHOP_HOST
    > exe 同级 printshop.json 的 {"port":18080,"host":"0.0.0.0"}
      > 代码默认值 18080
```

实现要点：
- `config.py`：`__post_init__` 追加读 `PRINTSHOP_PORT`/`PRINTSHOP_HOST`（沿用现有
  `PRINTSHOP_SCREEN_BLE_ADDRESS` 的环境变量处理风格）；新增
  `Config.from_json_file(Path) -> Config`，缺文件/坏 JSON/缺键均静默忽略回退默认值。
- `main.py:main()`：加 argparse（参数解析后从 `sys.argv` 剥离，`--ble-selftest` 与
  `--web` 分支逻辑不动），`cfg = Config(port=args.port, host=args.host)`。
- `printshop.spec` 打包时**不**打包配置文件；`install_autostart.ps1` 自启项参数不变
  （数据目录机制不变，配置文件走 exe 同级目录，冻结态 chdir 已覆盖该语义）。

### 3.3 启动端口预检（fail-fast，拒绝静默换端口）

`main.py` 在构造 uvicorn.Server 之前：

```python
def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            s.bind((host if host != "0.0.0.0" else "", port))
            return True
        except OSError:          # WinError 10048/10013 等
            return False
```

- 端口被占 → 写日志（"`port 18080 被占用`，可用 `printshop.exe --port 19000` 或
  编辑 exe 同级 printshop.json 更换"）+ 托盘通知 + 退出码 3，**不改端口硬起**。
- 附带把 GUI 模式 15s 超时报错（`main.py:172-180`）的失败原因同步 `"端口占用"` 上下文。

### 3.4 双实例互斥（同一台店内机误双开）

自启动场景可能重复拉起第二份 printshop。第二份预检会发现端口被占而退出，但报错应
可区分"被自己占用"与"被其他程序占用"：预检失败时向 `http://127.0.0.1:{port}/` 发一次
GET（2s 超时），响应含 `"WeChat Self-Print"`（FastAPI title，`main.py:123`）即判定为
本服务已运行 → 托盘提示"已在运行"后静默退出（退出码 0），否则按端口冲突报错（退出码 3）。

### 3.5 固件与文档同步点（换端口时的核对清单）

1. `print_screen.ino` 现役 TRANSPORT_WIFI 分支 `#define SERVER_PORT` 改为新端口，按
   `firmware/README.md §三` 流程重编译烧录；BLE 变体不受影响（不依赖 HTTP 端口）。
2. `README.md:16/60-61`、`firmware/README.md §三(规划段)` 默认端口文案 8000 → 18080。
3. `printshop/battery_track.py:224` `--base-url` 默认值同步 18080。
4. 托盘菜单已显示 `局域网IP:端口`（取自 cfg，自动跟随，无需改动），换端口后以托盘
   显示值为准核对小屏连通性（`GET /api/screen/state` 200 为准）。

### 3.6 可选加固：端口系统级预留

在店内主控机执行一次（管理员，重启后持续生效），防止其他软件抢注 18080：

```
netsh int ipv4 add excludedportrange protocol=tcp startport=18080 numberofports=1
```

列为运维建议而非代码改动（需管理员权限，不适合写入服务自身）。

### 3.7 测试

- pytest：`Config.from_json_file` 缺失/坏 JSON/正常三分支；env 覆盖优先级高于 JSON；
  CLI 高于 env（解析层单测）。
- `_port_free` 占用/空闲两态（占用态用本地再 bind 一个 socket 构造）。
- 双实例互斥判定：mock HTTP 探测命中/不命中。

---

## 4. 实施清单（建议顺序）

| # | 动作 | 文件 |
|---|---|---|
| 1 | 默认端口 8000 → 18080 | `printshop/config.py:9` |
| 2 | env 覆盖 + `Config.from_json_file` | `printshop/config.py` |
| 3 | argparse `--port/--host` + 端口预检 + 双实例互斥 | `printshop/main.py` |
| 4 | 退出码约定（0=已运行让位，2=启动失败，3=端口冲突）写注释与 README | `printshop/main.py`、`README.md` |
| 5 | 文档默认端口 + `--port` 用法示例 | `README.md`、`firmware/README.md` |
| 6 | `battery_track.py` 默认 base-url | `printshop/battery_track.py:224` |
| 7 | 单测 + 全量回归（现 285 例不回归） | `tests/` |

回滚路径：所有改动仅影响启动参数解析与默认值，`git revert` 即回 8000 原状，
不涉及存储/协议/固件契约变更。

---

## 5. 日常运维口径（写给店内/交接）

- 换端口三选一：`printshop.exe --port 19000`（临时）；设环境变量 `PRINTSHOP_PORT`
  （会话级）；编辑 exe 同级 `printshop.json`（持久，推荐）。
- 换端口后**必须**同步改固件 `SERVER_PORT` 并重烧（见 3.5），否则小屏失联。
- 端口被其他程序占用时程序会明确报错退出并给出建议参数，日志见托盘"打开日志"。
