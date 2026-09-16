# 工具链现场摸底报告（M-A0 / 方案 A1 前置）

- 任务命名日期：2026-09-07
- 报告文件：`docs/toolchain-m-a0-report-2026-09-07.md`
- 实际执行（本机系统时钟）：2026-09-08 20:01（无时区显示）
- 工作目录确认：`pwd` → `/e/kaifa/自助打印`
- 执行人：子代理（工具链摸底代理）
- 范围：仅读取/记录与创建官方预编译库备份；未修改任何源代码，未 git commit，未执行 lib 重编/menuconfig/sdkconfig 覆盖等破坏性操作。

## 0. 已阅读

- `docs/plan-2026-09-07-wifi-power-save.md` 第 510~575 行（P0 与方案 A1）
- `firmware/README.md` §一/§二（arduino-cli 环境约定）

## 1. 版本与布局记录

### 1.1 arduino-cli 版本

```text
$ tools/arduino-cli/arduino-cli.exe version
arduino-cli  Version: 1.5.1 Commit: 01f3d4f2b Date: 2026-06-05T10:22:12Z
EXIT=0
```

### 1.2 core list

```text
$ tools/arduino-cli/arduino-cli.exe core list
ID          Installed Latest Name
esp32:esp32 3.3.3     3.3.11 esp32
```

### 1.3 config dump

```text
$ tools/arduino-cli/arduino-cli.exe config dump
board_manager:
    additional_urls:
        - http://127.0.0.1:18765/package_esp32_index_cn.json
directories:
    data: C:\arduino15
```

### 1.4 platform package.json version

路径：`C:\arduino15\packages\esp32\hardware\esp32\3.3.3\package.json`

```json
{
  "name": "framework-arduinoespressif32",
  "version": "3.3.3",
  "description": "Arduino Wiring-based Framework for the Espressif ESP32, ESP32-P4, ESP32-S and ESP32-C series of SoCs",
  "keywords": ["framework", "arduino", "espressif", "esp32"],
  "license": "LGPL-2.1-or-later",
  "repository": { "type": "git", "url": "https://github.com/espressif/arduino-esp32" }
}
```

### 1.5 esp32c6 预编译库目录

路径：`C:\arduino15\packages\esp32\tools\esp32-arduino-libs\idf-release_v5.5-f1a1df9b-v3\esp32c6`

顶层内容：

```text
bin
dependencies.lock
dio_qspi
flags
include
ld
lib
pioarduino-build.py
qio_qspi
sdkconfig
```

`ls -la` 摘要：

```text
drwxr-xr-x  ... bin
-rw-r--r--  ... dependencies.lock (12869 bytes)
drwxr-xr-x  ... dio_qspi
drwxr-xr-x  ... flags
drwxr-xr-x  ... include
drwxr-xr-x  ... ld
drwxr-xr-x  ... lib
-rw-r--r--  ... pioarduino-build.py (49267 bytes)
drwxr-xr-x  ... qio_qspi
-rw-r--r--  ... sdkconfig (117845 bytes)
```

`du -sh`：

```text
241M	C:\arduino15\packages\esp32\tools\esp32-arduino-libs\idf-release_v5.5-f1a1df9b-v3\esp32c6
```

### 1.6 libs 组合目录（qio_qspi / dio_qspi）

两者均存在：

```text
esp32c6/qio_qspi/
├── include/sdkconfig.h
└── libspi_flash.a

esp32c6/dio_qspi/
├── include/sdkconfig.h
└── libspi_flash.a
```

各自大小相同（libspi_flash.a 1,167,436 bytes）。对 qio_qspi/include/sdkconfig.h 与 dio_qspi/include/sdkconfig.h 做了 `cmp`：结果 `different`（两者配置头不完全相同，属官方两套 flash 组合）。

### 1.7 flags / sdkconfig 要点

`flags/` 文件清单：`S_flags`、`c_flags`、`cpp_flags`、`defines`、`includes`、`ld_flags`、`ld_libs`、`ld_scripts`。

要点（未贴全文）：

- `defines`：包含 `-DESP32_ARDUINO_LIB_BUILDER -DESP_PLATFORM -DIDF_VER=\"v5.5.1-418-gf1a1df9b2e\"` 等。
- `c_flags` / `cpp_flags`：riscv32 目标，`-march=rv32imac_zicsr_zifencei`。
- `includes`：包含 `esp_pm/include`、`esp_wifi/include` 等组件 include 路径。
- `ld_libs`：包含 `-lesp_pm`、`-lesp_wifi`、`-lbt` 等。
- 相关头文件存在：
  - `esp32c6/include/esp_pm/include/esp_pm.h`
  - `esp32c6/include/esp_wifi/include/esp_wifi_he.h`

