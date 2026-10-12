import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

from app.db import get_job, update_application_status
from app.filters import load_profile
from app.generator import tailor_documents_for_job

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR.parent / "data"
USER_DATA_DIR = DATA_DIR / "chrome_user_data"


def get_user_data_dir() -> Path:
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return USER_DATA_DIR


def is_logged_in_to_linkedin(page) -> bool:
    """Check if the current browser session is logged in to LinkedIn."""
    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)
    current_url = page.url
    # If redirected to login, signup, or guest feed
    if any(auth_kw in current_url for auth_kw in ["/login", "/signup", "/authwall", "uas/login"]):
        return False
    # Check for feed identity element or global nav
    if page.locator(".global-nav__me, .feed-identity-module, [data-control-name='nav.settings']").count() > 0:
        return True
    return False


def fill_common_form_fields(page, profile: dict):
    """Fill commonly encountered input fields in LinkedIn Easy Apply modal."""
    # Phone number
    phone = profile.get("phone", "")
    if phone:
        phone_inputs = page.locator("input[id*='phoneNumber'], input[name*='phoneNumber'], input[aria-label*='phone' i]")
        if phone_inputs.count() > 0 and not phone_inputs.first.input_value():
            try:
                phone_inputs.first.fill(phone)
            except Exception:
                pass

    # Years of experience numeric inputs
    yoe = str(profile.get("years_of_experience", 6))
    numeric_inputs = page.locator("input[type='text'][id*='numeric'], input[type='number']")
    for i in range(numeric_inputs.count()):
        try:
            inp = numeric_inputs.nth(i)
            if not inp.input_value():
                inp.fill(yoe)
        except Exception:
            pass


def handle_file_upload(page, resume_pdf_path: Path) -> bool:
    """Upload resume PDF to Easy Apply file input if present."""
    if not resume_pdf_path.exists():
        return False

    file_inputs = page.locator("input[type='file']")
    if file_inputs.count() > 0:
        try:
            file_inputs.first.set_input_files(str(resume_pdf_path.resolve()))
            time.sleep(2)
            return True
        except Exception as e:
            print(f"File upload error: {e}")
    return False


