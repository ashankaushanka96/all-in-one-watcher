from functools import lru_cache
import html
import json
import os
import smtplib
from typing import Optional

import boto3
from botocore.exceptions import NoCredentialsError, PartialCredentialsError
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from loguru import logger

from app.config.settings import load_settings
from app.models.component_model import Component
import app.variables as var


root_logger = logger.bind(comp_name="SendMail")
settings = load_settings()


class SendMail:
    def __init__(self):
        self.mail_enabled = settings.mail_configs.mail_send
        self.username, self.password, self.smtp_server = self.load_mail_credentials()
        self.fromaddr = settings.mail_configs.from_address
        self.toaddr = settings.mail_configs.to_addresses

    def get_secrets_from_aws(self, secret_name, region_name):
        if not secret_name or not region_name:
            root_logger.warning("AWS secret_name/region_name not configured.")
            return None, None, None

        try:
            client = boto3.client(service_name="secretsmanager", region_name=region_name)
            get_secret_value_response = client.get_secret_value(SecretId=secret_name)
            secret = get_secret_value_response.get("SecretString") or ""
            secrets_dict = json.loads(secret)

            username = secrets_dict.get("USERNAME")
            password = secrets_dict.get("PASSWORD")
            smtp_server = secrets_dict.get("SMTP_SERVER")

            if not all([username, password, smtp_server]):
                root_logger.error("Required keys missing in the secret.")
                return None, None, None

            root_logger.debug("Successfully retrieved secrets from AWS.")
            return username, password, smtp_server

        except NoCredentialsError:
            root_logger.exception("AWS credentials not found.")
        except PartialCredentialsError:
            root_logger.exception("Incomplete AWS credentials.")
        except Exception as e:
            root_logger.exception(f"Failed to retrieve secrets from AWS: {e}")

        return None, None, None

    def get_secrets_from_env(self, env_path):
        try:
            from dotenv import load_dotenv
        except Exception:
            root_logger.error("python-dotenv not installed; cannot load env credentials.")
            return None, None, None

        load_dotenv(dotenv_path=os.path.expanduser(env_path))

        try:
            username = os.getenv("SMTP_USERNAME")
            password = os.getenv("SMTP_PASSWORD")
            smtp_server = os.getenv("SMTP_SERVER")

            if not all([username, password, smtp_server]):
                root_logger.error("Required SMTP_* keys missing in environment.")
                return None, None, None

            return username, password, smtp_server

        except Exception as e:
            root_logger.exception(f"Failed to retrieve env variables: {e}")

        return None, None, None

    def load_mail_credentials(self):
        secret_name = settings.mail_configs.mail_credentials.secret.name
        region_name = settings.meta_data.region

        root_logger.debug(f"Loading mail credentials from AWS secret {secret_name!r} in {region_name}...")
        creds = self.get_secrets_from_aws(secret_name, region_name)
        if all(creds):
            root_logger.info(f"Loaded mail credentials from AWS Secrets Manager (smtp={creds[2]}).")
            return creds

        env_path = settings.mail_configs.mail_credentials.env.path

        root_logger.debug(f"Falling back to ENV file {env_path} for mail credentials...")
        creds = self.get_secrets_from_env(env_path)
        if all(creds):
            root_logger.info(f"Loaded mail credentials from ENV file {env_path} (smtp={creds[2]}).")
            return creds

        root_logger.error(
            f"Failed to load mail credentials from AWS secret {secret_name!r} or ENV file {env_path}; "
            "mail alerts are disabled."
        )
        self.mail_enabled = False
        return None, None, None

    def get_day_names(self, running_dates):
        days_map = {
            0: "Sunday",
            1: "Monday",
            2: "Tuesday",
            3: "Wednesday",
            4: "Thursday",
            5: "Friday",
            6: "Saturday",
        }
        return ", ".join(days_map.get(day, str(day)) for day in running_dates)

    def _safe(self, value) -> str:
        if value is None:
            return "N/A"
        return html.escape(str(value))

    def _get_run_script_path(self, component: Component) -> str:
        if getattr(component, "runScriptPath", None) and getattr(component, "runScript", None):
            return f"{component.runScriptPath}/{component.runScript}"
        if getattr(component, "runScriptPath", None):
            return str(component.runScriptPath)
        return "N/A"

    def format_schedules(self, component: Component) -> str:
        schedules = [
            schedule
            for effective_day in sorted(component.schedules)
            for schedule in component.schedules[effective_day]
        ]

        if not schedules:
            return """
            <tr>
                <td colspan="3" style="padding: 12px; border: 1px solid #e5e7eb; color: #6b7280;">
                    No schedules configured
                </td>
            </tr>
            """

        rows = []
        for schedule in schedules:
            start_time = self._safe(getattr(schedule, "startTime", "N/A"))
            end_time = self._safe(getattr(schedule, "endTime", "N/A"))
            day_name = self._safe(self.get_day_names([getattr(schedule, "effectiveDay", "N/A")]))
            rows.append(
                f"""
                <tr>
                    <td style="padding: 12px; border: 1px solid #e5e7eb; color: #111827;">{start_time}</td>
                    <td style="padding: 12px; border: 1px solid #e5e7eb; color: #111827;">{end_time}</td>
                    <td style="padding: 12px; border: 1px solid #e5e7eb; color: #111827; font-weight: 600;">{day_name}</td>
                </tr>
                """
            )
        return "".join(rows)

    def _build_error_section(self, error_message: Optional[str]) -> str:
        if not error_message:
            return ""

        return f"""
        <div style="padding: 0 28px 24px 28px;">
            <div style="font-size: 18px; font-weight: 700; color: #b91c1c; margin-bottom: 12px;">
                Execution Error
            </div>
            <div style="
                background: #fef2f2;
                border: 1px solid #fecaca;
                border-left: 5px solid #ef4444;
                border-radius: 10px;
                padding: 14px;
                color: #7f1d1d;
                font-size: 13px;
                line-height: 1.6;
                white-space: pre-wrap;
                word-break: break-word;
                font-family: Consolas, 'Courier New', monospace;
            ">{self._safe(error_message)}</div>
        </div>
        """

    def _build_email_body(
        self,
        component: Component,
        message: str,
        icon: str,
        message_color: str,
        status_message: str,
        status_message_color: str,
        error_message: Optional[str] = None,
    ) -> str:
        tag = self._safe(getattr(component, "tag", "N/A"))
        server_ip = self._safe(getattr(var, "SERVER_IP", "unknown"))
        run_script_path = self._safe(self._get_run_script_path(component))
        component_name = self._safe(str(getattr(component, "name", "Unknown")).upper())
        error_section = self._build_error_section(error_message)

        return f"""
        <html>
            <head>
                <meta charset="UTF-8">
            </head>
            <body style="margin: 0; padding: 24px; background: #f3f4f6; font-family: Arial, Helvetica, sans-serif; color: #111827;">
                <div style="max-width: 760px; margin: 0 auto; background: #ffffff; border: 1px solid #e5e7eb; border-radius: 16px; overflow: hidden; box-shadow: 0 10px 30px rgba(0, 0, 0, 0.08);">

                    <div style="padding: 24px 28px; background: {message_color}; color: #ffffff;">
                        <div style="font-size: 28px; font-weight: 700; line-height: 1.3;">
                            {icon} {self._safe(message)}
                        </div>
                        <div style="margin-top: 6px; font-size: 14px; opacity: 0.95;">
                            Watcher component status notification
                        </div>
                    </div>

                    <div style="padding: 20px 28px 8px 28px;">
                        <span style="display: inline-block; padding: 8px 14px; border-radius: 999px; background: {status_message_color}; color: #ffffff; font-size: 13px; font-weight: 700;">
                            {self._safe(status_message)}
                        </span>
                    </div>

                    <div style="padding: 12px 28px 10px 28px;">
                        <div style="font-size: 18px; font-weight: 700; color: #111827; margin-bottom: 14px;">
                            Component Summary
                        </div>

                        <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
                            <tr>
                                <td style="padding: 12px; width: 220px; background: #f9fafb; border: 1px solid #e5e7eb; color: #6b7280; font-weight: 600;">Component Name</td>
                                <td style="padding: 12px; border: 1px solid #e5e7eb; color: #111827; font-weight: 700;">{component_name}</td>
                            </tr>
                            <tr>
                                <td style="padding: 12px; background: #f9fafb; border: 1px solid #e5e7eb; color: #6b7280; font-weight: 600;">Server</td>
                                <td style="padding: 12px; border: 1px solid #e5e7eb; color: #111827;">{server_ip}</td>
                            </tr>
                            <tr>
                                <td style="padding: 12px; background: #f9fafb; border: 1px solid #e5e7eb; color: #6b7280; font-weight: 600;">Tag</td>
                                <td style="padding: 12px; border: 1px solid #e5e7eb; color: #111827;">{tag}</td>
                            </tr>
                            <tr>
                                <td style="padding: 12px; background: #f9fafb; border: 1px solid #e5e7eb; color: #6b7280; font-weight: 600;">Run Script Path</td>
                                <td style="padding: 12px; border: 1px solid #e5e7eb; color: #111827; word-break: break-all; font-family: Consolas, 'Courier New', monospace;">{run_script_path}</td>
                            </tr>
                        </table>
                    </div>

                    <div style="padding: 10px 28px 24px 28px;">
                        <div style="font-size: 18px; font-weight: 700; color: #111827; margin-bottom: 14px;">
                            Schedules
                        </div>
                        <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
                            <thead>
                                <tr>
                                    <th style="text-align: left; padding: 12px; background: #111827; color: #ffffff; border: 1px solid #111827;">Start Time</th>
                                    <th style="text-align: left; padding: 12px; background: #111827; color: #ffffff; border: 1px solid #111827;">End Time</th>
                                    <th style="text-align: left; padding: 12px; background: #111827; color: #ffffff; border: 1px solid #111827;">Day</th>
                                </tr>
                            </thead>
                            <tbody>
                                {self.format_schedules(component)}
                            </tbody>
                        </table>
                    </div>

                    {error_section}

                    <div style="padding: 18px 28px 24px 28px; color: #6b7280; font-size: 12px; border-top: 1px solid #f3f4f6;">
                        Generated automatically by All-in-one-watcher. Please do not reply to this email.
                    </div>
                </div>
            </body>
        </html>
        """

    def _build_watcher_status_body(
        self,
        server_ip: str,
        message: str,
        icon: str,
        message_color: str,
        status_message: str,
        status_message_color: str,
    ) -> str:
        return f"""
        <html>
            <head>
                <meta charset="UTF-8">
            </head>
            <body style="margin: 0; padding: 24px; background: #f3f4f6; font-family: Arial, Helvetica, sans-serif; color: #111827;">
                <div style="max-width: 760px; margin: 0 auto; background: #ffffff; border: 1px solid #e5e7eb; border-radius: 16px; overflow: hidden; box-shadow: 0 10px 30px rgba(0, 0, 0, 0.08);">

                    <div style="padding: 24px 28px; background: {message_color}; color: #ffffff;">
                        <div style="font-size: 28px; font-weight: 700; line-height: 1.3;">
                            {icon} {self._safe(message)}
                        </div>
                        <div style="margin-top: 6px; font-size: 14px; opacity: 0.95;">
                            Watcher monitor status notification
                        </div>
                    </div>

                    <div style="padding: 20px 28px 8px 28px;">
                        <span style="display: inline-block; padding: 8px 14px; border-radius: 999px; background: {status_message_color}; color: #ffffff; font-size: 13px; font-weight: 700;">
                            {self._safe(status_message)}
                        </span>
                    </div>

                    <div style="padding: 12px 28px 10px 28px;">
                        <div style="font-size: 18px; font-weight: 700; color: #111827; margin-bottom: 14px;">
                            Watcher Summary
                        </div>

                        <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
                            <tr>
                                <td style="padding: 12px; width: 220px; background: #f9fafb; border: 1px solid #e5e7eb; color: #6b7280; font-weight: 600;">Service</td>
                                <td style="padding: 12px; border: 1px solid #e5e7eb; color: #111827; font-weight: 700;">All-in-one-watcher monitor</td>
                            </tr>
                            <tr>
                                <td style="padding: 12px; background: #f9fafb; border: 1px solid #e5e7eb; color: #6b7280; font-weight: 600;">Server</td>
                                <td style="padding: 12px; border: 1px solid #e5e7eb; color: #111827;">{self._safe(server_ip)}</td>
                            </tr>
                            <tr>
                                <td style="padding: 12px; background: #f9fafb; border: 1px solid #e5e7eb; color: #6b7280; font-weight: 600;">Status</td>
                                <td style="padding: 12px; border: 1px solid #e5e7eb; color: #111827; font-weight: 700;">{self._safe(status_message)}</td>
                            </tr>
                        </table>
                    </div>

                    <div style="padding: 18px 28px 24px 28px; color: #6b7280; font-size: 12px; border-top: 1px solid #f3f4f6;">
                        Generated automatically by All-in-one-watcher. Please do not reply to this email.
                    </div>
                </div>
            </body>
        </html>
        """

    def _deliver_message(self, message: MIMEMultipart, logger_with_name):
        try:
            server = smtplib.SMTP(self.smtp_server, 587, timeout=10)
            server.ehlo()
            server.starttls()
            server.login(self.username, self.password)
            server.sendmail(self.fromaddr, self.toaddr, message.as_string())
            server.quit()
            return True
        except smtplib.SMTPAuthenticationError as e:
            logger_with_name.exception(f"SMTP auth failed: {e}")
        except smtplib.SMTPException as e:
            logger_with_name.exception(f"SMTP error sending email: {e}")
        except Exception as e:
            logger_with_name.exception(f"Unexpected error sending email: {e}")
        return False

    def mail_send(
        self,
        component: Component,
        status_event: Optional[str] = None,
        error_message: Optional[str] = None,
    ):
        logger_with_name = logger.bind(comp_name=component.name)

        if not self.mail_enabled:
            logger_with_name.warning("Trying to send email but it's suppressed (disabled).")
            return

        msg = MIMEMultipart()
        msg["From"] = self.fromaddr
        msg["To"] = ", ".join(self.toaddr)

        process_pid = getattr(getattr(component, "process", None), "pid", None)
        component_name_upper = str(getattr(component, "name", "Unknown")).upper()
        event = (status_event or ("DOWN" if process_pid is None else "STARTED")).upper()

        if event == "DOWN":
            subject = f"{component_name_upper} [DOWN] on {getattr(var, 'SERVER_IP', 'unknown')}"
            icon = "⚠️"
            message_color = "#ef4444"
            message = f"{component_name_upper} COMPONENT IS DOWN"

            if getattr(component, "needToUp", False):
                status_message_color = "#2563eb"
                status_message = "Restart in progress"
            else:
                status_message_color = "#ef4444"
                status_message = "Restart not configured. Please check manually"
        elif event == "RESTARTED":
            subject = f"{component_name_upper} [RESTARTED] on {getattr(var, 'SERVER_IP', 'unknown')}"
            icon = "✅"
            message_color = "#16a34a"
            status_message_color = "#16a34a"
            message = f"{component_name_upper} RESTARTED AND IS WORKING NOW"
            status_message = "Watcher restarted the component successfully"
        else:
            subject = f"{component_name_upper} [STARTED] on {getattr(var, 'SERVER_IP', 'unknown')}"
            icon = "✅"
            message_color = "#16a34a"
            status_message_color = "#16a34a"
            message = f"{component_name_upper} STARTED AND IS WORKING NOW"
            status_message = "Watcher started the component successfully"

        msg["Subject"] = subject

        body = self._build_email_body(
            component=component,
            message=message,
            icon=icon,
            message_color=message_color,
            status_message=status_message,
            status_message_color=status_message_color,
            error_message=error_message,
        )
        msg.attach(MIMEText(body, "html"))

        if self._deliver_message(msg, logger_with_name):
            logger_with_name.info(
                f"Email sent to {self.toaddr} about {component.name} "
                f"[{event}]"
            )

    def watcher_status_send(self, status: str, server_ip: str):
        logger_with_name = logger.bind(comp_name="WatcherMonitor")

        if not self.mail_enabled:
            logger_with_name.warning("Trying to send watcher email but it's suppressed (disabled).")
            return

        msg = MIMEMultipart()
        msg["From"] = self.fromaddr
        msg["To"] = ", ".join(self.toaddr)

        if status == "down":
            subject = f"Watcher is Down on {server_ip}"
            icon = "⚠️"
            message_color = "#ef4444"
            message = f"WATCHER DOWN ON {server_ip}"
            status_message_color = "#2563eb"
            status_message = "Watcher restarting in progress"
        else:
            subject = f"Watcher is Started on {server_ip}"
            icon = "✅"
            message_color = "#16a34a"
            message = f"WATCHER STARTED ON {server_ip}"
            status_message_color = "#16a34a"
            status_message = "Watcher is running"

        msg["Subject"] = subject
        msg.attach(
            MIMEText(
                self._build_watcher_status_body(
                    server_ip=server_ip,
                    message=message,
                    icon=icon,
                    message_color=message_color,
                    status_message=status_message,
                    status_message_color=status_message_color,
                ),
                "html",
            )
        )

        if self._deliver_message(msg, logger_with_name):
            logger_with_name.info(f"Watcher status email sent to {self.toaddr} [{status.upper()}]")


@lru_cache(maxsize=1)
def get_mail_service() -> SendMail:
    return SendMail()