### 1.8 预编译库工具/版本信息

`versions.txt`（节选）：

```text
lib-builder: master 64767cc
esp-idf: release/v5.5 f1a1df9b2e
arduino: master 8cb2659f1
tinyusb: master a6c16d147
...
```

`package.json`（工具包内）：

```json
{ "name": "framework-arduinoespressif32-libs", "version": "5.5.0+sha.f1a1df9b2e", ... }
```

## 2. sdkconfig 关键键核对

执行：

```bash
grep -n "CONFIG_PM_ENABLE\|CONFIG_FREERTOS_USE_TICKLESS_IDLE\|CONFIG_BT_LE_SLEEP_ENABLE\|CONFIG_PM_POWER_DOWN_CPU_IN_LIGHT_SLEEP\|CONFIG_PM_POWER_DOWN_PERIPHERAL_IN_LIGHT_SLEEP\|CONFIG_ESP_SLEEP_FLASH_LEAKAGE_WORKAROUND" \
  /c/arduino15/packages/esp32/tools/esp32-arduino-libs/idf-release_v5.5-f1a1df9b-v3/esp32c6/sdkconfig
```

原样结果：

```text
1070:# CONFIG_BT_LE_SLEEP_ENABLE is not set
1671:CONFIG_ESP_SLEEP_FLASH_LEAKAGE_WORKAROUND=y
1798:# CONFIG_PM_ENABLE is not set
1801:CONFIG_PM_POWER_DOWN_CPU_IN_LIGHT_SLEEP=y
1802:# CONFIG_PM_POWER_DOWN_PERIPHERAL_IN_LIGHT_SLEEP is not set
```

补充核对：`CONFIG_FREERTOS_USE_TICKLESS_IDLE` 在 sdkconfig 中**没有出现**（即未设置且未被 Kconfig 写出该行），全文无该键；当前 `grep` 结果中也没有它。与主代理探明结论一致：

- `CONFIG_PM_ENABLE`：未设置
- `CONFIG_FREERTOS_USE_TICKLESS_IDLE`：未设置（缺失/未写出）
- `CONFIG_BT_LE_SLEEP_ENABLE`：未设置
- `CONFIG_PM_POWER_DOWN_CPU_IN_LIGHT_SLEEP`：`y`
- `CONFIG_PM_POWER_DOWN_PERIPHERAL_IN_LIGHT_SLEEP`：未设置
- `CONFIG_ESP_SLEEP_FLASH_LEAKAGE_WORKAROUND`：`y`

## 3. 官方预编译库备份

先确认源大小：

```text
241M	C:\arduino15\packages\esp32\tools\esp32-arduino-libs\idf-release_v5.5-f1a1df9b-v3\esp32c6
```

执行：

```bash
cp -a \
  /c/arduino15/packages/esp32/tools/esp32-arduino-libs/idf-release_v5.5-f1a1df9b-v3/esp32c6 \
  /c/arduino15/packages/esp32/tools/esp32-arduino-libs/idf-release_v5.5-f1a1df9b-v3/esp32c6.orig-2026-09-07
```

结果：**成功**，`cp` 退出码 0。

备份路径：

```text
C:\arduino15\packages\esp32\tools\esp32-arduino-libs\idf-release_v5.5-f1a1df9b-v3\esp32c6.orig-2026-09-07
```

`du -sh` 备份目录：

```text
241M	C:\arduino15\packages\esp32\tools\esp32-arduino-libs\idf-release_v5.5-f1a1df9b-v3\esp32c6.orig-2026-09-07
```

未改 `C:\arduino15` 下其它文件。

## 4. lib-builder 准备度

### 4.1 WSL

```text
$ wsl.exe --status
EXIT=50
（输出为 UTF-16LE 中文，iconv 后为：）
未安装适用于 Linux 的 Windows 子系统。可通过运行 “wsl.exe --install” 进行安装。
有关详细信息，请访问 https://aka.ms/wslinstall

$ wsl.exe -l -v
EXIT=1
（同样提示未安装适用于 Linux 的 Windows 子系统）
```

结论：本机 **未安装 WSL / 无 Linux 发行版**。

### 4.2 本机已有 lib-builder / docker / git

```text
$ ls -d /c/esp32-arduino-lib-builder
ls: cannot access '/c/esp32-arduino-lib-builder': No such file or directory

$ ls -d /c/tools
ls: cannot access '/c/tools': No such file or directory

$ docker --version
docker: command not found
EXIT=127

$ git --version
git version 2.54.0.windows.1
EXIT=0
```