def apply_to_job(job_id: str, auto_submit: bool = False, headless: bool = False) -> Dict[str, Any]:
    """Execute application workflow for a job.
    
    In Safety Mode (auto_submit=False):
      Steps through LinkedIn Easy Apply, attaches tailored PDF, and stops at the final
      Review screen so the user can review and click Submit in the browser window.
    
    If External ATS:
      Opens the company career page and prepares the tailored documents.
    """
    clean_id = str(job_id).strip().strip('"').strip("'")
    job = get_job(clean_id)
    if not job:
        raise ValueError(f"Job ID '{job_id}' not found in database")

    profile = load_profile()

    # Ensure resume PDF is generated
    resume_path = job.get("resume_path")
    if not resume_path or not Path(resume_path).exists():
        print(f"Tailoring documents for job {clean_id} before applying...")
        tailor_result = tailor_documents_for_job(clean_id)
        resume_path = tailor_result.get("resume_pdf")

    resume_pdf = Path(resume_path)
    user_dir = get_user_data_dir()

    job_url = job.get("url")
    if not job_url or "linkedin.com" not in job_url:
        job_url = f"https://www.linkedin.com/jobs/view/{clean_id}/"

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(user_dir.resolve()),
            headless=headless,
            viewport={"width": 1280, "height": 900},
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
        )

        page = context.pages[0] if context.pages else context.new_page()

        try:
            # 1. Navigate to Job URL
            page.goto(job_url, wait_until="domcontentloaded", timeout=45000)
            time.sleep(3)

            # 2. Check for Easy Apply Button
            easy_apply_btn = page.locator("button.jobs-apply-button, button:has-text('Easy Apply')").first
            
            if easy_apply_btn.count() == 0:
                # Check for external Apply button
                external_apply_btn = page.locator("button:has-text('Apply'), a:has-text('Apply')").first
                if external_apply_btn.count() > 0:
                    ext_page = None
                    try:
                        with page.expect_popup(timeout=8000) as popup_info:
                            external_apply_btn.click()
                        ext_page = popup_info.value
                        ext_page.wait_for_load_state("domcontentloaded")
                        actual_url = ext_page.url
                    except Exception:
                        actual_url = page.url
                        ext_page = page

                    # Automatically reveal tailored resume PDF in macOS Finder so it's ready to drag-and-drop!
                    try:
                        subprocess.Popen(["open", "-R", str(resume_pdf.resolve())])
                    except Exception:
                        pass

                    update_application_status(clean_id, "external_redirect", notes=f"Redirected to external portal: {actual_url}")
                    
                    target_page = ext_page or page
                    target_page.bring_to_front()
                    print(f"External ATS page open: {actual_url}. Keeping browser open for user to apply...")

                    # Keep browser open while user completes the application!
                    try:
                        while not target_page.is_closed():
                            time.sleep(2)
                    except Exception:
                        pass

                    return {
                        "status": "external_redirect",
                        "job_id": clean_id,
                        "title": job.get("title"),
                        "company": job.get("company"),
                        "resume_pdf": str(resume_pdf),
                        "url": actual_url,
                        "message": "External ATS job. Company application page opened with tailored resume ready.",
                    }

                update_application_status(clean_id, "failed", notes="Apply button not found on page")
                return {
                    "status": "failed",
                    "job_id": clean_id,
                    "title": job.get("title"),
                    "company": job.get("company"),
                    "message": "Apply button not found. Please check job listing manually.",
                }

            # 3. Click Easy Apply
            easy_apply_btn.click()
            time.sleep(2)

            modal = page.locator(".jobs-easy-apply-modal, [role='dialog']").first
            if modal.count() == 0:
                update_application_status(clean_id, "failed", notes="Easy Apply modal did not open")
                return {
                    "status": "failed",
                    "job_id": clean_id,
                    "message": "Easy Apply modal failed to open.",
                }

            # 4. Multi-step form completion
            max_steps = 10
            step = 0
            while step < max_steps:
                step += 1
                fill_common_form_fields(modal, profile)
                handle_file_upload(modal, resume_pdf)

                # Check if we reached the final "Submit application" stage
                submit_btn = modal.locator("button:has-text('Submit application'), button[aria-label*='Submit application' i]").first
                if submit_btn.count() > 0 and submit_btn.is_visible():
                    if auto_submit:
                        submit_btn.click()
                        time.sleep(3)
                        update_application_status(clean_id, "applied", notes="Auto-submitted via Easy Apply")
                        return {
                            "status": "applied",
                            "job_id": clean_id,
                            "title": job.get("title"),
                            "company": job.get("company"),
                            "message": "Application submitted successfully via LinkedIn Easy Apply!",
                        }
                    else:
                        # Safety Mode: Pause on the review screen for user confirmation
                        update_application_status(clean_id, "ready_to_submit", notes="Paused at final review screen")
                        page.bring_to_front()
                        
                        # Keep browser open until user submits or closes the window!
                        print("Application paused at review screen for user review. Browser remains open...")
                        try:
                            while not page.is_closed():
                                # Check if user clicked submit
                                if modal.count() == 0 or page.locator("text='Application sent'").count() > 0:
                                    update_application_status(clean_id, "applied", notes="User submitted from review screen")
                                    time.sleep(3)
                                    return {
                                        "status": "applied",
                                        "job_id": clean_id,
                                        "title": job.get("title"),
                                        "company": job.get("company"),
                                        "message": "Application submitted successfully!",
                                    }
                                time.sleep(2)
                        except Exception:
                            pass
                        
                        return {
                            "status": "ready_to_submit",
                            "job_id": clean_id,
                            "title": job.get("title"),
                            "company": job.get("company"),
                            "message": "Application pre-filled and paused at Review screen in browser window.",
                        }

                # Check for "Review" button
                review_btn = modal.locator("button:has-text('Review'), button[aria-label*='Review' i]").first
                if review_btn.count() > 0 and review_btn.is_visible():
                    review_btn.click()
                    time.sleep(2)
                    continue

                # Check for "Next" button
                next_btn = modal.locator("button:has-text('Next'), button[aria-label*='Next' i]").first
                if next_btn.count() > 0 and next_btn.is_visible():
                    next_btn.click()
                    time.sleep(2)
                    continue

                # Neither Next, Review, nor Submit found (could be additional required fields)
                break

            # If we exited the loop without submitting or reaching review, keep browser open for user
            update_application_status(clean_id, "pending_manual_input", notes="Complex questions require manual input")
            page.bring_to_front()
            try:
                subprocess.Popen(["open", "-R", str(resume_pdf.resolve())])
            except Exception:
                pass
            try:
                while not page.is_closed():
                    time.sleep(2)
            except Exception:
                pass
            return {
                "status": "pending_manual_input",
                "job_id": clean_id,
                "title": job.get("title"),
                "company": job.get("company"),
                "message": "Easy Apply opened with tailored resume attached. Please complete custom employer questions in browser.",
            }

        except Exception as e:
            update_application_status(clean_id, "failed", notes=str(e))
            return {
                "status": "failed",
                "job_id": clean_id,
                "error": str(e),
                "message": f"Application error: {str(e)}",
            }
