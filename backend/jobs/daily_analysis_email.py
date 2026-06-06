from __future__ import annotations

import argparse
import html
import json
import os
import smtplib
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from backend.integrations.google_sheets import append_analysis_summary
from backend.services.analysis_service import AnalysisService


ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "data" / "scheduled_reports"
TZ = timezone(timedelta(hours=8))


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _line_to_html(line: str) -> str:
    escaped = html.escape(line)
    if escaped.startswith("# "):
        return f"<h1>{escaped[2:]}</h1>"
    if escaped.startswith("## "):
        return f"<h2>{escaped[3:]}</h2>"
    if escaped.startswith("### "):
        return f"<h3>{escaped[4:]}</h3>"
    if escaped.startswith("- "):
        return f"<li>{escaped[2:]}</li>"
    if escaped.startswith(tuple(f"{i}. " for i in range(1, 10))):
        return f"<p class=\"brief\">{escaped}</p>"
    if not escaped.strip():
        return "<br>"
    return f"<p>{escaped}</p>"


def markdown_to_html(markdown: str, title: str) -> str:
    body = "\n".join(_line_to_html(line) for line in markdown.splitlines())
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans TC", Arial, sans-serif;
      line-height: 1.65;
      color: #10201c;
      background: #fbfaf6;
      max-width: 980px;
      margin: 0 auto;
      padding: 32px 22px 56px;
    }}
    h1 {{ font-size: 28px; margin: 0 0 18px; }}
    h2 {{
      font-size: 20px;
      margin: 28px 0 10px;
      padding: 10px 12px;
      border-left: 5px solid #087f6f;
      background: #eef8f4;
    }}
    h3 {{ font-size: 16px; margin: 20px 0 8px; color: #14584e; }}
    p, li {{ font-size: 15px; }}
    li {{ margin: 4px 0; }}
    .brief {{
      font-weight: 650;
      background: #fff;
      border: 1px solid #d6e6df;
      border-radius: 6px;
      padding: 8px 10px;
      margin: 6px 0;
    }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""


def build_subject(result: dict[str, Any]) -> str:
    summary = result.get("summary") or {}
    state = summary.get("market_state") or "分析完成"
    action = summary.get("action") or ""
    symbol = result.get("symbol") or ""
    return f"每日 AI 投資分析：{symbol} - {state} - {action}".strip()


def save_outputs(result: dict[str, Any]) -> dict[str, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    symbol = str(result.get("symbol") or "report").replace("/", "_")
    base = REPORT_DIR / f"{stamp}_{symbol}"
    markdown_path = base.with_suffix(".md")
    html_path = base.with_suffix(".html")
    json_path = base.with_suffix(".json")

    markdown = result.get("report_markdown") or result.get("ai_report") or ""
    title = build_subject(result)
    markdown_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(markdown_to_html(markdown, title), encoding="utf-8")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"markdown": markdown_path, "html": html_path, "json": json_path}


def send_email(result: dict[str, Any], files: dict[str, Path]) -> bool:
    smtp_host = _env("SMTP_HOST")
    smtp_port = int(_env("SMTP_PORT", "587") or "587")
    smtp_user = _env("SMTP_USER")
    smtp_password = _env("SMTP_PASSWORD")
    sender = _env("REPORT_EMAIL_FROM", smtp_user)
    recipients = [item.strip() for item in _env("REPORT_EMAIL_TO").split(",") if item.strip()]

    if not smtp_host or not sender or not recipients:
        print("Email skipped: SMTP_HOST, REPORT_EMAIL_FROM/SMTP_USER, or REPORT_EMAIL_TO is not configured.")
        return False

    subject = build_subject(result)
    markdown = files["markdown"].read_text(encoding="utf-8")
    html_body = files["html"].read_text(encoding="utf-8")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(markdown)
    msg.add_alternative(html_body, subtype="html")

    for label, path in files.items():
        if label == "json":
            continue
        maintype, subtype = ("text", "html") if path.suffix == ".html" else ("text", "markdown")
        msg.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
        smtp.ehlo()
        if _env("SMTP_STARTTLS", "true").lower() not in {"0", "false", "no"}:
            smtp.starttls()
            smtp.ehlo()
        if smtp_user and smtp_password:
            smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)

    print(f"Email sent to {', '.join(recipients)}")
    return True


def update_google_sheet(result: dict[str, Any], files: dict[str, Path]) -> bool:
    if _env("UPDATE_GOOGLE_SHEET", "true").lower() not in {"1", "true", "yes"}:
        print("Google Sheet skipped: UPDATE_GOOGLE_SHEET is false.")
        return False
    try:
        return append_analysis_summary(result, files)
    except Exception as exc:  # noqa: BLE001
        print(f"Google Sheet update failed: {exc}")
        if _env("GOOGLE_SHEET_REQUIRED", "false").lower() in {"1", "true", "yes"}:
            raise
        return False


def run(symbol: str, mode: str, model: str, manual_context: str, send: bool) -> dict[str, Any]:
    result = AnalysisService().analyze_now(
        symbol=symbol,
        mode=mode,
        model=model,
        manual_context=manual_context,
    )
    files = save_outputs(result)
    print("Saved report files:")
    for key, path in files.items():
        print(f"- {key}: {path}")
    update_google_sheet(result, files)
    if send:
        send_email(result, files)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run scheduled AI investment analysis and optionally email the report.")
    parser.add_argument("--symbol", default=_env("REPORT_SYMBOL", "2603.TW"))
    parser.add_argument("--mode", default=_env("REPORT_MODE", "personalized"), choices=["general", "personalized"])
    parser.add_argument("--model", default=_env("REPORT_MODEL", ""))
    parser.add_argument("--manual-context", default=_env("REPORT_MANUAL_CONTEXT", ""))
    parser.add_argument("--send-email", action="store_true", default=_env("SEND_EMAIL", "true").lower() in {"1", "true", "yes"})
    args = parser.parse_args()

    result = run(args.symbol, args.mode, args.model, args.manual_context, args.send_email)
    summary = result.get("summary") or {}
    print(
        json.dumps(
            {
                "symbol": result.get("symbol"),
                "mode": result.get("mode"),
                "analysis_mode": result.get("analysis_mode"),
                "model_used": result.get("model_used"),
                "market_state": summary.get("market_state"),
                "action": summary.get("action"),
                "elapsed_seconds": result.get("elapsed_seconds"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
