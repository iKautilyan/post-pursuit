import json
import threading
from pathlib import Path
from typing import List, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from app.applicant import apply_to_job, get_user_data_dir
from app.db import (
    get_all_jobs,
    get_applied_jobs,
    get_job,
    get_new_shortlist,
    get_shortlist,
    get_unfiltered_jobs,
    init_db,
    update_application_status,
    update_filter_result,
)
from app.filters import apply_filters, load_profile, PROFILE_PATH
from app.generator import tailor_documents_for_job
from app.scraper import scrape_jobs

app = FastAPI(
    title="Job Automation & Tailoring Engine",
    version="2.0.0",
)

init_db()


# --------------------------------------------------
# Health & Status
# --------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok"
    }


# --------------------------------------------------
# Jobs & Ingestion
# --------------------------------------------------

@app.get("/jobs")
def list_jobs():
    jobs = get_all_jobs()
    return {
        "count": len(jobs),
        "jobs": jobs,
    }


import threading
from datetime import datetime, timezone

# Scrape status tracker for background tasks
scrape_state = {
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "total_found": 0,
    "details_fetched": 0,
    "progress": {},
    "error": None,
}


def _run_scrape_background(max_details: Optional[int], headless: bool):
    global scrape_state
    scrape_state["status"] = "running"
    scrape_state["started_at"] = datetime.now(timezone.utc).isoformat()
    scrape_state["finished_at"] = None
    scrape_state["error"] = None

    def on_progress(p):
        scrape_state["progress"] = p

    try:
        jobs = scrape_jobs(
            max_details=max_details,
            headless=headless,
            progress_callback=on_progress,
        )
        scrape_state["total_found"] = len(jobs)
        scrape_state["status"] = "completed"
    except Exception as e:
        scrape_state["status"] = "failed"
        scrape_state["error"] = str(e)
    finally:
        scrape_state["finished_at"] = datetime.now(timezone.utc).isoformat()


@app.get("/scrape/status")
def scrape_status():
    """Check the status of ongoing or completed scraping tasks."""
    return scrape_state


@app.post("/scrape")
def scrape(
    background: bool = Query(
        default=False,
        description="If True, runs scraping in the background and returns immediately.",
    ),
    max_details: Optional[int] = Query(
        default=25,
        description="Max job descriptions to fetch per run. Set to 0 to skip details, or higher for more.",
    ),
    headless: bool = Query(
        default=True,
        description="Run browser in headless mode (recommended for automation).",
    ),
):
    """Scrapes LinkedIn searches and stores/deduplicates jobs in SQLite.

    - Use background=true to prevent n8n / HTTP client timeouts.
    - Set max_details to control how many descriptions are fetched per run.
    """
    global scrape_state

    if background:
        if scrape_state["status"] == "running":
            return {
                "status": "already_running",
                "message": "A scraping task is already running in background.",
                "started_at": scrape_state["started_at"],
            }

        thread = threading.Thread(
            target=_run_scrape_background,
            args=(max_details, headless),
            daemon=True,
        )
        thread.start()

        return {
            "status": "accepted",
            "message": "Scraping started in background.",
            "check_status_url": "/scrape/status",
            "max_details": max_details,
            "headless": headless,
        }

    # Synchronous mode
    jobs = scrape_jobs(max_details=max_details, headless=headless)
    return {
        "count": len(jobs),
        "max_details": max_details,
        "jobs": jobs,
    }


# --------------------------------------------------
# Deterministic Filtering & Scoring (0–10)
# --------------------------------------------------

@app.post("/filter")
def filter_jobs():
    """Run profile-based 0-10 scoring on all unfiltered jobs."""
    unfiltered = get_unfiltered_jobs()
    profile = load_profile()
    min_score = profile.get("min_score", 7.0)

    passed = 0
    rejected = 0
    pending = 0
    results = []

    for job in unfiltered:
        result = apply_filters(job, profile=profile)
        update_filter_result(job["job_id"], result)

        summary = {
            "job_id": job["job_id"],
            "title": job["title"],
            "company": job["company"],
            "location": job.get("location"),
            "status": result.status,
            "score": result.score,
            "seniority": result.seniority,
            "work_model": result.work_model,
            "requires_german": result.requires_german,
            "visa_mentioned": result.visa_mentioned,
            "tech_stack": result.tech_stack,
            "reasons": result.reasons,
        }
        results.append(summary)

        if result.status == "passed" and result.score >= min_score:
            passed += 1
        elif result.status == "rejected":
            rejected += 1
        else:
            pending += 1

    return {
        "processed": len(unfiltered),
        "shortlisted": passed,
        "rejected": rejected,
        "pending": pending,
        "min_score": min_score,
        "jobs": results,
    }


