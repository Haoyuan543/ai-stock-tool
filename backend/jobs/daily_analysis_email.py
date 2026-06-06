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
from backend.services.history_writer import write_history_to_supabase
from backend.services.prediction_validation_service import validate_due_predictions
from backend.services.report_auditor import audit_report


ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "data" / "scheduled_reports"
TZ = timezone(timedelta(hours=8))


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def parse_symbols(symbol: str, symbols: str = "") -> list[str]:
    raw = symbols or symbol
    for separator in [";", "\n", "\t", " "]:
        raw = raw.replace(separator, ",")
    parsed: list[str] = []
    seen: set[str] = set()
    for item in raw.split(","):
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        parsed.append(value)
    return parsed or ["2603.TW"]


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


def _email_settings() -> tuple[str, int, str, str, str, list[str]]:
    smtp_host = _env("SMTP_HOST")
    smtp_port = int(_env("SMTP_PORT", "587") or "587")
    smtp_user = _env("SMTP_USER")
    smtp_password = _env("SMTP_PASSWORD")
    sender = _env("REPORT_EMAIL_FROM", smtp_user)
    recipients = [item.strip() for item in _env("REPORT_EMAIL_TO").split(",") if item.strip()]
    return smtp_host, smtp_port, smtp_user, smtp_password, sender, recipients


def send_batch_email(items: list[dict[str, Any]]) -> bool:
    smtp_host, smtp_port, smtp_user, smtp_password, sender, recipients = _email_settings()
    if not smtp_host or not sender or not recipients:
        print("Batch email skipped: SMTP_HOST, REPORT_EMAIL_FROM/SMTP_USER, or REPORT_EMAIL_TO is not configured.")
        return False

    successful = [item for item in items if item.get("result")]
    failed = [item for item in items if item.get("error")]
    subject = f"每日 AI 投資分析：{len(successful)} 檔完成"
    if failed:
        subject += f"，{len(failed)} 檔失敗"

    lines = ["# 每日 AI 投資分析批次報告", ""]
    for item in successful:
        result = item["result"]
        summary = result.get("summary") or {}
        lines.extend(
            [
                f"## {result.get('symbol')}",
                f"- 今日結論：{summary.get('market_state', '')}",
                f"- 今日動作：{summary.get('action', '')}",
                f"- 一句話：{summary.get('one_line', '')}",
                f"- 買進建議：{summary.get('buy_advice', '')}",
                f"- 賣出建議：{summary.get('sell_advice', '')}",
                "",
            ]
        )
    for item in failed:
        lines.extend([f"## {item.get('symbol')} 失敗", f"- 錯誤：{item.get('error')}", ""])

    markdown = "\n".join(lines)
    html_body = markdown_to_html(markdown, subject)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(markdown)
    msg.add_alternative(html_body, subtype="html")

    for item in successful:
        files = item.get("files") or {}
        for label, path in files.items():
            if label == "json":
                continue
            maintype, subtype = ("text", "html") if path.suffix == ".html" else ("text", "markdown")
            msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
        smtp.ehlo()
        if _env("SMTP_STARTTLS", "true").lower() not in {"0", "false", "no"}:
            smtp.starttls()
            smtp.ehlo()
        if smtp_user and smtp_password:
            smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)

    print(f"Batch email sent to {', '.join(recipients)}")
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


def update_supabase_history(result: dict[str, Any]) -> bool:
    return write_history_to_supabase(result)


def run_one(
    service: AnalysisService,
    symbol: str,
    mode: str,
    model: str,
    manual_context: str,
) -> dict[str, Any]:
    result = service.analyze_now(
        symbol=symbol,
        mode=mode,
        model=model,
        manual_context=manual_context,
    )
    if _env("RUN_REPORT_AUDIT", "true").lower() in {"1", "true", "yes"}:
        result["report_audit"] = audit_report(result)
        audit = result["report_audit"]
        print(f"Report audit: score={audit['audit_score']}, needs_revision={audit['needs_revision']}")
    else:
        result["report_audit"] = {"audit_score": None, "needs_revision": False, "audit_warnings": [], "failed_rules": []}
    files = save_outputs(result)
    print("Saved report files:")
    for key, path in files.items():
        print(f"- {key}: {path}")
    update_google_sheet(result, files)
    update_supabase_history(result)
    return {"symbol": symbol, "result": result, "files": files, "error": ""}


def run(symbol: str, mode: str, model: str, manual_context: str, send: bool) -> dict[str, Any]:
    item = run_one(AnalysisService(), symbol, mode, model, manual_context)
    if send:
        send_email(item["result"], item["files"])
    return item["result"]


def run_many(symbols: list[str], mode: str, model: str, manual_context: str, send: bool) -> list[dict[str, Any]]:
    service = AnalysisService()
    items: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            print(f"Starting analysis: {symbol}")
            items.append(run_one(service, symbol, mode, model, manual_context))
        except Exception as exc:  # noqa: BLE001
            print(f"Analysis failed for {symbol}: {exc}")
            items.append({"symbol": symbol, "result": None, "files": {}, "error": str(exc)})

    if send:
        if len(items) == 1 and items[0].get("result"):
            send_email(items[0]["result"], items[0]["files"])
        else:
            send_batch_email(items)

    if not any(item.get("result") for item in items):
        raise RuntimeError("All scheduled analyses failed.")
    validate_due_predictions()
    return items


def main() -> None:
    parser = argparse.ArgumentParser(description="Run scheduled AI investment analysis and optionally email the report.")
    parser.add_argument("--symbol", default=_env("REPORT_SYMBOL", "2603.TW"))
    parser.add_argument("--symbols", default=_env("REPORT_SYMBOLS", ""))
    parser.add_argument("--mode", default=_env("REPORT_MODE", "personalized"), choices=["general", "personalized"])
    parser.add_argument("--model", default=_env("REPORT_MODEL", ""))
    parser.add_argument("--manual-context", default=_env("REPORT_MANUAL_CONTEXT", ""))
    parser.add_argument("--send-email", action="store_true", default=_env("SEND_EMAIL", "true").lower() in {"1", "true", "yes"})
    args = parser.parse_args()

    symbols = parse_symbols(args.symbol, args.symbols)
    items = run_many(symbols, args.mode, args.model, args.manual_context, args.send_email)
    successful = [item["result"] for item in items if item.get("result")]
    failed = [item for item in items if item.get("error")]
    print(
        json.dumps(
            {
                "symbols": symbols,
                "successful": len(successful),
                "failed": [{"symbol": item.get("symbol"), "error": item.get("error")} for item in failed],
                "results": [
                    {
                        "symbol": result.get("symbol"),
                        "mode": result.get("mode"),
                        "analysis_mode": result.get("analysis_mode"),
                        "model_used": result.get("model_used"),
                        "market_state": (result.get("summary") or {}).get("market_state"),
                        "action": (result.get("summary") or {}).get("action"),
                        "elapsed_seconds": result.get("elapsed_seconds"),
                    }
                    for result in successful
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
