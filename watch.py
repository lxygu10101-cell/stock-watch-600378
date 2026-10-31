#!/usr/bin/env python3
"""昊华科技(600378) 到价提醒 —— 外部定时运行版。

只用 Python 标准库，配置全部来自环境变量，可跑在 GitHub Actions、云函数或 VPS 上。

    TARGET_PRICE=32.0 DIRECTION=up MAIL_TO=me@example.com \
    SMTP_HOST=smtp.qq.com SMTP_PORT=465 SMTP_USER=me@qq.com SMTP_PASS=授权码 \
    python3 watch.py

本地看逻辑（不发信、不看交易时段）:
    python3 watch.py --dry-run --force
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

BEIJING = timezone(timedelta(hours=8))
UA = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


# ---------------------------------------------------------------- 配置
def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


class Config:
    def __init__(self) -> None:
        self.secid = env("STOCK_SECID", "1.600378")          # 1=沪市, 0=深市
        self.target = float(env("TARGET_PRICE", "0"))
        self.direction = (env("DIRECTION", "up") or "up").lower()  # up / down / both
        self.hysteresis_pct = float(env("HYSTERESIS_PCT", "0.5"))
        self.alert_if_already_met = env("ALERT_IF_ALREADY_MET", "0") == "1"
        self.state_file = Path(env("STATE_FILE", "state.json"))
        self.min_interval_min = float(env("MIN_INTERVAL_MIN", "10"))
        self.stale_seconds = int(env("STALE_SECONDS", "300"))

        self.smtp_host = env("SMTP_HOST")
        self.smtp_port = int(env("SMTP_PORT", "465"))
        self.smtp_user = env("SMTP_USER")
        self.smtp_pass = env("SMTP_PASS")
        self.mail_from = env("MAIL_FROM", self.smtp_user)
        self.mail_to = env("MAIL_TO")

        # 通知通道：ntfy（默认）或 email
        self.notify_channel = (env("NOTIFY_CHANNEL", "ntfy") or "ntfy").lower()
        self.ntfy_server = (env("NTFY_SERVER", "https://ntfy.sh") or "https://ntfy.sh").rstrip("/")
        self.ntfy_topic = env("NTFY_TOPIC")
        self.ntfy_priority = int(env("NTFY_PRIORITY", "4"))

    @property
    def click_url(self) -> str:
        """点开通知时跳转的行情页。"""
        prefix = "sh" if self.secid.startswith("1.") else "sz"
        code = self.secid.split(".", 1)[1]
        return f"https://quote.eastmoney.com/{prefix}{code}.html"


# ---------------------------------------------------------------- 行情
class Quote:
    def __init__(self, code, name, price, prev_close, open_, high, low, ts, source):
        self.code, self.name = code, name
        self.price, self.prev_close = price, prev_close
        self.open, self.high, self.low = open_, high, low
        self.ts = ts              # 行情更新时刻（北京时间 aware datetime）
        self.source = source

    @property
    def pct(self) -> float:
        return (self.price / self.prev_close - 1) * 100 if self.prev_close else 0.0

    def summary(self) -> dict:
        return {
            "code": self.code, "name": self.name, "price": self.price,
            "prev_close": self.prev_close, "pct": round(self.pct, 2),
            "high": self.high, "low": self.low,
            "ts": self.ts.strftime("%Y-%m-%d %H:%M:%S"), "source": self.source,
        }


def http_get(url: str, *, gbk: bool = False, tries: int = 4) -> str:
    last: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}
            )
            with urllib.request.urlopen(req, timeout=10,
                                        context=ssl.create_default_context()) as r:
                raw = r.read()
            return raw.decode("gbk", "replace") if gbk else raw.decode("utf-8", "replace")
        except Exception as e:                       # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"请求失败 {url}: {last}")


def fetch_eastmoney(secid: str) -> Quote:
    url = (
        "https://push2.eastmoney.com/api/qt/stock/get?"
        f"secid={secid}&fields=f43,f44,f45,f46,f57,f58,f60,f86"
    )
    data = json.loads(http_get(url)).get("data")
    if not data:
        raise RuntimeError("东财返回空数据（可能停牌或代码错误）")

    def num(key: str) -> float:
        v = data.get(key)
        if v in (None, "-", ""):
            raise RuntimeError(f"字段 {key} 无有效值")
        return float(v) / 100.0

    return Quote(
        data.get("f57"), data.get("f58"),
        num("f43"), num("f60"), num("f46"), num("f44"), num("f45"),
        datetime.fromtimestamp(int(data["f86"]), BEIJING), "eastmoney",
    )


def fetch_tencent(secid: str) -> Quote:
    """备用源。secid 1.600378 -> sh600378."""
    prefix = "sh" if secid.startswith("1.") else "sz"
    code = secid.split(".", 1)[1]
    body = http_get(f"http://qt.gtimg.cn/q={prefix}{code}", gbk=True)
    fields = body.split('"')[1].split("~")
    return Quote(
        code, fields[1], float(fields[3]), float(fields[4]), float(fields[5]),
        float(fields[33]), float(fields[34]),
        datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=BEIJING),
        "tencent",
    )


def fetch_quote(secid: str) -> Quote:
    try:
        return fetch_eastmoney(secid)
    except Exception as e:                           # noqa: BLE001
        print(f"[warn] 东财失败，切备用源: {e}", file=sys.stderr)
        return fetch_tencent(secid)


# ---------------------------------------------------------------- 交易时段
def in_session(now: datetime) -> bool:
    """北京时间工作日 09:25-11:30、13:00-15:00。"""
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 25 <= hm <= 11 * 60 + 30) or (13 * 60 <= hm <= 15 * 60)


# ---------------------------------------------------------------- 状态机
def evaluate(state: dict, quote: Quote, cfg: Config) -> list[str]:
    """返回触发的方向列表。就地更新 state。"""
    cur = quote.price
    prev = state.get("last_price")
    target = cfg.target
    band = target * cfg.hysteresis_pct / 100.0
    fired: list[str] = []

    if cfg.direction in ("up", "both"):
        armed = state.get("armed_up", True)
        if prev is not None and armed and prev < target <= cur:
            fired.append("up")
            armed = False
        elif cur < target - band:
            armed = True
        state["armed_up"] = armed

    if cfg.direction in ("down", "both"):
        armed = state.get("armed_down", True)
        if prev is not None and armed and prev > target >= cur:
            fired.append("down")
            armed = False
        elif cur > target + band:
            armed = True
        state["armed_down"] = armed

    # 首次运行：默认只记录基准价，不补发历史信号
    if prev is None and cfg.alert_if_already_met:
        if cfg.direction in ("up", "both") and cur >= target:
            fired.append("up")
            state["armed_up"] = False
        if cfg.direction in ("down", "both") and cur <= target:
            fired.append("down")
            state["armed_down"] = False

    state["last_price"] = cur
    state["last_ts"] = quote.ts.isoformat()
    return fired


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:                           # noqa: BLE001
        print(f"[warn] 状态文件损坏，重建: {e}", file=sys.stderr)
        return {}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 邮件
def build_mail(quote: Quote, direction: str, cfg: Config) -> tuple[str, str]:
    verb = "上穿" if direction == "up" else "下穿"
    subject = f"【股价提醒】{quote.name} {verb} {cfg.target:.2f} 元，现价 {quote.price:.2f}"
    arrow = "▲" if quote.pct >= 0 else "▼"
    body = (
        f"{quote.name}（{quote.code}）已{verb}目标价 {cfg.target:.2f} 元。\n\n"
        f"  现价：{quote.price:.2f} 元\n"
        f"  涨跌：{arrow} {quote.pct:+.2f}%（昨收 {quote.prev_close:.2f}）\n"
        f"  今日：最高 {quote.high:.2f} / 最低 {quote.low:.2f} / 开盘 {quote.open:.2f}\n"
        f"  行情时间：{quote.ts.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"  数据来源：{quote.source}\n\n"
        f"-- 自动监控，请勿直接回复。\n"
    )
    return subject, body


def send_mail(cfg: Config, subject: str, body: str, *, dry_run: bool) -> None:
    if dry_run:
        print("[dry-run] 本应发送邮件：")
        print("  收件人:", cfg.mail_to)
        print("  标题:", subject)
        print("  正文:\n" + "\n".join("    " + ln for ln in body.splitlines()))
        return

    if not (cfg.smtp_host and cfg.smtp_user and cfg.smtp_pass and cfg.mail_to):
        raise RuntimeError("缺少 SMTP_HOST / SMTP_USER / SMTP_PASS / MAIL_TO")

    msg = EmailMessage()
    msg["From"] = cfg.mail_from
    msg["To"] = cfg.mail_to
    msg["Subject"] = subject
    msg.set_content(body)

    last: Exception | None = None
    for i in range(3):
        try:
            if cfg.smtp_port == 465:
                with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port,
                                      context=ssl.create_default_context(),
                                      timeout=20) as s:
                    s.login(cfg.smtp_user, cfg.smtp_pass)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20) as s:
                    s.starttls(context=ssl.create_default_context())
                    s.login(cfg.smtp_user, cfg.smtp_pass)
                    s.send_message(msg)
            print("[ok] 邮件已发送")
            return
        except Exception as e:                       # noqa: BLE001
            last = e
            print(f"[warn] 发信失败({i + 1}/3): {e}", file=sys.stderr)
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"邮件发送失败: {last}")


# ---------------------------------------------------------------- ntfy
def send_ntfy(cfg: Config, subject: str, body: str, *, dry_run: bool,
              tags: list[str] | None = None) -> None:
    if not cfg.ntfy_topic:
        raise RuntimeError("缺少 NTFY_TOPIC")

    payload = {
        "topic": cfg.ntfy_topic,
        "title": subject,
        "message": body,
        "priority": cfg.ntfy_priority,
        "tags": tags or ["bell"],
        "click": cfg.click_url,
    }

    if dry_run:
        print("[dry-run] 本应发送 ntfy 推送：")
        print("  服务器:", cfg.ntfy_server)
        print("  主题:", cfg.ntfy_topic)
        print("  标题:", subject)
        print("  正文:\n" + "\n".join("    " + ln for ln in body.splitlines()))
        return

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last: Exception | None = None
    for i in range(3):
        try:
            req = urllib.request.Request(
                cfg.ntfy_server, data=data, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": UA},
            )
            with urllib.request.urlopen(req, timeout=15,
                                        context=ssl.create_default_context()) as r:
                r.read()
            print("[ok] ntfy 推送已发布")
            return
        except Exception as e:                       # noqa: BLE001
            last = e
            print(f"[warn] 推送失败({i + 1}/3): {e}", file=sys.stderr)
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"ntfy 推送失败: {last}")


def notify(cfg: Config, subject: str, body: str, *, dry_run: bool,
           tags: list[str] | None = None) -> None:
    if cfg.notify_channel == "email":
        send_mail(cfg, subject, body, dry_run=dry_run)
    else:
        send_ntfy(cfg, subject, body, dry_run=dry_run, tags=tags)


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印，不发邮件")
    ap.add_argument("--force", action="store_true", help="跳过交易时段/数据新鲜度检查")
    ap.add_argument("--test-notify", action="store_true", help="只发一条测试推送，不查行情")
    ap.add_argument("--test-mail", action="store_true", help="只发一封测试邮件，不查行情")
    args = ap.parse_args()

    cfg = Config()

    if args.test_notify:
        now = datetime.now(BEIJING)
        subject = "【测试】昊华科技股价监控推送通道"
        body = (
            f"这是一条测试推送，发送时间 {now:%Y-%m-%d %H:%M:%S}（北京时间）。\n"
            f"收到即表示 ntfy 通道正常，之后到价提醒会发到这个主题。\n"
        )
        try:
            notify(cfg, subject, body, dry_run=args.dry_run, tags=["white_check_mark"])
        except Exception as e:                       # noqa: BLE001
            print(f"[error] {e}", file=sys.stderr)
            return 1
        return 0

    if args.test_mail:
        now = datetime.now(BEIJING)
        subject = "【测试】昊华科技股价监控邮件通道"
        body = (
            f"这是一封测试邮件，发送时间 {now:%Y-%m-%d %H:%M:%S}（北京时间）。\n"
            f"收到即表示 SMTP 配置正确，之后到价提醒会发到这个地址。\n"
        )
        try:
            send_mail(cfg, subject, body, dry_run=args.dry_run)
        except Exception as e:                       # noqa: BLE001
            print(f"[error] {e}", file=sys.stderr)
            return 1
        return 0

    if cfg.target <= 0:
        print("[error] 必须设置 TARGET_PRICE", file=sys.stderr)
        return 2

    now = datetime.now(BEIJING)
    print(f"[info] 运行 {now:%Y-%m-%d %H:%M:%S} 目标 {cfg.target:.2f} 方向 {cfg.direction}")

    if not args.force and not in_session(now):
        print("[skip] 非交易时段")
        return 0

    try:
        quote = fetch_quote(cfg.secid)
    except Exception as e:                           # noqa: BLE001
        print(f"[error] 获取行情失败: {e}", file=sys.stderr)
        return 1

    print("[info] 行情", json.dumps(quote.summary(), ensure_ascii=False))

    age = abs((now - quote.ts).total_seconds())
    if not args.force and age > cfg.stale_seconds:
        print(f"[skip] 行情过期 {age:.0f}s（可能休市/停牌）")
        return 0

    state = load_state(cfg.state_file)
    last_notify = state.get("last_notify_ts")
    fired = evaluate(state, quote, cfg)

    if fired:
        fresh = True
        if last_notify:
            gap = (now - datetime.fromisoformat(last_notify)).total_seconds()
            fresh = gap >= cfg.min_interval_min * 60
            if not fresh:
                print(f"[skip] 冷却期内，距上次提醒 {gap / 60:.1f} 分钟")
        if fresh:
            subject, body = build_mail(quote, fired[0], cfg)
            tag = "chart_with_upwards_trend" if fired[0] == "up" else "chart_with_downwards_trend"
            notify(cfg, subject, body, dry_run=args.dry_run, tags=[tag])
            state["last_notify_ts"] = now.isoformat()

    save_state(cfg.state_file, state)
    print(f"[info] 已保存状态: armed_up={state.get('armed_up')} "
          f"armed_down={state.get('armed_down')} last={state.get('last_price')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
