import os
import re
from typing import Callable, Dict, List, Optional
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright

from app.db import get_job, save_jobs, update_job_description
from app.filters import detect_seniority, load_profile


# --------------------------------------------------
# Configuration
# --------------------------------------------------

SEARCHES = [
    # Europe - Germany
    {"keywords": "Frontend Developer", "location": "Germany"},
    {"keywords": "React Developer", "location": "Germany"},
    {"keywords": "Full Stack Developer", "location": "Germany"},

    # Europe - Netherlands & Ireland
    {"keywords": "Frontend Developer", "location": "Netherlands"},
    {"keywords": "Full Stack Developer", "location": "Netherlands"},
    {"keywords": "Frontend Developer", "location": "Ireland"},

    # Europe - UK & Switzerland
    {"keywords": "React Developer", "location": "United Kingdom"},
    {"keywords": "Full Stack Developer", "location": "United Kingdom"},
    {"keywords": "Frontend Developer", "location": "Switzerland"},

    # Middle East - Dubai / UAE
    {"keywords": "Frontend Developer", "location": "Dubai"},
    {"keywords": "React Developer", "location": "Dubai"},
    {"keywords": "Full Stack Developer", "location": "United Arab Emirates"},

    # Australia & New Zealand
    {"keywords": "Frontend Developer", "location": "Australia"},
    {"keywords": "React Developer", "location": "Australia"},
    {"keywords": "Full Stack Developer", "location": "Australia"},
    {"keywords": "Frontend Developer", "location": "New Zealand"},
    {"keywords": "Full Stack Developer", "location": "New Zealand"},
]

# Default max detail pages to scrape per run to avoid HTTP timeout.
# Set to None to scrape all.
DEFAULT_MAX_DETAILS = 25


# --------------------------------------------------
# Helpers
# --------------------------------------------------

def build_search_url(keywords, location):
    return (
        "https://www.linkedin.com/jobs/search/"
        f"?keywords={quote_plus(keywords)}"
        f"&location={quote_plus(location)}"
    )


def extract_job_id(url):
    match = re.search(r"/jobs/view/[^/?]+-(\d+)", url)
    if match:
        return match.group(1)
    return None


def parse_job_card(card, job_url):
    text = card.inner_text().strip()
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    if not lines:
        return None

    job_id = extract_job_id(job_url)
    if not job_id:
        return None

    # LinkedIn sometimes duplicates the title in the card.
    title = lines[0]
    if len(lines) > 1 and lines[1] == title:
        lines.pop(1)

    company = lines[1] if len(lines) > 1 else None
    location = lines[2] if len(lines) > 2 else None

    posted = None
    for line in lines:
        if (
            "ago" in line.lower()
            or "actively hiring" in line.lower()
            or "vor " in line.lower()
        ):
            posted = line
            break

    return {
        "job_id": job_id,
        "title": title,
        "company": company,
        "location": location,
        "posted": posted,
        "url": job_url,
    }


def extract_job_description(page):
    selector = ".show-more-less-html__markup"
    elements = page.locator(selector)

    if elements.count() == 0:
        return None

    return elements.first.inner_text().strip()


def scrape_job_details(page, job):
    print(f"  Fetching details: {job.get('title')} ({job.get('job_id')})")

    page.goto(
        job["url"],
        wait_until="domcontentloaded",
        timeout=25000,
    )

    page.wait_for_timeout(1000)

    description = extract_job_description(page)
    job["description"] = description

    return job


# --------------------------------------------------
# Search scraping
# --------------------------------------------------

def scrape_search(page, keywords, location):
    search_url = build_search_url(keywords, location)

    print()
    print("=" * 70)
    print(f"SEARCH: {keywords} | {location}")
    print(search_url)
    print("=" * 70)

    try:
        page.goto(
            search_url,
            wait_until="domcontentloaded",
            timeout=25000,
        )
        page.wait_for_timeout(1500)
    except Exception as e:
        print(f"  Warning: search navigation failed: {e}")
        return []

    job_links = page.locator('a[href*="/jobs/view/"]')
    count = job_links.count()

    print(f"Found {count} job links")

    jobs = []
    seen_ids = set()

    for i in range(count):
        link = job_links.nth(i)
        try:
            job_url = link.get_attribute("href")
            if not job_url:
                continue

            job_id = extract_job_id(job_url)
            if not job_id:
                continue

            # Deduplicate jobs within this search.
            if job_id in seen_ids:
                continue

            seen_ids.add(job_id)

            card = link.locator("xpath=ancestor::li[1]")
            if card.count() == 0:
                continue

            job = parse_job_card(card, job_url)
            if job:
                job["search_keywords"] = keywords
                job["search_location"] = location
                jobs.append(job)

        except Exception as e:
            print(f"  Could not parse job {i}: {e}")

    print(f"Extracted {len(jobs)} unique jobs")
    return jobs