@app.get("/shortlist")
def shortlist(
    min_score: float = Query(default=7.0),
    only_pending: bool = Query(default=False),
    only_unapplied: bool = Query(default=True),
):
    """Return all jobs meeting or exceeding the score threshold (default 7.0).
    If only_unapplied=true (default), returns jobs that haven't been marked as applied yet.
    If only_pending=true, returns only jobs that do not yet have a generated resume.
    """
    jobs = get_shortlist(min_score=min_score, only_pending=only_pending, only_unapplied=only_unapplied)
    return {
        "min_score": min_score,
        "only_pending": only_pending,
        "only_unapplied": only_unapplied,
        "count": len(jobs),
        "jobs": jobs,
    }


@app.get("/shortlist/new")
@app.get("/filter/shortlist/new")
def shortlist_new(
    min_score: float = Query(default=7.0),
    hours: int = Query(default=12),
    only_pending: bool = Query(default=False),
    only_unapplied: bool = Query(default=True),
):
    """Return new jobs discovered in the last N hours that meet the score threshold.
    If only_unapplied=true (default), returns jobs that haven't been marked as applied yet.
    """
    jobs = get_new_shortlist(min_score=min_score, hours=hours, only_pending=only_pending, only_unapplied=only_unapplied)
    return {
        "min_score": min_score,
        "hours": hours,
        "only_pending": only_pending,
        "only_unapplied": only_unapplied,
        "count": len(jobs),
        "jobs": jobs,
    }


# --------------------------------------------------
# AI Resume & Cover Letter Generation
# --------------------------------------------------

class GenerateRequest(BaseModel):
    job_id: Optional[str] = None
    api_key: Optional[str] = None
    force: Optional[bool] = False


class BatchGenerateRequest(BaseModel):
    job_ids: List[str]
    api_key: Optional[str] = None
    force: Optional[bool] = False


@app.post("/generate")
def generate_for_job(
    req: Optional[GenerateRequest] = None,
    job_id: Optional[str] = Query(default=None),
    api_key: Optional[str] = Query(default=None),
    force: bool = Query(default=False),
):
    """Tailor candidate's LaTeX resume to PDF and create custom cover letter for a job.

    Accepts job_id from JSON body or URL query parameter.
    If already generated, returns existing PDFs immediately unless force=true.
    """
    target_job_id = (req.job_id if req and req.job_id else None) or job_id
    target_api_key = (req.api_key if req and req.api_key else None) or api_key
    force_gen = (req.force if req and req.force is not None else False) or force

    if not target_job_id:
        raise HTTPException(
            status_code=400,
            detail="Missing 'job_id'. Send it in JSON body (e.g. {'job_id': '4463910895'}) or as query param ?job_id=4463910895",
        )

    target_job_id = str(target_job_id).strip().strip('"').strip("'")

    try:
        result = tailor_documents_for_job(target_job_id, api_key=target_api_key, force=force_gen)
        return {
            "status": "success",
            "result": result,
            "download_urls": {
                "resume_pdf": f"/download/resume/{target_job_id}",
                "cover_letter_pdf": f"/download/cover-letter/{target_job_id}",
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/generate/batch")
def generate_batch(req: BatchGenerateRequest):
    """Batch generate tailored resume and cover letter for multiple jobs."""
    results = []
    errors = []

    for jid in req.job_ids:
        try:
            res = tailor_documents_for_job(jid, api_key=req.api_key)
            results.append(res)
        except Exception as e:
            errors.append({"job_id": jid, "error": str(e)})

    return {
        "generated_count": len(results),
        "error_count": len(errors),
        "results": results,
        "errors": errors,
    }


@app.get("/download/resume/{job_id}")
def download_resume(job_id: str):
    """Download the tailored PDF resume for a specific job."""
    clean_id = str(job_id).strip().strip('"').strip("'")
    job = get_job(clean_id)
    if not job or not job.get("resume_path"):
        raise HTTPException(status_code=404, detail="Resume not generated for this job")

    pdf_path = Path(job["resume_path"])
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="Resume PDF file missing")

    filename = f"Ritobrita_De_Resume_{job.get('company', 'Company')}_{clean_id}.pdf"
    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=filename,
    )


@app.get("/download/cover-letter/{job_id}")
def download_cover_letter(job_id: str):
    """Download the tailored cover letter PDF (or text) for a specific job."""
    clean_id = str(job_id).strip().strip('"').strip("'")
    job = get_job(clean_id)
    if not job or not job.get("cover_letter_path"):
        raise HTTPException(status_code=404, detail="Cover letter not generated for this job")

    doc_path = Path(job["cover_letter_path"])
    if not doc_path.exists():
        raise HTTPException(status_code=404, detail="Cover letter file missing")

    media_type = "application/pdf" if doc_path.suffix == ".pdf" else "text/plain"
    filename = f"Ritobrita_De_Cover_Letter_{job.get('company', 'Company')}_{clean_id}{doc_path.suffix}"
    return FileResponse(
        path=str(doc_path),
        media_type=media_type,
        filename=filename,
    )


