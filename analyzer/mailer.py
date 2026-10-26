"""美股简报邮件推送（SMTP）：把 data/news/us_market/YYYY-MM-DD.md 推送到邮箱。

渠道事实（借自 quant.example.com 上 github-trending-push 的 email 通道）：
QQ 邮箱 SMTP（smtp.qq.com:587 STARTTLS，授权码登录），收件人列表可配。
本模块只复用「发信渠道」的通行做法，发信账号/授权码由本系统环境变量
自行配置（MAIL_* / SMTP_*），不读取 trending 项目的 .env。

全部标准库实现（smtplib + email.message），不新增 pip 依赖（服务器 Python 3.10 兼容）。
"""
from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr

log = logging.getLogger(__name__)

DEFAULT_SMTP_HOST = "smtp.qq.com"
DEFAULT_SMTP_PORT = 587
DEFAULT_SENDER_NAME = "quant-trading 美股简报"


class MailError(RuntimeError):
    """邮件发送失败（配置缺失 / SMTP 异常），message 可直接展示。"""


@dataclass(frozen=True)
class MailConfig:
    """邮件推送配置（环境变量 MAIL_* / SMTP_*，全部缺省时功能停用）。"""

    smtp_host: str = DEFAULT_SMTP_HOST
    smtp_port: int = DEFAULT_SMTP_PORT
    smtp_user: str = ""            # 发件邮箱账号
    smtp_pass: str = ""            # SMTP 授权码（不是邮箱登录密码）
    mail_from: str = ""            # 发件地址，缺省取 smtp_user
    mail_to: list[str] = field(default_factory=list)   # 收件人（逗号分隔可多个）
    sender_name: str = DEFAULT_SENDER_NAME
    timeout: float = 15.0
    dry_run: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.smtp_user and self.smtp_pass and self.mail_to)

    @property
    def from_addr(self) -> str:
        return self.mail_from or self.smtp_user

    @classmethod
    def from_env(cls) -> "MailConfig":
        def _split(raw: str) -> list[str]:
            return [x.strip() for x in raw.split(",") if x.strip()]

        return cls(
            smtp_host=os.environ.get("SMTP_HOST", DEFAULT_SMTP_HOST).strip() or DEFAULT_SMTP_HOST,
            smtp_port=int(os.environ.get("SMTP_PORT", DEFAULT_SMTP_PORT)),
            smtp_user=os.environ.get("SMTP_USER", "").strip(),
            smtp_pass=os.environ.get("SMTP_PASS", "").strip(),
            mail_from=os.environ.get("MAIL_FROM", "").strip(),
            mail_to=_split(os.environ.get("MAIL_TO", "")),
            sender_name=os.environ.get("MAIL_SENDER_NAME", DEFAULT_SENDER_NAME),
            timeout=float(os.environ.get("MAIL_TIMEOUT", "15")),
            dry_run=os.environ.get("MAIL_DRY_RUN", "").strip().lower()
            in ("1", "true", "yes", "on"),
        )


def send_mail(cfg: MailConfig, subject: str, html_body: str,
              text_body: str | None = None) -> dict:
    """发送一封 HTML 邮件；dry_run 只记日志不连 SMTP。

    返回 {"ok": True, "to": [...], "dry_run": bool}；失败抛 MailError。
    """
    if not cfg.enabled:
        raise MailError("邮件推送未配置（需 SMTP_USER/SMTP_PASS/MAIL_TO）")
    to = list(cfg.mail_to)

    if cfg.dry_run:
        log.info("MAIL DRY-RUN subject=%s to=%s body=%d bytes", subject, to, len(html_body))
        print(f"[mailer] DRY-RUN 跳过 SMTP：subject={subject} to={to} "
              f"body={len(html_body)} bytes")
        return {"ok": True, "to": to, "dry_run": True}

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg.sender_name, cfg.from_addr))
    msg["To"] = ", ".join(to)
    msg.set_content(text_body or "本邮件需支持 HTML 的客户端阅读。")
    msg.add_alternative(html_body, subtype="html")

    try:
        if cfg.smtp_port == 465:
            s = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=cfg.timeout)
        else:
            s = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=cfg.timeout)
            s.starttls()
        with s:
            s.login(cfg.smtp_user, cfg.smtp_pass)
            s.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"邮件发送失败: {e}") from e

    log.info("email sent to=%s subject=%s", to, subject)
    return {"ok": True, "to": to, "dry_run": False}
