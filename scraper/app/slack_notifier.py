import os
import json
import urllib.parse
from pathlib import Path
from typing import Dict, Any, Optional

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    SLACK_SDK_AVAILABLE = True
except ImportError:
    SLACK_SDK_AVAILABLE = False

import urllib.request


def get_slack_config() -> Dict[str, Optional[str]]:
    """Retrieve Slack credentials and repo configuration from environment."""
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    channel_id = os.environ.get("SLACK_CHANNEL_ID")
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    github_repo = os.environ.get("GITHUB_REPOSITORY", "ritode/job-automation")

    # Diagnostic logging (masks sensitive parts)
    token_status = f"Found (starts with {bot_token[:6]}..., len={len(bot_token)})" if bot_token else "EMPTY / NOT SET"
    channel_status = f"Found ({channel_id})" if channel_id else "EMPTY / NOT SET"
    webhook_status = "Found" if webhook_url else "EMPTY / NOT SET"
    print(f"[Slack Config Debug] SLACK_BOT_TOKEN: {token_status} | SLACK_CHANNEL_ID: {channel_status} | SLACK_WEBHOOK_URL: {webhook_status} | SDK_AVAILABLE: {SLACK_SDK_AVAILABLE}")

    # Fallback to local .env if not found in os.environ
    if not (bot_token or webhook_url):
        env_file = Path(__file__).resolve().parent.parent / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("SLACK_BOT_TOKEN="):
                    bot_token = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("SLACK_CHANNEL_ID="):
                    channel_id = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("SLACK_WEBHOOK_URL="):
                    webhook_url = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("GITHUB_REPOSITORY="):
                    github_repo = line.split("=", 1)[1].strip().strip('"').strip("'")

    return {
        "bot_token": bot_token,
        "channel_id": channel_id,
        "webhook_url": webhook_url,
        "github_repo": github_repo,
    }


def format_slack_message(job: Dict[str, Any], github_repo: str) -> str:
    """Format a clean, mobile-optimized Slack message with action links."""
    job_id = job.get("job_id", "")
    title = job.get("title", "Role")
    company = job.get("company", "Company")
    location = job.get("location") or "Not specified"
    score = job.get("score", "N/A")
    raw_stack = job.get("tech_stack", "")

    # Clean tech stack string
    if isinstance(raw_stack, list):
        clean_stack = ", ".join(raw_stack)
    else:
        clean_stack = str(raw_stack).replace("[", "").replace("]", "").replace('"', "").replace("'", "")

    job_url = job.get("url") or f"https://www.linkedin.com/jobs/view/{job_id}/"

    # Pre-filled GitHub issue URL to mark applied from phone in 1 tap
    encoded_title = urllib.parse.quote(f"applied:{job_id}")
    encoded_body = urllib.parse.quote(
        f"Marked as applied from Slack for {title} at {company} (Score: {score}/10).\nJob URL: {job_url}"
    )
    mark_applied_url = f"https://github.com/{github_repo}/issues/new?title={encoded_title}&body={encoded_body}"

    msg = (
        f"🎯 *{title}* at *{company}*\n"
        f"📍 {location}  |  ⭐️ Match Score: *{score} / 10*\n"
        f"🛠️ *Stack:* `{clean_stack}`\n\n"
        f"🔗 <{job_url}|Open Listing on LinkedIn>\n"
        f"✅ *<{mark_applied_url}|Tap Here to Mark as Applied (Stops Reminders)>*\n"
    )
    return msg


def send_slack_notification(
    job: Dict[str, Any],
    resume_pdf_path: Optional[Path] = None,
    cover_letter_pdf_path: Optional[Path] = None,
) -> bool:
    """Send notification to Slack, attaching the tailored resume PDF if available."""
    config = get_slack_config()
    bot_token = config["bot_token"]
    channel_id = config["channel_id"]
    webhook_url = config["webhook_url"]
    github_repo = config["github_repo"] or "ritode/job-automation"

    message_text = format_slack_message(job, github_repo)

    # Method 1: Slack Bot Token with Native File Attachment (Best for Mobile!)
    if bot_token and channel_id and SLACK_SDK_AVAILABLE:
        client = WebClient(token=bot_token)
        try:
            if resume_pdf_path and Path(resume_pdf_path).exists():
                pdf_file = Path(resume_pdf_path)
                filename = f"Ritobrita_De_Resume_{job.get('company', 'Company')}_{job.get('job_id')}.pdf"

                # Upload resume PDF directly into channel with description
                client.files_upload_v2(
                    channel=channel_id,
                    file=str(pdf_file.resolve()),
                    filename=filename,
                    title=f"Tailored Resume: {job.get('title')}",
                    initial_comment=message_text,
                )
                print(f"Sent Slack message with attached resume for {job.get('job_id')} via Slack SDK.")
                return True
            else:
                client.chat_postMessage(
                    channel=channel_id,
                    text=message_text,
                )
                print(f"Sent Slack message for {job.get('job_id')} via Slack SDK.")
                return True
        except SlackApiError as e:
            print(f"Slack API Error: {e.response.get('error', e)}")

    # Method 2: Incoming Webhook (Fallback)
    if webhook_url:
        try:
            payload = json.dumps({"text": message_text}).encode("utf-8")
            req = urllib.request.Request(
                webhook_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    print(f"Sent Slack message for {job.get('job_id')} via Webhook.")
                    return True
                else:
                    print(f"Slack Webhook returned HTTP {resp.status}")
        except Exception as e:
            print(f"Slack Webhook failed: {e}")

    print("Warning: Neither SLACK_BOT_TOKEN+SLACK_CHANNEL_ID nor SLACK_WEBHOOK_URL is configured.")
    return False
