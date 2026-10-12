# Mi Fitness Sync

本仓库基于 [kevinkwee/Mi-Fitness-Sync](https://github.com/kevinkwee/Mi-Fitness-Sync)，增加 **小米跑步同步** 原生 Mac 应用，面向中国区小米运动健康与国行 Watch S4。支持筛选活动、只同步跑步、Strava 网页自动上传和重复检测，沿用原项目 MIT 许可证。

**桌面版 1.0.3**：[下载发行版](https://github.com/bestzbf/Mi-Fitness-Sync/releases/tag/v1.0.3)。选择 `XiaomiRunSync-v1.0.3-macos-arm64.zip`，解压后将 **小米跑步同步.app** 放入用户的 `~/Applications` 文件夹。需要 Apple Silicon Mac 和 macOS 14 或以上，不需要安装 Python。此构建采用本地签名，未经过 Apple 公证；首次打开时 macOS 可能要求确认来源。

下面保留原版 CLI 的说明；桌面应用的操作和构建方法见“国行 Watch S4：只同步跑步”。

An unofficial Python CLI for accessing Mi Fitness workout data and manually syncing activities that failed to reach Strava.

## Why This Exists

Mi Fitness is the Android app used by Xiaomi wearable devices. When a workout is recorded on the watch, the data is synced to the phone over Bluetooth/BLE and then uploaded to the Mi Fitness cloud under the user's Xiaomi account.

Mi Fitness officially supports third-party integrations such as Strava. In practice, workout activities are sometimes not uploaded to Strava even though they appear inside Mi Fitness.

I created this project because I wanted a way to manually sync workouts that were recorded in Mi Fitness but never made it to Strava.

## What This Is Good For

This is mainly useful if you want to:

1. Confirm that your workouts really exist in Mi Fitness cloud storage
2. Inspect recent activities when Mi Fitness fails to push them to Strava
3. Pull activity data yourself instead of waiting for the official sync to work
4. Export workouts to GPX, TCX, or FIT for manual backup or re-upload
5. Upload workouts directly to Strava with duplicate detection

## Install

Python 3.12+ is required.

Install in editable mode:

```bash
python -m pip install -e .
```

Then run with:

```bash
mi-fitness-sync --help
```

Or without installing:

```bash
python main.py --help
```

## 国行 Watch S4：只同步跑步

本地版本支持中国区（`CN`），只选择户外跑步（类型 1）、跑步机（类型 3）和越野跑（类型 5），跳过骑行、其他运动、已删除及无法识别类型的记录。已用实际账号完成扫码登录和中国区云端记录读取；具体跑步轨迹、心率和 FIT 数据以云端实际返回内容为准。

### 默认：免费自动同步

本机已封装原生 Mac 应用 **小米跑步同步.app**，安装在用户的 `~/Applications`，可直接双击打开。独立包在 `dist/小米跑步同步.app`，包含运行环境，日常使用无需 Python 或终端。应用适用于 Apple Silicon Mac、macOS 14 及以上。

应用按如下流程操作：

1. 选择今天、近三天、近一周或近一月，点击 **刷新活动**。
2. **隐藏骑行** 默认开启，也可取消以查看骑行；骑行始终不可选、不可同步。**仅显示跑步** 可以隐藏其他运动。
3. 勾选需要同步的新跑步，点击 **开始同步到 Strava**；每批最多 15 条，只处理本次勾选的跑步。
4. 首次在软件内置的 Strava 官方页面完成登录。已有登录时，软件会自动选择文件、等待解析并点击保存，无需手动操作网页。
5. 每条成功后自动记录对应的 Strava 活动 ID，并把文件保留到 **已上传**。Strava 判定重复的活动自动跳过；部分失败时停止，重试会跳过已确认的部分。

服务器要求验证账号时，仍需在官方页面完成验证。需要自行修复旧活动时，可点击 **打开 Strava** 手动处理；手动上传后在右上角设置菜单选择 **手动上传后标记选中项已完成…**。

刷新只读取云端活动，不导出或上传。应用会保留原设置的开始时间，配置前的活动可以查看，但不能同步。设置和本地查重记录保存在 `~/Library/Application Support/XiaomiRunSync`；小米登录仍保存在 `~/.mi_fitness_sync/auth`，不会打包进应用。

1.0.1 增加小米会话自动续期：云端返回 HTTP 401 时，用本机保存的 `passToken` 和原设备 ID 换取新会话，再重试一次。新 `serviceToken` 与对应的 `ssecurity` 会一起原子写入，后续重开应用继续复用。网络中断或小米服务异常不会清空凭证，也不会要求重新扫码；只有小米拒绝长期登录凭证或要求安全验证时才需要扫码。凭证持续保存在本机，服务器决定其有效期。

1.0.2 修复 Google 登录弹窗和 Strava 会话保存：OAuth 弹窗保留与原页面的关联并共享固定 WebKit 配置，上传页可复用；Strava 的会话 Cookie 保存到 macOS 钥匙串，在重新打开软件时恢复。Google 的 Cookie 不复制到钥匙串，密码不保存。服务器撤销或过期的登录仍需要重新验证。

1.0.2 同时修复国行 Watch S4 新版户外运动二进制数据中的旧字段重复解析，保留心率采样，并在 GPS 尚未定位或出现缺口时继续把真实心率写入 FIT。云端摘要表明有心率而详细数据未读取成功时，会停止导出并提示重试。已上传的 Strava 活动不会被本地修复自动修改；需使用含心率的修复 FIT 替换对应的旧活动。

1.0.3 增加免费网页自动同步。每次向官网上传一个所选 FIT，确认页面识别为跑步后点击官方保存按钮；只有保存后的活动页面与本次活动 ID 相符，或官方返回重复活动 ID 时，才记录完成。每条结果先原子写入批次确认文件，再交给本机服务校验并归档，重启时可恢复未提交的确认结果。网页结构变化、保存失败或本机记录失败时停止本批次，保留已完成记录和待处理文件。钥匙串操作不弹出阻塞程序的验证窗口，固定 WebKit 配置继续保留官网会话。

查重包含源活动 ID、本地确认上传记录以及跑步开始时间 ±6 秒的匹配；同时间骑行不参与匹配。已导出但尚未确认上传的文件可以再次打开上传流程，不会重复生成 FIT。若 Strava 上还有经其他工具上传的跑步，可通过 **导入 Strava 查重记录** 选择账号数据导出的 `activities.csv`，对比其中的 Run、TrailRun、VirtualRun 等跑步类型。带时区的时间会自动换算；无时区 CSV 可选 UTC 或北京时间。无法解析的跑步时间会停止导入，保留已有索引。

免费模式通过官方上传页自动操作，不实时调用 Strava API，也不扫描账号全部远端活动；官网的重复检测在上传处理时运行。可选的 CSV 索引可在上传前跳过已有跑步，但只覆盖导入时的记录。导出完成不会标记为上传成功；每条需收到官网可核实的结果后自动标记。Strava 账号数据下载方法见[官方说明](https://support.strava.com/en-us/articles/15401919-how-do-i-export-my-strava-data)。

重新构建本机应用：

```bash
.venv/bin/python -m pip install 'pyinstaller>=6.16,<7'
.venv/bin/python scripts/build_mac_app.py --install
```

如果没有已安装的应用，下面的终端向导仍可使用。

直接双击项目里的 **`同步跑步.command`**。首次需要用小米手机扫描电脑二维码，并在手机确认使用与手表绑定的小米账号。程序会直接接收登录结果，无需在浏览器反复验证或输入小米密码。

免费模式不需要创建 Strava API 应用。Strava [官方上传说明](https://support.strava.com/en-us/articles/15402173-how-do-i-bulk-upload-activities-to-strava)明确免费账号可上传 FIT 文件，每批最多 15 个，每个文件最大 25 MB。

当前配置按用户要求只处理配置完成之后的新跑步，不回填历史跑步。终端向导的起始时间保存在 `.local/free-sync-settings.json`；升级桌面应用时保留原值，应用设置保存在上文的 Application Support 文件夹。

使用终端向导时，每次跑步：

1. 先将手表记录同步到手机小米运动健康和小米云端。
2. 在 Mac 双击 **`同步跑步.command`**，程序自动导出新跑步到 **`待上传跑步`** 文件夹。
3. 有文件时会打开 Strava 的普通文件上传页和文件夹；在网页选择 FIT 文件并保存。
4. 上传成功后，把对应 FIT 文件移到其他文件夹，例如 `待上传跑步/已上传`。尚未上传的文件留在原处，下次仍可找到。

程序记录已经导出的源活动，重复运行不会重新导出这些活动。本地导出记录不代表已上传 Strava，也不查询 Strava 远端重复记录。网页上传和保存需要自己完成。没有新跑步时会正常完成检查，不要求再次登录或创建 API 应用。

若已开启小米运动健康到 Strava 的直接同步，请先在 App 中断开这条连接，避免它另行上传手表骑行；码表继续按原方式同步骑行到 Strava。

也可在项目目录手动导出指定范围：

```bash
.venv/bin/mi-fitness-sync export-runs --since 2026-10-08T00:00:00+08:00

# 只导出户外跑步，排除跑步机和越野跑
.venv/bin/mi-fitness-sync export-runs --since 2026-10-08T00:00:00+08:00 --outdoor-only
```

`--output-dir` 可改变输出文件夹；导出历史与该文件夹内的 `.exported.json` 一起保存，请保留该文件。`--limit` 默认 1000，指扫描的全部运动条数（包括骑行）。达到上限会停止，可缩小日期范围或增大上限。

### 可选：通过自己的 Strava API 应用自动上传

如果你已有可用的 Strava API 应用，可以采用原项目的 OAuth 连接。若 API 设置页要求订阅，使用上面的免费网页模式即可。不要将 Client Secret 或账号密码发送到聊天中。

```bash
.venv/bin/mi-fitness-sync strava-login
.venv/bin/mi-fitness-sync sync-runs
.venv/bin/mi-fitness-sync sync-runs --upload
```

`sync-runs` 默认预览最近 30 天的跑步，`--upload` 正式上传缺失记录；中国区为默认值。加 `--outdoor-only` 只选择户外跑步，`--since` 与 `--until` 指定时间范围。时间未指定时区时按电脑本地时区解释。

API 模式仅匹配 Strava 上的 Run、TrailRun、VirtualRun，按 UTC 开始时间 ±6 秒检查重复；同一时间的码表骑行不影响匹配。重复检查失败会停止上传，每条上传会留下本地 FIT 备份。

FIT 导出继承原项目的 GPS 平滑设置，可用 `--no-smooth` 关闭。跑步机没有 GPS 时，需有带时间戳的运动样本才能导出 FIT。没有创建定时任务。

## Strava API Setup

Before using any Strava commands you need a Strava API application:

1. Go to https://www.strava.com/settings/api and create an application
2. Set the **Authorization Callback Domain** to `localhost` (just the domain — no protocol, no port, no path)

The CLI uses `http://localhost:{port}/callback` as the redirect URI. Strava only matches on the domain portion, so the callback domain setting must be exactly `localhost`.

## Quick Start

```bash
# 1. Log in to your Mi / Xiaomi account (prompts for email and password)
mi-fitness-sync login

# 2. Check auth status
mi-fitness-sync auth-status

# 3. List recent workouts
mi-fitness-sync list-activities --limit 10

# 4. List activities and show which ones are already on Strava
mi-fitness-sync list-activities --limit 10 --strava

# 5. View normalized detail for one activity
mi-fitness-sync activity-detail sid:key:1717200000 --json

# 6. Export a workout
mi-fitness-sync export-activity sid:key:1717200000 --format gpx --output run.gpx

# 7. Authenticate with Strava (prompts for client ID and secret)
mi-fitness-sync strava-login

# 8. Upload a workout to Strava
mi-fitness-sync upload-to-strava sid:key:1717200000
```

All credentials are entered via CLI arguments or interactive prompts — there are no environment variables.

## Commands

Commands that access Mi Fitness accept `--state-path` to override the default auth state file location. Commands that access Strava accept `--strava-token-path` to override the default Strava token file location.

### `login`

Logs into Xiaomi Passport for the Mi Fitness service and saves the auth state locally. Email and password can be passed as arguments or entered interactively when omitted. The password prompt does not echo input.

If Xiaomi requires extra verification, the CLI supports the interactive flows it can complete locally:

1. Captcha challenges download an image to the local captcha directory and try to open it with the OS default image viewer
2. Step-2 verification prompts for the email/SMS code in the terminal
3. Browser or app notification approval is not automated; the CLI prints the verification URL when Xiaomi requires it

Examples:

```bash
mi-fitness-sync login
mi-fitness-sync login --email you@example.com --password your-password
```

Flags:

| Flag | Description |
|------|-------------|
| `--email` | Mi / Xiaomi account email (prompted if omitted) |
| `--password` | Mi / Xiaomi account password (prompted securely if omitted) |
| `--state-path` | Override the persisted auth state file path |

### `logout`

Deletes the saved local Mi Fitness auth state.

```bash
mi-fitness-sync logout
```

Flags:

| Flag | Description |
|------|-------------|
| `--state-path` | Override the persisted auth state file path |

### `auth-status`

Shows the currently saved Mi Fitness auth state.

```bash
mi-fitness-sync auth-status
mi-fitness-sync auth-status --json
```

Flags:

| Flag | Description |
|------|-------------|
| `--json` | Print the full auth state as JSON |
| `--state-path` | Override the persisted auth state file path |

### `list-activities`

Lists workout activities from the Mi Fitness cloud.

```bash
mi-fitness-sync list-activities --limit 10
mi-fitness-sync list-activities --since 2024-01-01 --json
mi-fitness-sync list-activities --since 2026-03-20 --strava
mi-fitness-sync list-activities --since 1717200000 --until 1719800000 --limit 50
```

Flags:

| Flag | Description |
|------|-------------|
| `--since` | Inclusive start time (unix seconds or ISO-8601) |
| `--until` | Inclusive end time (unix seconds or ISO-8601) |
| `--limit` | Maximum activities to return (default: 20) |
| `--category` | Mi Fitness category filter string |
| `--country-code` | Two-letter country override (e.g. `ID`, `GB`, `US`); mapped to the Mi Fitness region automatically |
| `--strava` | Show whether each activity is already uploaded to Strava |
| `--strava-token-path` | Override the Strava token file path (used with `--strava`) |
| `--json` | Print activities as JSON |
| `--verbose` | Enable debug logging |
| `--state-path` | Override the persisted auth state file path |

When `--country-code` is omitted, the CLI uses automatic Mi Fitness region detection.

### `activity-detail`

Fetches the normalized detail payload for one activity.

```bash
mi-fitness-sync activity-detail sid:key:1717200000
mi-fitness-sync activity-detail sid:key:1717200000 --country-code ID --json
```

Flags:

| Flag | Description |
|------|-------------|
| `activity_id` | Activity ID from `list-activities` (positional, required) |
| `--country-code` | Two-letter country override |
| `--json` | Print normalized detail as JSON |
| `--no-cache` | Disable the local FDS binary cache |
| `--cache-dir` | Override the local FDS cache directory |
| `--verbose` | Enable debug logging |
| `--state-path` | Override the persisted auth state file path |

Detail lookup behavior:

1. Prefers the richer workout JSON payload when Mi Fitness exposes it
2. Checks the Mi Fitness cloud for downloadable binary workout data (FDS files containing GPS tracks, heart rate, etc.)
3. Can synthesize detail from FDS sport samples or GPS track points when the JSON payload is missing
4. Attaches recoverable FDS sport-report and recovery-rate data (not consumed by exporters)
5. Fails if neither the JSON detail nor FDS binary data is available

### `export-activity`

Exports one activity to GPX, TCX, or FIT.

```bash
mi-fitness-sync export-activity sid:key:1717200000 --format gpx --output run.gpx
mi-fitness-sync export-activity sid:key:1717200000 --format tcx --output run.tcx.gz --gzip
mi-fitness-sync export-activity sid:key:1717200000 --format gpx --smooth-mode full --outlier-speed 7:30
mi-fitness-sync export-activity sid:key:1717200000 --format fit
```

Flags:

| Flag | Description |
|------|-------------|
| `activity_id` | Activity ID from `list-activities` (positional, required) |
| `--format` | Export format: `gpx`, `tcx`, or `fit` (required) |
| `--output` | Destination file path (default: `~/.mi_fitness_sync/exports/<sanitized_title>_<local_start_time>.<format>`) |
| `--gzip` | Gzip-compress the output |
| `--country-code` | Two-letter country override |
| `--no-cache` | Disable the local FDS binary cache |
| `--cache-dir` | Override the local FDS cache directory |
| `--no-smooth` | Disable GPS smoothing entirely |
| `--outlier-speed` | Max plausible speed for outlier detection, as km/h (for example `180` or `180kmh`) or pace (for example `7:30` or `7:30/km`) |
| `--smooth-mode` | Smoothing strategy: `match` (default, converge toward the Mi Fitness summary distance) or `full` (apply the strongest smoothing pass) |
| `--verbose` | Enable debug logging |
| `--state-path` | Override the persisted auth state file path |

Export behavior:

1. GPX requires GPS track points in the detail payload
2. TCX and FIT prefer track points but fall back to timestamped sport samples without coordinates
3. GPS smoothing is enabled by default for exports that include GPS coordinates; it only adjusts latitude/longitude and leaves timestamps, heart rate, cadence, and other fields unchanged
4. `match` mode tries to bring the smoothed GPS distance closer to the Mi Fitness summary distance, while `full` applies the largest valid smoothing window after outlier cleanup
5. FIT generation is best-effort — only fields present in the Mi Fitness source are included

### `strava-login`

Authenticates with Strava using the OAuth2 authorization code flow. Opens a browser for authorization, then stores the tokens locally. Client ID and secret can be passed as arguments or entered interactively.

```bash
mi-fitness-sync strava-login
mi-fitness-sync strava-login --client-id YOUR_ID --client-secret YOUR_SECRET
```

Flags:

| Flag | Description |
|------|-------------|
| `--client-id` | Strava API client ID (prompted if omitted) |
| `--client-secret` | Strava API client secret (prompted if omitted) |
| `--port` | Local port for the OAuth callback server (default: 5478) |
| `--strava-token-path` | Override the Strava token file path |

The flow:

1. Starts a temporary local HTTP server on `localhost:{port}`
2. Opens the Strava authorization page in your browser
3. After authorization, Strava redirects to the local server with an auth code
4. Exchanges the code for access and refresh tokens
5. Saves tokens to `~/.mi_fitness_sync/strava/tokens.json`

The CLI refreshes Strava access tokens automatically using the stored refresh token whenever the saved access token is near expiry.

### `strava-status`

Shows the currently saved Strava auth state.

```bash
mi-fitness-sync strava-status
```

Flags:

| Flag | Description |
|------|-------------|
| `--strava-token-path` | Override the Strava token file path |

### `strava-logout`

Revokes the Strava access token via the Strava API and deletes the local token file. Exits cleanly if no tokens exist. Deletes local tokens even if remote revocation fails.

```bash
mi-fitness-sync strava-logout
```

Flags:

| Flag | Description |
|------|-------------|
| `--strava-token-path` | Override the Strava token file path |

### `upload-to-strava`

Uploads one Mi Fitness activity to Strava as a FIT file. The FIT file is also saved locally.

```bash
mi-fitness-sync upload-to-strava sid:key:1717200000
mi-fitness-sync upload-to-strava sid:key:1717200000 --output run.fit
mi-fitness-sync upload-to-strava sid:key:1717200000 --smooth-mode full --outlier-speed 180kmh
mi-fitness-sync upload-to-strava sid:key:1717200000 --skip-duplicate-check
```

Flags:

| Flag | Description |
|------|-------------|
| `activity_id` | Activity ID from `list-activities` (positional, required) |
| `--output` | Local FIT file save path (default: `~/.mi_fitness_sync/exports/<sanitized_title>_<local_start_time>.fit`) |
| `--country-code` | Two-letter country override |
| `--skip-duplicate-check` | Skip checking Strava for existing activities with a similar start time |
| `--strava-token-path` | Override the Strava token file path |
| `--no-cache` | Disable the local FDS binary cache |
| `--cache-dir` | Override the local FDS cache directory |
| `--no-smooth` | Disable GPS smoothing entirely before generating the FIT upload |
| `--outlier-speed` | Max plausible speed for outlier detection, as km/h or pace |
| `--smooth-mode` | Smoothing strategy: `match` (default) or `full` |
| `--verbose` | Enable debug logging |
| `--state-path` | Override the persisted Mi Fitness auth state file path |

Upload behavior:

1. Fetches the activity detail from Mi Fitness
2. Renders to FIT format with the same optional smoothing controls as `export-activity`, then saves the file locally
3. Checks Strava for existing activities within ±5 minutes of the start time (unless `--skip-duplicate-check` is set)
4. If potential duplicates are found, prompts for confirmation before uploading
5. Uploads the FIT file to Strava and polls until processing completes
6. Prints the Strava activity URL on success
7. The sport type is mapped from Mi Fitness to the closest Strava equivalent; unmapped types let Strava auto-detect

## Project Layout

```
src/mi_fitness_sync/
  activity/    Activity listing, detail, region routing, normalization
  auth/        Xiaomi Passport auth and persistence
  cli/         Command-line entry point
  export/      GPX, TCX, and FIT rendering
  fds/         Mi Fitness FDS binary parsing and caching
  strava/      Strava OAuth2 auth and upload
tests/         Per-module test files mirroring the package structure
```

Example:

```bash
python -m mi_fitness_sync logout
```

## Local Auth Storage

By default, Mi Fitness auth state is stored in the user profile under `~/.mi_fitness_sync/auth/auth.json`.

You can override that location with `--state-path`.

Strava tokens are stored under `~/.mi_fitness_sync/strava/tokens.json`. You can override that location with `--strava-token-path`.

When Xiaomi requires a captcha, the image is saved under `~/.mi_fitness_sync/captcha/` and the CLI tries to open it with your OS default image app. The file is deleted on a best-effort basis after you respond; if deletion fails, the CLI prints the saved path so you can remove it manually.

Both files contain sensitive credentials and should not be shared or committed to version control.

## Limitations

1. This is an unofficial project and is not affiliated with Xiaomi, Mi Fitness, or Strava.
2. Parts of the Xiaomi login flow had to be pieced together from app behavior and decompiled code.
3. Xiaomi may change endpoints, cookies, signatures, or response formats at any time.
4. Some accounts may require captcha or notification approval flows that are not fully automated here. Step-2 verification (SMS/email code) is supported interactively.
5. Detail retrieval currently depends on the raw `huami_sport_record` payload shape used by the Android app; Xiaomi may change that format without notice.
6. FDS download-url metadata does not guarantee the corresponding cloud object is still downloadable, so some workouts still cannot be enriched from FDS.
7. GPX export is unavailable for workouts where Mi Fitness does not expose GPS track points.
8. TCX and FIT require timestamped export points. The exporter uses GPS track points directly when they are present in the normalized detail and otherwise falls back to timestamped sport samples; if neither source exists, those exports are unavailable.
9. FIT export is best-effort and may omit fields when Mi Fitness does not provide enough source data for a fuller activity file.

## Security Notes

1. The CLI accepts Mi account and Strava client credentials either on the command line or via interactive prompts.
2. Command-line arguments may be persisted in shell history or process listings depending on your environment, so interactive prompts are safer on shared machines.
3. The persisted Mi Fitness auth state and Strava token files contain sensitive session data and should be protected.

If you use this on a shared machine, treat the local auth state like a credential.

## License
This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
