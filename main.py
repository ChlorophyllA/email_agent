import imaplib
import os
import smtplib
import email
from email.mime.text import MIMEText
from email.utils import formataddr
import json
import time
import hashlib
import requests
from pathlib import Path

CONFIG_FILE = "config.json"
REPLIED_RECORD_FILE = "replied_ids.txt"

def load_config():
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def load_replied_ids():
    if not Path(REPLIED_RECORD_FILE).exists():
        return set()
    with open(REPLIED_RECORD_FILE, 'r') as f:
        return set(line.strip() for line in f)

def save_replied_id(msg_id):
    with open(REPLIED_RECORD_FILE, 'a') as f:
        f.write(msg_id + '\n')

def send_wechat_message(send_key, title, content):
    url = f"https://sctapi.ftqq.com/{send_key}.send"
    data = {
        "title": title,
        "desp": content
    }
    try:
        resp = requests.post(url, data=data, timeout=5)
        return resp.json()
    except Exception as e:
        print(f"微信推送失败: {e}")
        return None

def fetch_unread_emails(account):
    """登录IMAP，获取未读邮件列表，返回 (msg_id, 原始邮件对象) 列表"""
    try:
        imap = imaplib.IMAP4_SSL(account["imap_server"], account["imap_port"])
        imap.login(account["email"], account["password"])
        imap.select("INBOX")
        # 搜索未读邮件
        status, data = imap.search(None, "UNSEEN")
        if status != 'OK':
            return []
        msg_ids = data[0].split()
        emails = []
        for num in msg_ids:
            status, msg_data = imap.fetch(num, "(RFC822)")
            if status == 'OK':
                raw_email = msg_data[0][1]
                email_msg = email.message_from_bytes(raw_email)
                # 提取Message-ID，用于去重回复
                msg_id_header = email_msg.get("Message-ID", hashlib.md5(str(num).encode()).hexdigest())
                emails.append((msg_id_header, email_msg, num))
        imap.close()
        imap.logout()
        return emails
    except Exception as e:
        print(f"登录 {account['name']} 失败: {e}")
        return []

def should_reply(email_msg, config, replied_ids):
    """判断是否应该自动回复"""
    auto_cfg = config["auto_reply"]
    if not auto_cfg.get("enabled", False):
        return False

    # 避免重复回复
    msg_id = email_msg.get("Message-ID", "")
    if msg_id in replied_ids:
        return False

    # 避免回复自动回复的邮件（检查 Auto-Submitted 头）
    if auto_cfg.get("avoid_loop", True):
        auto_sub = email_msg.get("Auto-Submitted", "")
        if auto_sub and auto_sub.lower() != "no":
            return False

    # 关键词白名单（如果设置了且非空）
    keywords = auto_cfg.get("subject_keywords", [])
    if keywords:
        subject = email_msg.get("Subject", "")
        if not any(kw in subject for kw in keywords):
            return False

    # 发件人白名单
    sender_whitelist = auto_cfg.get("sender_whitelist", [])
    if sender_whitelist:
        from_addr = email.utils.parseaddr(email_msg.get("From", ""))[1]
        if from_addr not in sender_whitelist:
            return False

    return True

def send_auto_reply(account, to_addr, config):
    """发送自动回复邮件"""
    auto_cfg = config["auto_reply"]
    subject = auto_cfg["reply_subject"]
    body = auto_cfg["reply_body"]

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = formataddr(("自动回复助手", account["email"]))
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Auto-Submitted"] = "auto-replied"   # 告诉对方这是自动回复，避免循环

    try:
        if account["smtp_port"] == 465:
            smtp = smtplib.SMTP_SSL(account["smtp_server"], account["smtp_port"])
        else:
            smtp = smtplib.SMTP(account["smtp_server"], account["smtp_port"])
            smtp.starttls()
        smtp.login(account["email"], account["password"])
        smtp.sendmail(account["email"], [to_addr], msg.as_string())
        smtp.quit()
        print(f"已自动回复 {to_addr}")
        return True
    except Exception as e:
        print(f"发送自动回复失败: {e}")
        return False

def mark_as_read(account, msg_num):
    """将邮件标记为已读（可选）"""
    try:
        imap = imaplib.IMAP4_SSL(account["imap_server"], account["imap_port"])
        imap.login(account["email"], account["password"])
        imap.select("INBOX")
        imap.store(msg_num, "+FLAGS", "\\Seen")
        imap.close()
        imap.logout()
    except Exception as e:
        print(f"标记已读失败: {e}")

def main():
    qq_email = os.environ.get('QQ_EMAIL')
    qq_auth_code = os.environ.get('QQ_AUTH_CODE')
    campus_email = os.environ.get('CAMPUS_EMAIL')
    campus_password = os.environ.get('CAMPUS_PASSWORD')
    send_key = os.environ.get('SEND_KEY')
    auto_reply_enabled = os.environ.get('AUTO_REPLY_ENABLED', 'false').lower() == 'true'

    # 2. 将读取到的值赋值给 config 字典
    config = {
        "accounts": [
            {"name": "QQ邮箱", "email": qq_email, "password": qq_auth_code, },
            {"name": "校园邮箱", "email": campus_email, "password": campus_password, }
        ],
        "wechat": {"send_key": send_key},
        "auto_reply": {"enabled": auto_reply_enabled, 
                       "subject_keywords": [],            
                        "sender_whitelist": [],
                        "reply_subject": "AutoReply：已收到您的邮件", 
                        "reply_body": "您好，\n\n我已收到您的邮件，会尽快回复。\n\nI've received your email and will get back to you soon.\n\n（此邮件为自动回复）", 
                        "avoid_loop": True}
    }
    replied_ids = load_replied_ids()
    send_key = config["wechat"]["send_key"]

    for account in config["accounts"]:
        print(f"检查 {account['name']} ...")
        unreads = fetch_unread_emails(account)
        if not unreads:
            continue
        for msg_id, email_msg, msg_num in unreads:
            # 提取信息用于微信推送
            subject = email_msg.get("Subject", "(无主题)")
            from_addr = email.utils.parseaddr(email_msg.get("From", ""))[1]
            date = email_msg.get("Date", "")

            # 微信推送
            title = f"📧 {account['name']} 新邮件"
            content = f"**发件人**: {from_addr}\n**主题**: {subject}\n**时间**: {date}"
            send_wechat_message(send_key, title, content)

            # 自动回复
            if should_reply(email_msg, config, replied_ids):
                success = send_auto_reply(account, from_addr, config)
                if success:
                    save_replied_id(msg_id)

            # 可选：将邮件标记为已读（如果你不想下次重复提醒）
            mark_as_read(account, msg_num)

    print("本轮检查完成")

def main_handler(event, context):
    """云函数的主入口函数"""
    main()  # 调用你原有的 main 函数
    return "Mail check completed."

if __name__ == "__main__":
    main()