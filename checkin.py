#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2026 GLaDOS 自动签到 (积分增强版)

功能：
- 全自动签到
- 精准获取当前积分 (Points)
- PushPlus 微信推送（包含积分、剩余天数、签到结果）
- 智能多域名切换 (优先 glados.one)
- 支持 Cookie-Editor 导出格式
"""

import requests
import json
import os
import sys
import time
import smtplib
import ssl
from email.message import EmailMessage
from html import escape
from pathlib import Path
from datetime import datetime, timedelta, timezone

MAIL_TO = "418796833@qq.com"
BEIJING = timezone(timedelta(hours=8))


def checkin_succeeded(response):
    if not isinstance(response, dict):
        return False
    message = str(response.get("message", "")).strip()
    # Already-recorded responses can carry a nonzero code.
    if "code" in response:
        return response["code"] in (0, "0") or message in (
            "Checkin Repeats! Please Try Tomorrow",
            "Today's observation logged. Return tomorrow for more points.",
        )
    return message.startswith("Checkin! Got ") or message in (
        "Checkin! Get 1 Day", "Checkin Repeats! Please Try Tomorrow",
        "Today's observation logged. Return tomorrow for more points.",
    )


def dashboard_mail(reports):
    """Send only the explicitly selected Dashboard fields, never the raw API response."""
    now = datetime.now(BEIJING)
    message = EmailMessage()
    message["Subject"] = f"GLaDOS 签到成功 · Dashboard · {now:%Y-%m-%d %H:%M}"
    message["From"] = MAIL_TO
    message["To"] = MAIL_TO
    sections = []
    text_sections = []
    for report in reports:
        fields = [
            ("账号", report["email"]), ("套餐", report["plan"]),
            ("已用流量", report["used_traffic"]), ("月流量额度", report["traffic_limit"]),
            ("剩余天数", report["left_days"]), ("当前积分", report["points"]),
            ("签到结果", report["result"]),
        ]
        text_sections.append("\n".join(f"{label}：{value}" for label, value in fields))
        rows = "".join(
            f'<tr><th style="text-align:left;padding:9px;border-bottom:1px solid #ddd">{label}</th>'
            f'<td style="padding:9px;border-bottom:1px solid #ddd">{escape(str(value))}</td></tr>'
            for label, value in fields
        )
        sections.append(f'<table style="border-collapse:collapse;width:100%;margin-bottom:20px">{rows}</table>')
    stamp = f"数据时间：{now:%Y-%m-%d %H:%M:%S}（北京时间）"
    message.set_content("GLaDOS 签到成功 · Dashboard\n\n" + "\n\n".join(text_sections) + "\n\n" + stamp + "\nhttps://glados.one/console")
    message.add_alternative(
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><body style="font-family:Arial,sans-serif;color:#222">'
        '<div style="max-width:640px;margin:auto;padding:24px"><h2>GLaDOS 签到成功</h2><p>Dashboard 数据摘要</p>'
        + "".join(sections) + f'<p>{stamp}</p><p><a href="https://glados.one/console">打开 Dashboard</a></p></div></body></html>',
        subtype="html",
    )
    return message


def send_dashboard_email():
    report_path = os.environ.get("DASHBOARD_REPORT_PATH", "")
    reports = json.loads(Path(report_path).read_text(encoding="utf-8")) if report_path and Path(report_path).is_file() else []
    if not reports:
        log("⏭️ 没有成功签到且数据完整的账号，跳过 Dashboard 邮件")
        return
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    if not password:
        raise RuntimeError("未配置 SMTP_PASSWORD（QQ 邮箱 SMTP 授权码）")
    message = dashboard_mail(reports)
    with smtplib.SMTP_SSL("smtp.qq.com", 465, context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(MAIL_TO, password)
        if smtp.send_message(message):
            raise RuntimeError("SMTP 拒绝了收件人")
    log("✅ QQ SMTP 已接受 Dashboard 邮件；实际送达以收件箱为准")

# Fix Windows Unicode Output
if sys.platform.startswith('win'):
    sys.stdout.reconfigure(encoding='utf-8')

# ================= 配置 =================

# 域名优先级：Cloud 第一
DOMAINS = [
    "https://glados.one",
    "https://glados.rocks", 
    "https://glados.network",
]

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Content-Type': 'application/json;charset=UTF-8',
    'Accept': 'application/json, text/plain, */*',
}

# ================= 工具函数 =================

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")

def extract_cookie(raw: str):
    """提取 Cookie，支持 Cookie-Editor 冒号格式"""
    if not raw: return None
    raw = raw.strip()
    
    # Cookie-Editor 格式 (koa:sess=xxx; koa:sess.sig=yyy)
    if 'koa:sess=' in raw or 'koa:sess.sig=' in raw:
        return raw
        
    # JSON
    if raw.startswith('{'):
        try:
            return 'koa.sess=' + json.loads(raw).get('token')
        except: pass
        
    # JWT Token
    if raw.count('.') == 2 and '=' not in raw and len(raw) > 50:
        return 'koa:sess=' + raw
        
    # Standard
    return raw

def get_cookies():
    raw = os.environ.get("GLADOS_COOKIE", "")
    if not raw:
        log("❌ 未配置 GLADOS_COOKIE")
        return []
    
    # Split by enter or &
    sep = '\n' if '\n' in raw else '&'
    return [extract_cookie(c) for c in raw.split(sep) if c.strip()]

# ================= 核心逻辑 =================

class GLaDOS:
    def __init__(self, cookie):
        self.cookie = cookie
        self.domain = DOMAINS[0]
        self.email = "?"
        self.left_days = "?"
        self.points = "?"
        self.points_change = "?"
        self.exchange_info = ""
        self.plan = "?"
        self.used_traffic = "未能读取"
        self.traffic_limit = "未能读取"
        
    def req(self, method, path, data=None):
        """带自动域名切换的请求"""
        for d in DOMAINS:
            try:
                url = f"{d}{path}"
                h = HEADERS.copy()
                h['Cookie'] = self.cookie
                h['Origin'] = d
                h['Referer'] = f"{d}/console/checkin"
                
                if method == 'GET':
                    resp = requests.get(url, headers=h, timeout=10)
                else:
                    resp = requests.post(url, headers=h, json=data, timeout=10)
                
                if resp.status_code == 200:
                    self.domain = d # Remember working domain
                    return resp.json()
            except Exception as e:
                log(f"⚠️ {d} 请求失败: {e}")
                continue
        return None

    def get_status(self):
        """获取状态：天数、邮箱"""
        res = self.req('GET', '/api/user/status')
        if res and 'data' in res:
            d = res['data']
            self.email = d.get('email', 'Unknown')
            self.left_days = str(d.get('leftDays', '?')).split('.')[0]
            # Verified against this account's live Dashboard: vip=21 is Basic / 200 GB.
            # Unknown tiers stay explicit instead of displaying a guessed quota.
            if str(d.get('vip')) == '21':
                self.plan, self.traffic_limit = 'Basic', '200 GB'
            else:
                self.plan = f"未识别套餐（等级 {d.get('vip', '?')}）"
            try:
                self.used_traffic = f"{float(d['traffic']) / 1024 ** 3:.2f} GB"
            except (KeyError, TypeError, ValueError):
                pass
            return True
        return False

    def get_points(self):
        """获取积分、变化历史、兑换计划"""
        res = self.req('GET', '/api/user/points')
        if res and 'points' in res:
            # 当前积分
            self.points = str(res.get('points', '0')).split('.')[0]
            
            # 最近一次积分变化
            history = res.get('history', [])
            if history:
                last = history[0]
                change = str(last.get('change', '0')).split('.')[0]
                if not change.startswith('-'):
                    change = '+' + change
                self.points_change = change
            
            # 兑换计划
            plans = res.get('plans', {})
            pts = int(self.points)
            exchange_lines = []
            for plan_id, plan_data in plans.items():
                need = plan_data['points']
                days = plan_data['days']
                if pts >= need:
                    exchange_lines.append(f"✅ {need}分→{days}天 (可兑换)")
                else:
                    exchange_lines.append(f"❌ {need}分→{days}天 (差{need-pts}分)")
            self.exchange_info = "<br>".join(exchange_lines)
            return True
        return False

    def checkin(self):
        """执行签到"""
        return self.req('POST', '/api/user/checkin', {'token': 'glados.one'})

# ================= 主程序 =================

def pushplus(token, title, content):
    if not token: return
    try:
        url = "http://www.pushplus.plus/send"
        requests.get(url, params={'token': token, 'title': title, 'content': content, 'template': 'html'}, timeout=5)
        log("✅ PushPlus 推送成功")
    except:
        log("❌ PushPlus 推送失败")

def telegram_push(token, chat_id, title, content):
    if not token or not chat_id: return
    try:
        import re
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        # Convert HTML to be Telegram-compatible
        text = f"<b>{title}</b>\n\n{content}"
        
        # 1. Block elements replacements (handle tags with attributes)
        text = text.replace("<br>", "\n")
        # Handle H3 tags
        text = re.sub(r"<h3[^>]*>", "<b>", text)
        text = text.replace("</h3>", "</b>\n")
        
        # 2. Paragraph and Div tags
        text = re.sub(r"<(div|p)[^>]*>", "", text)
        text = re.sub(r"</(div|p)>", "\n", text)
        
        # 3. Span and small tags
        text = re.sub(r"<(span|small)[^>]*>", "", text)
        text = re.sub(r"</(span|small)>", "", text)
        
        # 4. Final cleaning: Strip all HTML tags EXCEPT the ones supported by Telegram: b, i, u, s, a, code, pre
        text = re.sub(r"<(?!\/?(b|i|u|s|a|code|pre)\b)[^>]+>", "", text)
        
        # 5. Dedent each line to fix alignment issues caused by HTML template indentation
        lines = [line.strip() for line in text.split('\n')]
        text = "\n".join(lines)
        
        # 6. Collapse multiple newlines
        text = re.sub(r"\n\s*\n", "\n\n", text).strip()
        
        data = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        log(f"发送内容: {data}")
        resp=requests.post(url, json=data, timeout=5)
        if resp.status_code != 200:
            log(f"❌ Telegram 推送失败: {resp.json()}")
            return
        log("✅ Telegram 推送成功")
    except Exception as e:
        log(f"❌ Telegram 推送失败: {e}")

def main():
    log("🚀 2026 GLaDOS Checkin Starting...")
    cookies = get_cookies()
    if not cookies: sys.exit(1)
    
    results = []
    email_reports = []
    report_path = os.environ.get("DASHBOARD_REPORT_PATH", "")
    if report_path:
        Path(report_path).write_text('[]', encoding='utf-8')
    success_cnt = 0
    
    for i, cookie in enumerate(cookies, 1):
        g = GLaDOS(cookie)
        
        # 1. Checkin
        res = g.checkin()
        msg = res.get('message', 'Failure') if res else "Network Error"
        
        # 2. Get Info (Refresh data)
        status_ok = g.get_status()
        points_ok = g.get_points()
        
        # 3. Log
        succeeded = checkin_succeeded(res)
        status_icon = "✅" if succeeded else "⚠️"
        log(f"站点: {g.domain} | 用户: {g.email} | 积分: {g.points} | 天数: {g.left_days} | 结果: {msg}")
        
        if succeeded:
            success_cnt += 1
            if status_ok and points_ok:
                email_reports.append({
                    "email": g.email, "plan": g.plan, "used_traffic": g.used_traffic,
                    "traffic_limit": g.traffic_limit, "left_days": g.left_days,
                    "points": g.points, "result": msg,
                })
            else:
                log("⚠️ 签到完成，但 Dashboard 数据读取不完整，不生成成功邮件")
        
        # 4. Result Formatting
        results.append(f"""
