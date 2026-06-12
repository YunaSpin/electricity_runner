#!/usr/bin/env python3
"""
电费监控 - GitHub Actions 定时执行脚本
========================================
纯 Python 标准库，零 pip 依赖。
由 GitHub Actions 每 10 分钟触发一次：
  → 查询电表 → 判断阈值 → 发短信(如需) → 写日志 → 退出

SMTP 凭据通过环境变量注入（GitHub Secrets），不硬编码在代码中。
"""

import re
import json
import os
import sys
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
import urllib.request

# ===== 文件路径 =====
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "electricity_config.json")
LOG_FILE = os.path.join(BASE_DIR, "log.txt")
ALERT_STATE_FILE = os.path.join(BASE_DIR, "alert_state.json")

# ===== 默认配置 =====
DEFAULT_CONFIG = {
    "meter_id": "19500815873",
    "check_interval_minutes": 10,
    "balance_threshold": 10.0,
    "kwh_threshold": 15.0,
}

# ===== 告警去重间隔 =====
ALERT_COOLDOWN_HOURS = 6  # 同一告警 6 小时内不重复发送

URL_TEMPLATE = "http://www.wap.cnyiot.com/nat/pay.aspx?mid={}"


# ==================== 配置 ====================

def load_config():
    """读取配置文件"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                if k not in cfg:
                    cfg[k] = v
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


# ==================== 告警状态 ====================

def load_alert_state():
    """读取告警状态文件"""
    if os.path.exists(ALERT_STATE_FILE):
        try:
            with open(ALERT_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_alert_time": None, "last_alert_type": None}


def save_alert_state(state):
    """保存告警状态文件"""
    with open(ALERT_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def should_send_alert(alert_type):
    """
    判断是否应该发送告警。
    如果距离上次同类型告警超过 ALERT_COOLDOWN_HOURS 小时，则允许发送。
    """
    state = load_alert_state()
    last_time = state.get("last_alert_time")
    last_type = state.get("last_alert_type")

    if last_time is None:
        return True, state

    try:
        last_dt = datetime.strptime(last_time, "%Y-%m-%d %H:%M:%S")
        if datetime.now() - last_dt > timedelta(hours=ALERT_COOLDOWN_HOURS):
            return True, state
        # 如果告警类型变了（比如从余额低变成电量低），也允许发送
        if last_type != alert_type:
            return True, state
    except (ValueError, TypeError):
        return True, state

    return False, state


# ==================== 电表查询 ====================

def query_meter(meter_id):
    """查询电表数据，返回 (data_dict, error_string)"""
    try:
        url = URL_TEMPLATE.format(meter_id)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8")
    except Exception as e:
        return None, str(e)

    kwh_match = re.search(r'剩余电量:[\s\S]*?<label[^>]*>([0-9.]+)</label>', html)
    money_match = re.search(r'剩余金额:[\s\S]*?<label[^>]*>([0-9.]+)</label>', html)
    name_match = re.search(r'表.{0,20}称:[\s\S]*?<label[^>]*>([^<]+)</label>', html)

    if not money_match:
        return None, "解析失败"

    return {
        "name": name_match.group(1).strip() if name_match else "",
        "kwh": float(kwh_match.group(1)) if kwh_match else 0,
        "money": float(money_match.group(1)),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }, None


# ==================== 日志 ====================

def log_write(data, alert=False):
    """写入日志（格式与 GUI 版一致）"""
    line = f"[{data['time']}] {data['name']} | 余额:{data['money']:.2f}元 | 电量:{data['kwh']:.2f}kWh"
    if alert:
        line += " | *** 低于阈值，已发送短信 ***"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"[LOG ERROR] {e}", flush=True)


# ==================== 短信发送 ====================

def send_sms():
    """通过QQ邮箱SMTP发送短信到手机（从环境变量读取凭据）"""
    smtp_host = os.environ.get("SMTP_HOST", "smtp.qq.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    sms_receiver = os.environ.get("SMS_RECEIVER", "13996926630@139.com")

    if not smtp_user or not smtp_pass:
        print("[WARN] SMTP 凭据未设置，跳过短信发送", flush=True)
        return False

    try:
        msg = MIMEText("没电费了，贴汁，快充！", "plain", "utf-8")
        msg["From"] = smtp_user
        msg["To"] = sms_receiver
        msg["Subject"] = "电费告警"

        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
            server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [sms_receiver], msg.as_string())
        server.quit()
        return True
    except Exception as e:
        print(f"[SMS ERROR] 发送失败: {e}", flush=True)
        return False


# ==================== 主流程 ====================

def main():
    print("=" * 50, flush=True)
    print(f"  电费监控 Runner", flush=True)
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print("=" * 50, flush=True)

    cfg = load_config()
    meter_id = cfg["meter_id"]
    balance_threshold = cfg["balance_threshold"]
    kwh_threshold = cfg["kwh_threshold"]

    # 1. 查询电表
    print(f"[查询] 表计ID: {meter_id}", flush=True)
    data, error = query_meter(meter_id)

    if error:
        print(f"[失败] {error}", flush=True)
        sys.exit(1)

    print(f"[结果] {data['name']} | 余额:{data['money']:.2f}元 | 电量:{data['kwh']:.2f}kWh", flush=True)

    # 2. 判断阈值
    low_balance = data["money"] <= balance_threshold
    low_kwh = data["kwh"] <= kwh_threshold

    if low_balance or low_kwh:
        alert_type = "balance" if low_balance else "kwh"
        if low_balance and low_kwh:
            alert_type = "both"

        # 3. 告警去重检查
        should_send, state = should_send_alert(alert_type)

        if should_send:
            print(f"[告警] 触发！余额={data['money']:.2f}元 电量={data['kwh']:.2f}kWh", flush=True)
            sms_ok = send_sms()
            if sms_ok:
                state["last_alert_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                state["last_alert_type"] = alert_type
                save_alert_state(state)
                print("[短信] 发送成功", flush=True)
                log_write(data, alert=True)
            else:
                print("[短信] 发送失败，不标记告警状态", flush=True)
                log_write(data, alert=False)
        else:
            last_time = state.get("last_alert_time", "未知")
            print(f"[跳过] 已在 {last_time} 发送过告警，冷却中 ({ALERT_COOLDOWN_HOURS}h)", flush=True)
            log_write(data, alert=False)
    else:
        # 4. 恢复正常，清除告警状态
        state = load_alert_state()
        if state.get("last_alert_time"):
            print("[恢复] 余额/电量已恢复正常，清除告警状态", flush=True)
            save_alert_state({"last_alert_time": None, "last_alert_type": None})
        log_write(data, alert=False)

    print("=" * 50, flush=True)
    print("  执行完成", flush=True)
    print("=" * 50, flush=True)


if __name__ == "__main__":
    main()