# --------------------------------------------------
# Main scraper
# --------------------------------------------------

def scrape_jobs(
    searches: Optional[List[Dict[str, str]]] = None,
    max_details: Optional[int] = DEFAULT_MAX_DETAILS,
    headless: Optional[bool] = None,
    progress_callback: Optional[Callable[[Dict], None]] = None,
) -> List[Dict]:
    """Scrape LinkedIn jobs across target searches with incremental persistence.

    - Discovered job cards are immediately saved into SQLite.
    - Detail fetching skips jobs that already have descriptions or are student/intern roles.
    - Detail descriptions are committed to SQLite immediately upon retrieval.
    - Limits detail requests to `max_details` per run so it finishes within HTTP timeouts.
    """
    target_searches = searches or SEARCHES

    if headless is None:
        headless = os.environ.get("HEADLESS", "true").lower() != "false"

    profile = load_profile()
    exclude_seniority = profile.get("exclude_seniority", [])

    all_jobs = []
    seen_job_ids = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()

        # ------------------------------------------
        # 1. Search LinkedIn & Ingest Job Cards
        # ------------------------------------------
        for search_idx, search in enumerate(target_searches, start=1):
            if progress_callback:
                progress_callback({
                    "phase": "searching",
                    "current_search": search_idx,
                    "total_searches": len(target_searches),
                    "search": search,
                })

            jobs = scrape_search(
                page,
                search["keywords"],
                search["location"],
            )

            new_search_jobs = []
            for job in jobs:
                job_id = job["job_id"]
                if job_id not in seen_job_ids:
                    seen_job_ids.add(job_id)
                    all_jobs.append(job)
                    new_search_jobs.append(job)

            # Persist newly found job cards to SQLite immediately!
            if new_search_jobs:
                save_jobs(new_search_jobs)

        print()
        print("=" * 70)
        print(f"TOTAL UNIQUE JOBS FOUND: {len(all_jobs)}")
        print("=" * 70)

        # ------------------------------------------
        # 2. Smart Targeted Detail Fetching
        # ------------------------------------------
        # Find jobs that actually need descriptions
        jobs_needing_detail = []

        for job in all_jobs:
            jid = job["job_id"]
            db_record = get_job(jid)

            # If description is already in DB, reuse it
            if db_record and db_record.get("description"):
                job["description"] = db_record["description"]
                continue

            # Skip intern / student roles
            if detect_seniority(job.get("title", ""), exclude_seniority) == "intern":
                continue

            jobs_needing_detail.append(job)

        print(f"Jobs needing detail fetch: {len(jobs_needing_detail)}")

        if max_details is not None:
            jobs_to_scrape = jobs_needing_detail[:max_details]
            print(f"Fetching details for top {len(jobs_to_scrape)} jobs (max_details={max_details})")
        else:
            jobs_to_scrape = jobs_needing_detail

        details_fetched = 0
        for idx, job in enumerate(jobs_to_scrape, start=1):
            if progress_callback:
                progress_callback({
                    "phase": "details",
                    "current_job": idx,
                    "total_detail_jobs": len(jobs_to_scrape),
                    "job_id": job["job_id"],
                    "title": job.get("title"),
                })

            try:
                scrape_job_details(page, job)
                if job.get("description"):
                    # Immediately save description in SQLite
                    update_job_description(job["job_id"], job["description"])
                    details_fetched += 1
            except Exception as e:
                print(f"  Failed to fetch {job['job_id']}: {e}")
                job["description"] = None

        browser.close()

    print()
    print("=" * 70)
    print(f"SCRAPING COMPLETE — {len(all_jobs)} jobs indexed, {details_fetched} descriptions retrieved")
    print("=" * 70)

    return all_jobs


# --------------------------------------------------
# Entry point
# --------------------------------------------------

if __name__ == "__main__":
    jobs = scrape_jobs()

    for job in jobs[:10]:
        print(
            f"{job['job_id']} | "
            f"{job['title']} | "
            f"{job['company']}"
        )
