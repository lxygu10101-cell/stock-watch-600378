# 昊华科技（600378）到价邮件提醒

轮询东方财富行情，价格**上穿/下穿**目标价时发一封邮件到 QQ 邮箱（也支持 ntfy）。纯标准库，无第三方依赖。

## 当前配置

- 标的：昊华科技（600378）
- 目标价：**40.00 元**
- 方向：涨到（上穿）时提醒
- 通知：QQ 邮箱（SMTP）
- 触发后：价格回落到 39.80（目标价下方 0.5%）以下，再次上穿才会重新提醒

```
GitHub Actions 定时(5分钟)
        ↓
拉东方财富行情 → 失败自动切腾讯源
        ↓
校验交易时段 / 行情时间戳新鲜度（节假日自动静默）
        ↓
状态机：上穿才触发 + 回差重新武装 + 冷却去重
        ↓
SMTP 发信 → QQ 邮箱 App 推送
```

## 文件

| 文件 | 用途 |
|---|---|
| `watch.py` | 主程序：拉行情 → 校验 → 状态机 → 发信/推送 |
| `.github/workflows/watch.yml` | GitHub Actions 定时任务（周一至周五，每 5 分钟） |
| `.env.example` | 本地配置模板 |
| `.env.local` | 你自己的本地配置（含授权码，已被 git 忽略） |
| `state.json` | 运行时生成，记录上次价格和是否已触发（由 Actions 缓存持久化） |

## 触发逻辑

- **上穿**：上次价格 < 目标价 ≤ 本次价格 → 发信；之后价格回落到目标价下方 0.5%（回差可调）才重新武装。
- **下穿**：方向相反。
- 同一信号不会每 5 分钟重复轰炸；`MIN_INTERVAL_MIN` 控制最短提醒间隔。
- 首次运行只记录基准价，不补发历史信号；`ALERT_IF_ALREADY_MET=1` 可改为「已高于目标价也立刻提醒」。
- 只在北京时间工作日 09:25–11:30、13:00–15:00 判断；节假日靠行情时间戳是否过期自动静默。

## 本地试跑

```bash
cd /data/user/0/cc.codroid/files/home/workspace/stock-watch

# 只看逻辑，不发信，也不管交易时段
TARGET_PRICE=40.00 DIRECTION=up python3 -I watch.py --dry-run --force
```

### 先用 QQ 邮箱把发信通道测通

1. 手机浏览器打开 `https://mail.qq.com`（若自动跳 App 或显示精简版，在浏览器菜单里勾选 **「桌面版网站」**）→ 设置 → 账号与安全 → 开启 **IMAP/SMTP 服务** → 按提示验证，拿到 16 位**授权码**（不是 QQ 密码）。
2. 打开 `/data/user/0/cc.codroid/files/home/workspace/stock-watch/.env.local`，把 `SMTP_PASS` 换成授权码。
3. 加载配置并发一封测试邮件：

   ```bash
   set -a; source .env.local; set +a
   python3 watch.py --test-mail
   ```

   没收到先看垃圾箱，再检查授权码是否复制完整。

## 部署到 GitHub Actions

1. 新建一个 **public** 仓库（私有仓库的免费 Actions 分钟数不够每 5 分钟跑一个月），把本目录内容推上去。
2. 仓库 → Settings → Secrets and variables → Actions，添加 Secrets：

   | 名称 | 值 |
   |---|---|
   | `SMTP_HOST` | `smtp.qq.com` |
   | `SMTP_PORT` | `465` |
   | `SMTP_USER` | 你的 QQ 邮箱 |
   | `SMTP_PASS` | QQ 邮箱授权码 |
   | `MAIL_TO` | 收件邮箱 |
   | `MAIL_FROM` | 一般同 `SMTP_USER` |

   Variables（可省略，有默认值）：`TARGET_PRICE=40.00`、`DIRECTION=up`。
3. Actions 页面启用 `stock-watch`，点 **Run workflow** 手动跑一次验证。

> 公开仓库里的文件都会被人看到。**授权码和邮箱只放 Secrets**，不要写进 README 或代码；`.env.local` 已被忽略。

## 必须知道的限制

- **GitHub Actions 的定时最小 5 分钟，实际常有 5–20 分钟延迟。** 价格在两次轮询之间冲上目标又落回，会漏报。要更及时，换 1 分钟粒度的 Cloudflare Workers Cron 或自己的常开机器。
- GitHub 会**停用 60 天无提交的仓库里的定时任务**，长期用需要偶尔提交一次，或换托管方式。
- 当前网络下 `github.com` 的 DNS 被拦截，从这台手机直接部署需要 VPN 或换台设备操作；GitHub 的服务器本身发信不受影响。
- 邮箱服务商可能把来自 GitHub Actions IP 的邮件判为垃圾邮件，首次部署后检查垃圾箱并把发件人加入白名单。
- 这不是交易系统，只是提醒；市价、停牌、涨跌停都会影响实际成交。

## ntfy 备选通道

代码保留 ntfy 支持，但当前网络无法解析 `ntfy.sh`，需要 VPN 才能用。用法：`.env.local` 里设 `NOTIFY_CHANNEL=ntfy` 和 `NTFY_TOPIC`，测试命令改为 `python3 watch.py --test-notify`。
