import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from app.db import get_job, save_tailored_docs
from app.filters import load_profile

BASE_DIR = Path(__file__).resolve().parent
BASE_RESUME_PATH = BASE_DIR / "data" / "base_resume.tex"
GENERATED_DIR = BASE_DIR / "data" / "generated"

_GENERATE_LOCK = threading.Lock()
_LAST_CALL_TIME = 0.0


def find_tectonic_bin() -> str:
    """Locate the tectonic compiler binary."""
    tectonic = shutil.which("tectonic")
    if tectonic:
        return tectonic

    for candidate in [
        "/opt/homebrew/bin/tectonic",
        "/usr/local/bin/tectonic",
        str(Path.home() / ".cargo" / "bin" / "tectonic"),
    ]:
        if Path(candidate).exists() and os.access(candidate, os.X_OK):
            return candidate

    return "tectonic"


def compile_latex(tex_path: Path, output_dir: Path) -> Path:
    """Compile a .tex file to PDF using tectonic."""
    tectonic_bin = find_tectonic_bin()
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        tectonic_bin,
        str(tex_path),
        "--outdir",
        str(output_dir),
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    expected_pdf = output_dir / (tex_path.stem + ".pdf")

    if result.returncode != 0 or not expected_pdf.exists():
        err_msg = result.stderr or result.stdout
        raise RuntimeError(f"LaTeX compilation failed: {err_msg}")

    return expected_pdf


def clean_llm_latex_output(raw_text: str) -> str:
    """Strip code fences, preamble markdown, or extra trailing markers from LLM output."""
    cleaned = raw_text.strip()

    # Remove markdown code blocks if present
    if cleaned.startswith("```latex"):
        cleaned = cleaned[8:]
    elif cleaned.startswith("```tex"):
        cleaned = cleaned[6:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]

    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]

    cleaned = cleaned.strip()

    # Normalize unicode em-dashes and en-dashes for LaTeX T1 fontenc compatibility
    cleaned = cleaned.replace("—", "---").replace("–", "--")

    # Auto-escape unescaped ampersands (e.g. "Cloud & DevOps", "AI & Tooling")
    cleaned = re.sub(r'(?<!\\)&(?=[ \t\w])', r'\\&', cleaned)

    # Ensure it starts at \documentclass
    doc_start = cleaned.find("\\documentclass")
    if doc_start != -1:
        cleaned = cleaned[doc_start:]

    # Ensure it ends at \end{document}
    doc_end = cleaned.rfind("\\end{document}")
    if doc_end != -1:
        cleaned = cleaned[: doc_end + len("\\end{document}")]

    return cleaned.strip()


def build_cover_letter_latex(
    candidate_name: str,
    candidate_email: str,
    candidate_location: str,
    company: str,
    title: str,
    body_paragraphs: str,
) -> str:
    """Generate a clean, matching LaTeX template for the cover letter."""
    safe_body = (
        body_paragraphs.replace("&", "\\&")
        .replace("%", "\\%")
        .replace("$", "\\$")
        .replace("_", "\\_")
        .replace("#", "\\#")
    )

    return rf"""\documentclass[11pt,a4paper]{{article}}
\usepackage[margin=0.85in]{{geometry}}
\usepackage{{hyperref}}
\usepackage[T1]{{fontenc}}
\usepackage{{lmodern}}
\usepackage{{parskip}}

\setlength{{\parindent}}{{0pt}}
\setlength{{\parskip}}{{8pt}}

\begin{{document}}

{{\LARGE\textbf{{{candidate_name}}}}}
\par
\vspace{{2pt}}
{candidate_location} \quad \href{{mailto:{candidate_email}}}{{{candidate_email}}}
\par
\vspace{{10pt}}
\hrule
\vspace{{15pt}}

\textbf{{Date:}} \today \\
\textbf{{To:}} Hiring Team at {company} \\
\textbf{{Subject:}} Application for {title}

\vspace{{10pt}}

{safe_body}

\vspace{{15pt}}
Sincerely, \\
\textbf{{{candidate_name}}}

\end{{document}}
"""