# --------------------------------------------------
# Application Automation (1-Click & Safety Mode)
# --------------------------------------------------

@app.get("/apply-job", response_class=HTMLResponse)
def apply_job_ui(
    job_id: str = Query(...),
    auto_submit: bool = Query(default=False),
):
    """Browser-friendly 1-Click trigger endpoint for Slack messages.
    Launches application automation in a background thread and displays
    a real-time status page to the user.
    """
    clean_id = str(job_id).strip().strip('"').strip("'")
    job = get_job(clean_id)
    if not job:
        return HTMLResponse(
            content=f"""
            <html>
                <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 40px; text-align: center;">
                    <h2 style="color: #e53e3e;">Job Not Found</h2>
                    <p>Job ID <code>{clean_id}</code> was not found in the database.</p>
                </body>
            </html>
            """,
            status_code=404,
        )

    title = job.get("title", "Role")
    company = job.get("company", "Company")
    location = job.get("location", "")
    score = job.get("score", "N/A")
    job_url = job.get("url", f"https://www.linkedin.com/jobs/view/{clean_id}/")

    # Launch application workflow in a daemon thread so the browser tab responds immediately
    def _run_apply():
        try:
            apply_to_job(clean_id, auto_submit=auto_submit, headless=False)
        except Exception as e:
            print(f"Error in background apply for {clean_id}: {e}")

    threading.Thread(target=_run_apply, daemon=True).start()

    mode_text = "Full Auto-Submit" if auto_submit else "Safety Mode (Pauses at Review Screen)"
    mode_color = "#38a169" if auto_submit else "#3182ce"

    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Applying: {title} @ {company}</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
                background-color: #f7fafc;
                color: #2d3748;
                display: flex;
                align-items: center;
                justify-content: center;
                min-height: 100vh;
                margin: 0;
            }}
            .card {{
                background: white;
                border-radius: 12px;
                box-shadow: 0 10px 25px rgba(0, 0, 0, 0.08);
                max-width: 600px;
                width: 90%;
                padding: 32px;
                border: 1px solid #e2e8f0;
            }}
            .badge {{
                display: inline-block;
                padding: 4px 12px;
                border-radius: 9999px;
                font-size: 12px;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                background-color: {mode_color};
                color: white;
            }}
            .score-badge {{
                display: inline-block;
                padding: 4px 10px;
                border-radius: 6px;
                font-size: 13px;
                font-weight: 700;
                background: #ebf8ff;
                color: #2b6cb0;
                margin-left: 8px;
            }}
            h1 {{
                font-size: 22px;
                margin: 16px 0 6px 0;
                color: #1a202c;
            }}
            .company {{
                font-size: 16px;
                color: #4a5568;
                margin-bottom: 20px;
            }}
            .info-box {{
                background-color: #f0fff4;
                border: 1px solid #c6f6d5;
                color: #22543d;
                border-radius: 8px;
                padding: 16px;
                margin: 20px 0;
                font-size: 14px;
                line-height: 1.5;
            }}
            .buttons {{
                display: flex;
                gap: 12px;
                margin-top: 24px;
                flex-wrap: wrap;
            }}
            .btn {{
                padding: 10px 16px;
                border-radius: 6px;
                text-decoration: none;
                font-weight: 600;
                font-size: 13px;
                display: inline-flex;
                align-items: center;
                transition: opacity 0.2s;
            }}
            .btn:hover {{ opacity: 0.85; }}
            .btn-primary {{ background: #0073b1; color: white; }}
            .btn-outline {{ background: #edf2f7; color: #2d3748; }}
        </style>
    </head>
    <body>
        <div class="card">
            <span class="badge">{mode_text}</span>
            <span class="score-badge">Match: {score}/10</span>
            <h1>{title}</h1>
            <div class="company">{company} &bull; {location}</div>
            
            <div class="info-box">
                <strong>Application Assistant Launched!</strong><br>
                Playwright has opened the LinkedIn application window on your desktop. It is attaching your tailored ATS resume and stepping through the screens.
                <br><br>
                <em>Check your open browser window to review the pre-filled fields and click submit!</em>
            </div>

            <div class="buttons">
                <a href="/download/resume/{clean_id}" class="btn btn-outline" target="_blank">📄 View Resume PDF</a>
                <a href="/download/cover-letter/{clean_id}" class="btn btn-outline" target="_blank">📝 View Cover Letter</a>
                <a href="{job_url}" class="btn btn-primary" target="_blank">🔗 Open LinkedIn Listing</a>
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.post("/apply/{job_id}")
def apply_endpoint(
    job_id: str,
    auto_submit: bool = Query(default=False),
):
    """Programmatic API to trigger application workflow."""
    clean_id = str(job_id).strip().strip('"').strip("'")
    return apply_to_job(clean_id, auto_submit=auto_submit, headless=False)


@app.get("/applied")
def applied_jobs_list():
    """Retrieve all jobs marked as applied, review_ready, or external_redirect."""
    return {
        "jobs": get_applied_jobs()
    }


@app.get("/auth/linkedin")
def launch_linkedin_login():
    """Launch visible browser to perform one-time LinkedIn login."""
    user_dir = get_user_data_dir()
    
    def _open():
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(user_dir.resolve()),
                headless=False,
                viewport={"width": 1280, "height": 900},
                args=["--start-maximized"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://www.linkedin.com/login")
            print("Login browser opened. Log in to LinkedIn and close the browser window when done.")
            page.wait_for_timeout(120000)

    threading.Thread(target=_open, daemon=True).start()
    return {
        "status": "opened",
        "message": "Opened visible browser window for one-time LinkedIn login. Please log in on your screen.",
    }


@app.get("/mark-applied", response_class=HTMLResponse)
def mark_job_applied_ui(
    job_id: str = Query(...),
):
    """Browser-friendly 1-click endpoint from Slack to mark a job as applied.
    Once applied, it will no longer appear in future shortlist reminders.
    """
    clean_id = str(job_id).strip().strip('"').strip("'")
    job = get_job(clean_id)
    if not job:
        return HTMLResponse(
            content=f"""
            <html>
                <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 40px; text-align: center;">
                    <h2 style="color: #e53e3e;">Job Not Found</h2>
                    <p>Job ID <code>{clean_id}</code> was not found.</p>
                </body>
            </html>
            """,
            status_code=404,
        )

    update_application_status(clean_id, "applied", notes="Manually confirmed as applied by user via Slack")

    title = job.get("title", "Role")
    company = job.get("company", "Company")

    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Marked Applied: {title} @ {company}</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
                background-color: #f0fff4;
                color: #2d3748;
                display: flex;
                align-items: center;
                justify-content: center;
                min-height: 100vh;
                margin: 0;
            }}
            .card {{
                background: white;
                border-radius: 12px;
                box-shadow: 0 10px 25px rgba(0, 0, 0, 0.08);
                max-width: 520px;
                width: 90%;
                padding: 36px;
                text-align: center;
                border: 1px solid #c6f6d5;
            }}
            .icon {{
                font-size: 48px;
                margin-bottom: 12px;
            }}
            h1 {{
                font-size: 22px;
                color: #22543d;
                margin: 0 0 10px 0;
            }}
            .job-info {{
                font-size: 16px;
                color: #4a5568;
                margin-bottom: 24px;
            }}
            .desc {{
                background: #f7fafc;
                border: 1px solid #edf2f7;
                border-radius: 8px;
                padding: 16px;
                font-size: 14px;
                color: #4a5568;
                line-height: 1.5;
                margin-bottom: 24px;
            }}
            .undo-btn {{
                color: #a0aec0;
                text-decoration: underline;
                font-size: 13px;
                display: inline-block;
                margin-top: 12px;
            }}
            .undo-btn:hover {{ color: #718096; }}
        </style>
    </head>
    <body>
        <div class="card">
            <div class="icon">🎉</div>
            <h1>Application Recorded!</h1>
            <div class="job-info"><strong>{title}</strong> at <strong>{company}</strong></div>
            
            <div class="desc">
                ✅ Database updated: status is now <strong>Applied</strong>.<br>
                This job is now officially tracked and will <strong>no longer appear in your Slack reminder runs</strong>.
            </div>

            <div>
                <a href="/unmark-applied?job_id={clean_id}" class="undo-btn">Did you click by mistake? Undo (Mark as Not Applied)</a>
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/unmark-applied", response_class=HTMLResponse)
def unmark_job_applied_ui(job_id: str = Query(...)):
    """Reset application status back to not_applied."""
    clean_id = str(job_id).strip().strip('"').strip("'")
    job = get_job(clean_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    update_application_status(clean_id, "not_applied", notes="Reset to unapplied by user")
    return HTMLResponse(
        content=f"""
        <html>
            <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 40px; text-align: center;">
                <h2>↩️ Reset to Not Applied</h2>
                <p><strong>{job.get('title')}</strong> at <strong>{job.get('company')}</strong> has been reset.</p>
                <p>It will be included again in your next scheduled reminder run.</p>
            </body>
        </html>
        """
    )


# --------------------------------------------------
# Profile Management
# --------------------------------------------------

@app.get("/profile")
def get_profile():
    """Retrieve the candidate profile."""
    return load_profile()


@app.put("/profile")
def update_profile(updated: dict):
    """Update profile.json with new skills or settings as you upskill."""
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(updated, f, indent=2)
    return {
        "status": "updated",
        "profile": updated,
    }