<div style="border:2px solid #333; padding:15px; margin-bottom:15px; border-radius:10px; background:#fff;">
    <h3 style="margin:0 0 15px 0; color:#333; border-bottom:2px solid #333; padding-bottom:8px;">👤 {g.email}</h3>
    <p style="margin:8px 0; color:#000; font-size:16px;"><b>当前积分:</b> <span style="color:#e74c3c; font-size:22px; font-weight:bold;">{g.points}</span> <span style="color:#27ae60; font-weight:bold;">({g.points_change})</span></p>
    <p style="margin:8px 0; color:#000; font-size:16px;"><b>剩余天数:</b> <span style="font-weight:bold;">{g.left_days} 天</span></p>
    <p style="margin:8px 0; color:#000; font-size:16px;"><b>签到结果:</b> {msg}</p>
    <div style="margin-top:15px; padding:12px; background:#f0f0f0; border-radius:8px; border:1px solid #ccc;">
        <p style="margin:0 0 8px 0; color:#333; font-weight:bold; font-size:15px;">🎁 兑换选项:</p>
        <p style="margin:0; color:#000; font-size:14px; line-height:1.8;">
{g.exchange_info}</p>
    </div>
</div>
""")

    if report_path:
        Path(report_path).write_text(json.dumps(email_reports, ensure_ascii=False), encoding='utf-8')

    # Push
    push_level = os.environ.get("PUSH_LEVEL", "all").lower()
    
    if push_level == "fail_only" and success_cnt == len(cookies):
        log("⏭️ 根据 PUSH_LEVEL=fail_only 设置，所有账号签到成功，跳过推送")
        return

    ptoken = os.environ.get("PUSHPLUS_TOKEN")
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    
    if ptoken or (tg_token and tg_chat_id):
        title = f"GLaDOS签到: 成功{success_cnt}/{len(cookies)}"
        content = "".join(results)
        content += f"<br><small>时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</small>"
        
        if ptoken:
            pushplus(ptoken, title, content)
        if tg_token and tg_chat_id:
            telegram_push(tg_token, tg_chat_id, title, content)

    if success_cnt != len(cookies):
        sys.exit(1)

if __name__ == '__main__':
    if sys.argv[1:] == ['--email-only']:
        try:
            send_dashboard_email()
        except Exception as exc:
            # Do not print SMTP exceptions: server text may contain account or auth data.
            log(f"❌ Dashboard 邮件发送失败：{type(exc).__name__}")
            sys.exit(1)
    else:
        main()