def generate_with_fallback(client, contents: str):
    """Generate content with automatic fallback, concurrency serialization, and exponential backoff for rate limits."""
    global _LAST_CALL_TIME

    candidate_models = [
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
    ]
    last_err = None

    with _GENERATE_LOCK:
        # Enforce minimum 1.5s spacing between calls to stay under Gemini RPM limits
        elapsed = time.time() - _LAST_CALL_TIME
        if elapsed < 1.5:
            time.sleep(1.5 - elapsed)

        for attempt in range(4):
            for model_name in candidate_models:
                try:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=contents,
                    )
                    _LAST_CALL_TIME = time.time()
                    return response
                except Exception as e:
                    last_err = e
                    err_str = str(e).lower()
                    if "429" in err_str or "exhausted" in err_str or "503" in err_str or "unavailable" in err_str:
                        backoff = 3.0 * (attempt + 1)
                        print(f"Rate limited ({e}). Backing off for {backoff:.1f}s before retry...")
                        time.sleep(backoff)
                    else:
                        time.sleep(1.0)
                    continue

    if last_err:
        raise last_err
    raise RuntimeError("No model candidates available for generation")


def tailor_documents_for_job(job_id: str, api_key: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
    """Tailor the candidate's LaTeX resume and generate a cover letter for a specific job."""
    clean_id = str(job_id).strip().strip('"').strip("'") if job_id is not None else ""
    job = get_job(clean_id)
    if not job:
        raise ValueError(f"Job ID '{job_id}' not found in database")

    # If already generated and files exist on disk, return cached result immediately
    if not force and job.get("resume_path") and job.get("cover_letter_path"):
        r_path = Path(job["resume_path"])
        cl_path = Path(job["cover_letter_path"])
        if r_path.exists() and cl_path.exists():
            return {
                "job_id": clean_id,
                "title": job.get("title"),
                "company": job.get("company"),
                "location": job.get("location"),
                "url": job.get("url"),
                "score": job.get("score"),
                "tech_stack": job.get("tech_stack"),
                "resume_pdf": str(r_path),
                "cover_letter_pdf": str(cl_path),
                "status": "ready_to_apply",
                "cached": True,
            }

    profile = load_profile()

    if not BASE_RESUME_PATH.exists():
        raise FileNotFoundError(f"Base resume not found at {BASE_RESUME_PATH}")

    base_resume_latex = BASE_RESUME_PATH.read_text(encoding="utf-8")

    # Determine Gemini API Key
    effective_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not effective_key:
        for env_candidate in [
            BASE_DIR.parent / ".env",
            BASE_DIR.parent.parent / ".env",
        ]:
            if env_candidate.exists():
                for line in env_candidate.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("GEMINI_API_KEY="):
                        effective_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            if effective_key:
                break

    if not effective_key:
        raise ValueError(
            "GEMINI_API_KEY is not set. Please set it in your environment, "
            "add it to scraper/.env, or pass it in the request."
        )

    from google import genai

    client = genai.Client(api_key=effective_key)

    job_title = job.get("title") or "Software Engineer"
    job_company = job.get("company") or "Hiring Company"
    job_location = job.get("location") or "Target Location"
    job_desc = job.get("description") or "Frontend / Full Stack Engineering role."

    # ----------------------------------------------------
    # Unified Single-Prompt Generation
    # ----------------------------------------------------
    unified_prompt = f"""You are an elite executive tech recruiter and ATS resume optimization expert.
You have the candidate's verified base resume in LaTeX below.
Your task is to tailor the LaTeX resume AND write a custom tailored cover letter for this job opening in a SINGLE response.

JOB DETAILS:
Title: {job_title}
Company: {job_company}
Location: {job_location}
Job Description:
{job_desc[:5000]}

CANDIDATE BASE RESUME (LaTeX):
{base_resume_latex}

CRITICAL RULES:
1. PRESERVE COMPILATION INTEGRITY: Keep the exact LaTeX document structure, preamble, package imports, margins, fontenc, and custom commands (\\sectiontitle, \\experience, \\clientproject) intact so it compiles flawlessly.
2. ABSOLUTE TRUTHFULNESS: Never invent companies, dates, degrees, or fabricate fake jobs. Keep all company names (Fractal Analytics, Dragonfruit, Avataar, Holosuit), dates, and client projects (Mercedes-Benz, Mondelez, Pepperfry, Samsung) factual.
3. TAILOR THE SUMMARY: Rewrite the \\sectiontitle{{Summary}} to position the candidate strongly for THIS specific role and company, emphasizing relevant 6+ years of full-stack engineering, performance, security, and the tech stack requested.
4. OPTIMIZE THE SKILLS SECTION: Reorder and front-load the specific technologies and tools requested in the JD (e.g. React, TypeScript, Next.js, FastAPI, Python, Docker, Redis, etc.) under the existing categories.
5. REFINE EXPERIENCE BULLETS: In \\sectiontitle{{Experience}}, selectively emphasize, reword, or bring forward bullet points that demonstrate accomplishments directly matching the requirements in the job description (e.g. high performance UI, analytics, API design, security, caching, testing).
6. COVER LETTER: A compelling, persuasive 3-to-4 paragraph letter addressed to Hiring Team at {job_company}. Connect past achievements to the challenges described in the JD.

OUTPUT FORMAT SPECIFICATION:
Your response MUST contain exactly two sections separated by delimiters:
===RESUME_LATEX===
\\documentclass[10.5pt,a4paper]{{article}}
... (tailored LaTeX code) ...
\\end{{document}}
===COVER_LETTER===
(3-4 plain-text paragraphs for the cover letter)
"""

    response = generate_with_fallback(client, unified_prompt)
    full_output = response.text or ""

    if "===COVER_LETTER===" in full_output:
        parts = full_output.split("===COVER_LETTER===", 1)
        resume_raw = parts[0].replace("===RESUME_LATEX===", "")
        cover_letter_text = parts[1].strip()
    else:
        resume_raw = full_output
        cover_letter_text = (
            f"Dear Hiring Team at {job_company},\n\n"
            f"I am writing to express my enthusiastic interest in the {job_title} role at {job_company}. "
            f"With over 6 years of experience building high-performance web applications and analytics platforms "
            f"using React, TypeScript, and Python, I am confident in my ability to deliver immediate value to your team.\n\n"
            f"Throughout my career at Fractal Analytics, Dragonfruit, and Avataar, I have led end-to-end development of "
            f"scalable, secure applications for enterprise clients including Mercedes-Benz and US retailers. "
            f"I look forward to discussing how my background aligns with your engineering goals."
        )

    tailored_latex = clean_llm_latex_output(resume_raw)

    # ----------------------------------------------------
    # Compile PDFs
    # ----------------------------------------------------
    job_dir = GENERATED_DIR / str(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    tex_file = job_dir / "resume.tex"
    tex_file.write_text(tailored_latex, encoding="utf-8")

    # Compile Resume PDF
    try:
        resume_pdf = compile_latex(tex_file, job_dir)
    except Exception as e:
        print(f"Tailored compilation failed ({e}), falling back to base resume.")
        tex_file.write_text(base_resume_latex, encoding="utf-8")
        resume_pdf = compile_latex(tex_file, job_dir)

    # Save Cover Letter text
    cl_text_file = job_dir / "cover_letter.txt"
    cl_text_file.write_text(cover_letter_text, encoding="utf-8")

    # Build & Compile Cover Letter PDF
    cl_latex = build_cover_letter_latex(
        candidate_name=profile.get("name", "Ritobrita De"),
        candidate_email=profile.get("email", "deritobrita@gmail.com"),
        candidate_location=profile.get("location", "Bangalore, India"),
        company=job_company,
        title=job_title,
        body_paragraphs=cover_letter_text,
    )
    cl_tex_file = job_dir / "cover_letter.tex"
    cl_tex_file.write_text(cl_latex, encoding="utf-8")

    try:
        cl_pdf = compile_latex(cl_tex_file, job_dir)
        cl_pdf_path = str(cl_pdf)
    except Exception:
        cl_pdf_path = str(cl_text_file)

    # Save paths to DB
    save_tailored_docs(job_id, str(resume_pdf), cl_pdf_path)

    return {
        "job_id": job_id,
        "title": job_title,
        "company": job_company,
        "location": job.get("location"),
        "url": job.get("url"),
        "score": job.get("score"),
        "tech_stack": job.get("tech_stack"),
        "resume_pdf": str(resume_pdf),
        "resume_tex": str(tex_file),
        "cover_letter_pdf": cl_pdf_path,
        "cover_letter_text": str(cl_text_file),
        "status": "ready_to_apply",
    }