### 4.3 GitHub 可达性

```bash
timeout 15s git ls-remote https://github.com/espressif/esp32-arduino-lib-builder HEAD
```

结果：

```text
eb36458a92b39367e0d5c6b0864616a469cb8483	HEAD
EXIT=0
```

结论：**GitHub 可访问**，不需要离线镜像也可拉取 lib-builder。

### 4.4 arduino-cli 包镜像

`C:\arduino15\arduino-cli.yaml`：

```yaml
board_manager:
    additional_urls:
        - http://127.0.0.1:18765/package_esp32_index_cn.json
directories:
    data: C:\arduino15
```

本地镜像探测：

```bash
curl -s -m 3 http://127.0.0.1:18765/ | head -c 200
```

首次执行输出为空（head 未截到内容）。补充状态探测：

```text
http_code=000
EXIT=7
http_code=000
EXIT=7
```

结论：`127.0.0.1:18765` 当前**未实际提供可访问的本地镜像服务**（连接失败/拒绝）；配置里虽指向该地址，但现场不能依赖它作为下载源。GitHub 本身可达，A1 可从 GitHub 拉取。

## 5. M-A1 可行性结论

### 结论

**当前机器不可立即执行 lib-builder 重编。** 原因：

1. lib-builder 官方构建路径要求 Linux（Ubuntu/RPi/macOS）环境；
2. 本机 **未安装 WSL**；
3. 本机 **未安装 Docker**；
4. 本机 `/c/esp32-arduino-lib-builder` 不存在（未克隆）；
5. 本地包镜像 `127.0.0.1:18765` 不可达，但 **GitHub 可达**，因此安装/克隆/构建的网络障碍不大。

因此 M-A1 的阻塞项是**构建操作系统/发行版环境**，不是网络。需要先安装 WSL2 + Ubuntu（或另备 Linux 构建机）后才能跑 lib-builder。

### 下一步最小命令清单（主代理或后续 M-A1 执行）

安装/准备 WSL2 + Ubuntu 后（或在 Linux 构建机上）：

```bash
# 1) 安装 WSL2 + Ubuntu（Windows 管理员 PowerShell，需重启）
wsl --install -d Ubuntu

# 2) 在 Ubuntu 内安装依赖（按 lib-builder README 的 apt 清单）
sudo apt update
sudo apt install -y git wget curl flex bison gperf python3 python3-venv python3-pip \
  cmake ninja-build ccache libffi-dev libssl-dev dfu-util

# 3) 克隆 lib-builder
git clone https://github.com/espressif/esp32-arduino-lib-builder
cd esp32-arduino-lib-builder

# 4) 与已装 core 3.3.3/IDF v5.5 对齐后构建
#    注意：先用 -b menuconfig 或对照 v5.5 确认选项名；-b idf_libs qio 80m lightsleep 的精确参数
#    需按 lib-builder 当前 usage/configs/builds.json 再核对。
./build.sh -A <与 core 3.3.3 匹配的 arduino-esp32 标签/分支> \
  -I release/v5.5 -t esp32c6 -b idf_libs qio 80m lightsleep
```

若主代理决定继续使用当前 Windows 环境，也可选择“另备 Linux 盒子”路线；产物回拷路径为：

```text
<lib-builder>/out/tools/esp32-arduino-libs/esp32c6/
→ C:\arduino15\packages\esp32\tools\esp32-arduino-libs\idf-release_v5.5-f1a1df9b-v3\esp32c6\
```

覆盖前官方原目录已有备份：`esp32c6.orig-2026-09-07`。

## 6. 遗留待办

- **M-A1 重编 libs 未执行**：因为本机没有 WSL/Linux/Docker 构建环境；当前阶段仅完成 M-A0 摸底和官方库备份。
- 待安装 WSL2 + Ubuntu（或另备 Linux 构建机）后执行 A1.1 的构建步骤，并在 sdkconfig 静态验证 `CONFIG_PM_ENABLE=y`、`CONFIG_FREERTOS_USE_TICKLESS_IDLE=y`。
- A1.2 中计划新增的 `defconfig.lightsleep` 文件尚未创建（本任务不修改源码；由后续 M-A1 在 lib-builder 配置侧新增）。
- 本地包镜像服务 `127.0.0.1:18765` 当前不可达；若后续 arduino-cli 安装/更新依赖该镜像，需先恢复镜像服务，或确认 GitHub 直接下载可用。
