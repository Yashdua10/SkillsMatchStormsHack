#!/usr/bin/env python3
"""Local SkillMatch prototype API backed by SQLite (Python standard library only)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("SKILLMATCH_DB", ROOT / "skillmatch.sqlite3"))
RESUME_DIR = ROOT / "uploads" / "resumes"
SESSION_SECONDS = 60 * 60 * 24 * 7
PBKDF2_ROUNDS = 310_000
CODING_EXAM_SECONDS = 20 * 60


def grade_python_submission(source):
    """Grade the Python task in a constrained child process."""
    if not isinstance(source, str) or not source.strip() or len(source) > 12_000:
        raise ValueError("Enter a solution under 12,000 characters.")
    worker = r'''import ast, json, math, resource, sys
def lower_limit(which, value):
 _, hard = resource.getrlimit(which)
 resource.setrlimit(which, (min(value, hard), hard))
lower_limit(resource.RLIMIT_CPU, 1)
tree = ast.parse(json.loads(sys.stdin.read()), mode="exec")
allowed = (ast.Module,ast.FunctionDef,ast.arguments,ast.arg,ast.Return,ast.Assign,ast.AugAssign,ast.For,ast.If,ast.Expr,ast.Pass,ast.Name,ast.Load,ast.Store,ast.Constant,ast.List,ast.Tuple,ast.Dict,ast.Subscript,ast.BinOp,ast.UnaryOp,ast.Compare,ast.BoolOp,ast.IfExp,ast.Call,ast.keyword,ast.Add,ast.Sub,ast.Mult,ast.Div,ast.FloorDiv,ast.Mod,ast.USub,ast.UAdd,ast.Not,ast.Eq,ast.NotEq,ast.Lt,ast.LtE,ast.Gt,ast.GtE,ast.In,ast.NotIn,ast.And,ast.Or,ast.ListComp,ast.comprehension)
if len(tree.body)!=1 or not isinstance(tree.body[0],ast.FunctionDef) or tree.body[0].name!="total_sales": raise ValueError("Define exactly one function named total_sales(orders).")
if len(tree.body[0].args.args)!=1 or tree.body[0].args.args[0].arg!="orders": raise ValueError("The function must take one parameter named orders.")
for n in ast.walk(tree):
 if not isinstance(n,allowed): raise ValueError("Use basic Python statements only; imports and attribute access are disabled.")
 if isinstance(n,ast.Name) and n.id.startswith("__"): raise ValueError("Double underscore names are not allowed.")
 if isinstance(n,ast.Constant) and isinstance(n.value,int) and abs(n.value)>1000000: raise ValueError("Numeric literals must be at most 1,000,000.")
 if isinstance(n,ast.Call) and (not isinstance(n.func,ast.Name) or n.func.id not in {"round","len","sum","min","max","range","enumerate","abs","int","float","list","dict","sorted"}): raise ValueError("Only basic numeric and collection helpers can be called.")
def limited_range(*a):
 r=range(*a)
 if len(r)>20000: raise ValueError("Loop is too large.")
 return r
b={"round":round,"len":len,"sum":sum,"min":min,"max":max,"range":limited_range,"enumerate":enumerate,"abs":abs,"int":int,"float":float,"list":list,"dict":dict,"sorted":sorted}
env={"__builtins__":b}; exec(compile(tree,"<exam>","exec"),env,env); fn=env["total_sales"]
cases=[([],0.0),([{"quantity":2,"unit_price":3.5}],7.0),([{"quantity":2,"unit_price":4},{"quantity":3,"unit_price":1.5}],12.5),([{"quantity":1,"unit_price":0.1},{"quantity":3,"unit_price":0.2}],0.7),([{"quantity":0,"unit_price":88}],0.0)]
passed=0
for orders,expected in cases:
 try:
  actual=fn(orders)
  if isinstance(actual,(int,float)) and math.isfinite(actual) and round(actual,2)==expected: passed+=1
 except Exception: pass
print(json.dumps({"passed":passed,"total":len(cases)}))
'''
    try:
        result = subprocess.run([sys.executable, "-I", "-S", "-c", worker], input=json.dumps(source),
                                capture_output=True, text=True, timeout=2, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Your solution exceeded the execution limit.") from exc
    if result.returncode:
        lines = (result.stderr or "").strip().splitlines()
        raise ValueError((lines[-1] if lines else "The solution could not be run.")[:240])
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise ValueError("The solution did not finish cleanly.") from exc


def review_coding_attempt(source, passed, signals):
    """Optional consent-gated review; it never changes the test score."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key: return None
    model = os.environ.get("SKILLMATCH_EXAM_AI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
    payload = {"model":model,"temperature":0,"max_tokens":180,"response_format":{"type":"json_object"},"messages":[
        {"role":"system","content":"Review a coding exam submission and coarse session signals. Return JSON with feedback (one short constructive sentence), reviewRequired (boolean), and reason (short). Focus changes and paste events are non-conclusive signals, never proof of cheating. Never change or infer the deterministic test score."},
        {"role":"user","content":json.dumps({"task":"Implement total_sales(orders): sum quantity * unit_price, rounded to 2 decimals.","code":source[:12000],"testsPassed":passed,"sessionSignals":signals})}]}
    request = Request("https://api.openai.com/v1/chat/completions", data=json.dumps(payload).encode(), headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"}, method="POST")
    try:
        with urlopen(request, timeout=12) as response: answer=json.loads(response.read().decode())
        result=json.loads(answer["choices"][0]["message"]["content"])
        return {"feedback":str(result.get("feedback",""))[:240],"reviewRequired":bool(result.get("reviewRequired",False)),"reason":str(result.get("reason",""))[:160]}
    except Exception:
        return {"feedback":"AI feedback is unavailable; any exam flag deductions still follow the published rules.","reviewRequired":False,"reason":"AI review unavailable"}


def review_proctor_frame(data_url):
    """Classify a single transient exam frame; never identify a person or infer intent."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    model = os.environ.get("SKILLMATCH_PROCTOR_MODEL", "gpt-4.1").strip() or "gpt-4.1"
    payload = {"model": model, "temperature": 0, "max_completion_tokens": 100,
               "response_format": {"type": "json_object"}, "messages": [
        {"role": "system", "content": "First determine whether a human is visible in the frame. If no human head or upper body is visible, return event person_absent. Do not return none for an empty chair, empty room, or camera pointed away from the test taker. If a person is present, choose multiple_people if more than one person is clearly visible, phone_visible if a phone is clearly visible, or gaze_away if the visible head is clearly turned away from the screen. Return none only when one person is clearly visible and none of those conditions apply. Return uncertain only when image quality prevents this classification. Do not identify anyone or infer that cheating occurred. Return JSON with event and a brief neutral note."},
        {"role": "user", "content": [{"type": "text", "text": "Presence check is highest priority: is a person visibly in this frame? If not, event must be person_absent. Then check the other listed visual conditions. Do not infer intent."},
                                         {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}}]}]}
    request = Request("https://api.openai.com/v1/chat/completions", data=json.dumps(payload).encode(),
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=12) as response:
            answer = json.loads(response.read().decode())
        result = json.loads(answer["choices"][0]["message"]["content"])
        raw_event = str(result.get("event", "")).strip().lower().replace("-", "_").replace(" ", "_")
        note = str(result.get("note", ""))[:120]
        aliases = {
            "no_person": "person_absent", "no_person_visible": "person_absent",
            "person_not_visible": "person_absent", "no_one_visible": "person_absent",
            "no_human_visible": "person_absent", "nobody_visible": "person_absent",
            "absent": "person_absent", "person_missing": "person_absent",
            "multiple_persons": "multiple_people", "phone": "phone_visible",
            "looking_away": "gaze_away", "head_turned_away": "gaze_away",
        }
        event = aliases.get(raw_event, raw_event)
        if event == "none" and re.search(r"\b(no person|no people|no human|nobody|no one in frame|empty room|empty chair|camera pointed away)\b", note, re.I):
            event = "person_absent"
        if event not in {"person_absent", "multiple_people", "phone_visible", "gaze_away", "none", "uncertain"}:
            event = "uncertain"
        return {"event": event, "note": note}
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        detail = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", str(detail))[:110]
        reasons = {401: "API key rejected", 403: "model access denied", 404: "vision model unavailable", 429: "API rate limit or credits exhausted"}
        reason = reasons.get(exc.code, f"API request failed ({exc.code})")
        return {"event": "uncertain", "note": f"{reason}{': ' + detail if detail else '; check API key/model settings.'}"}
    except (TimeoutError, URLError) as exc:
        reason = getattr(exc, "reason", "")
        detail = str(reason)[:80] if reason else "request timed out"
        return {"event": "uncertain", "note": f"Vision request failed: {detail}"}
    except (KeyError, ValueError, TypeError):
        return {"event": "uncertain", "note": "Vision model returned an unreadable response."}
    except Exception as exc:
        print(f"[proctor] frame review failed: {type(exc).__name__}", file=sys.stderr)
        return {"event": "uncertain", "note": "Vision check failed; see the server terminal for the error type."}

SKILL_ALIASES = {
    "javascript": {"javascript", "js"},
    "data analysis": {"data analysis", "data analytics", "analytics"},
    "communication": {"communication", "written communication", "verbal communication"},
    "python": {"python", "python programming", "python fundamentals"},
    "figma": {"figma", "figma design"},
}


def clean_text(value):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#. ]", " ", str(value or "").casefold())).strip()


def split_skills(value):
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = re.split(r"[,;\n]+", str(value or ""))
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def skill_key(value):
    normalized = clean_text(value)
    for canonical, aliases in SKILL_ALIASES.items():
        if normalized in aliases:
            return canonical
    return normalized


def has_role_terms(text, terms):
    haystack = f" {clean_text(text)} "
    for term in terms:
        needle = clean_text(term)
        if needle and f" {needle} " in haystack:
            return True
    return False


def calculate_match(role, profile, assessments, semantic_score=None):
    """Score a student against a role; all component scores are 0..100."""
    required = split_skills(role.get("mustHaveSkills", ""))
    preferred = split_skills(role.get("niceToHaveSkills", ""))
    claimed = {skill_key(skill) for skill in split_skills(profile.get("skills", []))}
    assessed = {skill_key(skill): int(score) for skill, score in assessments.items()}

    def skill_result(skill):
        key = skill_key(skill)
        if key in assessed:
            value = max(0, min(100, assessed[key]))
            return {"skill": skill, "score": value, "evidence": "assessment", "verified": value >= 67}
        if key in claimed:
            return {"skill": skill, "score": 60, "evidence": "student profile", "verified": False}
        return {"skill": skill, "score": 0, "evidence": "missing", "verified": False}

    components = []
    required_results = [skill_result(skill) for skill in required]
    preferred_results = [skill_result(skill) for skill in preferred]
    missing_required = [item["skill"] for item in required_results if item["score"] < 60]
    matched = list(dict.fromkeys(item["skill"] for item in required_results + preferred_results if item["score"] >= 60))
    relevant_assessments = {skill: int(score) for skill, score in assessments.items()
                            if skill_key(skill) in {skill_key(x) for x in required + preferred}}
    if required_results:
        components.append({"key": "requiredSkills", "label": "Must-have skills", "weight": 50,
                           "score": round(sum(item["score"] for item in required_results) / len(required_results)),
                           "detail": f"{sum(item['score'] > 0 for item in required_results)} of {len(required_results)} listed"})
    if preferred_results:
        components.append({"key": "preferredSkills", "label": "Preferred skills", "weight": 15,
                           "score": round(sum(item["score"] for item in preferred_results) / len(preferred_results)),
                           "detail": f"{sum(item['score'] > 0 for item in preferred_results)} of {len(preferred_results)} listed"})

    education_requirement = str(role.get("education", "")).strip()
    education_level = clean_text(profile.get("educationLevel", ""))
    school = clean_text(profile.get("school", ""))
    area = clean_text(profile.get("area", ""))
    education_score = None
    education_detail = "No education requirement"
    if education_requirement and "no specific" not in clean_text(education_requirement):
        req = clean_text(education_requirement)
        if "currently enrolled" in req:
            education_score = 100 if school else 35
        elif "bachelor" in req:
            education_score = 100 if "bachelor" in education_level or "master" in education_level or "phd" in education_level else (50 if school else 0)
        elif "graduate" in req:
            education_score = 100 if "master" in education_level or "phd" in education_level else (35 if "bachelor" in education_level else 0)
        elif "coursework" in req or "equivalent" in req:
            education_score = 100 if school and (area or profile.get("projectTitle") or profile.get("experienceTitle")) else (50 if school else 0)
        elif "college diploma" in req or "diploma" in req:
            education_score = 100 if "diploma" in education_level else (45 if school else 0)
        else:
            education_score = 100 if req in education_level else (50 if school else 0)
        education_detail = f"{education_score}% alignment"
        components.append({"key": "education", "label": "Education", "weight": 10,
                           "score": education_score, "detail": education_detail})

    experience_requirement = str(role.get("experience", "")).strip()
    experience_score = None
    experience_detail = "No experience requirement"
    req_exp = clean_text(experience_requirement)
    if experience_requirement and "open to all" not in req_exp and "no prior" not in req_exp:
        has_experience = bool(profile.get("experienceTitle") or profile.get("experienceSummary"))
        has_project = bool(profile.get("projectTitle") or profile.get("projectUrl"))
        if "projects" in req_exp or "coursework" in req_exp:
            experience_score = 100 if has_experience or has_project else 20
        elif "2+" in req_exp:
            experience_score = 65 if has_experience else (30 if has_project else 0)
        elif "1+" in req_exp:
            experience_score = 75 if has_experience else (35 if has_project else 0)
        else:
            experience_score = 100 if has_experience else (40 if has_project else 0)
        experience_detail = f"{experience_score}% profile evidence"
        components.append({"key": "experience", "label": "Experience", "weight": 10,
                           "score": experience_score, "detail": experience_detail})

    stop_words = {"with", "from", "that", "this", "will", "work", "team", "role", "the", "and", "for", "our", "you", "your", "into", "have", "has", "are", "who", "can", "about", "other", "requirements", "intern", "internship"}
    role_terms = list(dict.fromkeys(term for key in ("roleTitle", "team", "description", "otherRequirements")
                                   for term in re.findall(r"[a-z0-9+#.]+", clean_text(role.get(key, "")))
                                   if len(term) > 3 and term not in stop_words))
    candidate_context = " ".join(str(profile.get(key, "")) for key in
                                  ("interest", "area", "goal", "experienceTitle", "experienceSummary", "projectTitle", "projectDescription"))
    context_hits = [term for term in role_terms if len(clean_text(term)) > 2 and has_role_terms(candidate_context, [term])]
    relevance_score = min(100, round(35 + 25 * len(context_hits))) if context_hits else (15 if candidate_context.strip() else 0)
    components.append({"key": "roleRelevance", "label": "Role relevance", "weight": 10,
                       "score": relevance_score,
                       "detail": f"{len(context_hits)} relevant profile signals" if context_hits else "No role-specific signal found"})

    has_project = bool(profile.get("projectTitle") and profile.get("projectUrl"))
    has_portfolio = bool(profile.get("portfolio"))
    has_work = bool(profile.get("experienceTitle") and (profile.get("experienceSummary") or profile.get("experienceTitle")))
    evidence_score = min(100, (45 if has_project else 0) + (25 if has_portfolio else 0) + (30 if has_work else 0))
    components.append({"key": "evidence", "label": "Project and work evidence", "weight": 5,
                       "score": evidence_score,
                       "detail": ", ".join(label for label, present in (("project", has_project), ("portfolio", has_portfolio), ("work history", has_work)) if present) or "No links or work history"})

    if semantic_score is not None:
        components.append({"key": "semanticFit", "label": "AI semantic fit", "weight": 15,
                           "score": max(0, min(100, int(semantic_score))),
                           "detail": "Relatedness of the role to opted-in profile text; not skill verification"})

    weight_total = sum(item["weight"] for item in components)
    score = round(sum(item["score"] * item["weight"] for item in components) / weight_total) if weight_total else 0
    reasons = [f"{item['label']}: {item['detail']}" for item in sorted(components, key=lambda part: part["score"] * part["weight"], reverse=True)
               if item["score"] >= 60][:2]
    if not reasons:
        reasons = ["Profile evidence is limited; add skills, projects, or experience to improve this match."]
    return {
        "score": score,
        "aiEnhanced": semantic_score is not None,
        "matchedSkills": matched,
        "missingRequiredSkills": missing_required,
        "skillDetails": required_results + preferred_results,
        "matchReasons": reasons,
        "scoreBreakdown": components,
        "assessments": relevant_assessments,
    }


def matching_role_text(role):
    return "\n".join(f"{label}: {str(role.get(key, '')).strip()[:1200]}"
                     for label, key in (("Role", "roleTitle"), ("Team", "team"), ("Description", "description"),
                                        ("Must-have skills", "mustHaveSkills"), ("Preferred skills", "niceToHaveSkills"),
                                        ("Other requirements", "otherRequirements")) if role.get(key))


def matching_profile_text(profile):
    # Deliberately omit names, email addresses, school names, and resume contents.
    fields = [
        ("Skills", ", ".join(split_skills(profile.get("skills", [])))),
        ("Interests", profile.get("interest", "")),
        ("Career goal", profile.get("goal", "")),
        ("Project", " ".join(str(profile.get(key, "")) for key in ("projectTitle", "projectDescription"))),
        ("Experience", " ".join(str(profile.get(key, "")) for key in ("experienceTitle", "experienceSummary"))),
    ]
    return "\n".join(f"{label}: {str(value).strip()[:1800]}" for label, value in fields if str(value).strip())


def semantic_match_scores(db, pairs):
    """Return cached OpenAI embedding similarities for explicitly opted-in profiles."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {}, "AI matching is off. Set OPENAI_API_KEY on the server to enable it."
    consented = [(key, role, profile) for key, role, profile in pairs if profile.get("shareWithAiMatching", False)]
    if not consented:
        return {}, "AI matching is ready; students can opt in from their dashboard."
    model = os.environ.get("SKILLMATCH_AI_MODEL", "text-embedding-3-small").strip() or "text-embedding-3-small"
    pair_texts = {}
    all_texts = []
    for pair_key, role, profile in consented:
        role_text = matching_role_text(role)
        profile_text = matching_profile_text(profile)
        if not role_text or not profile_text:
            continue
        pair_texts[pair_key] = (role_text, profile_text)
        all_texts.extend((role_text, profile_text))
    if not pair_texts:
        return {}, "AI matching is ready; opted-in profiles need more role or profile details."

    unique_texts = list(dict.fromkeys(all_texts))
    def cache_key(text):
        return hashlib.sha256(f"{model}\0{text}".encode("utf-8")).hexdigest()
    keys_by_text = {text: cache_key(text) for text in unique_texts}
    cached = {}
    for text in unique_texts:
        row = db.execute("SELECT vector_json FROM ai_embedding_cache WHERE cache_key=?", (keys_by_text[text],)).fetchone()
        if row:
            try:
                cached[text] = json.loads(row["vector_json"])
            except (TypeError, json.JSONDecodeError):
                pass
    missing = [text for text in unique_texts if text not in cached]
    if missing:
        body = json.dumps({"model": model, "input": missing, "encoding_format": "float"}).encode("utf-8")
        request = Request("https://api.openai.com/v1/embeddings", data=body,
                          headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                          method="POST")
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            for item in payload.get("data", []):
                index = item.get("index")
                vector = item.get("embedding")
                if isinstance(index, int) and 0 <= index < len(missing) and isinstance(vector, list) and vector:
                    cached[missing[index]] = vector
                    db.execute("INSERT OR REPLACE INTO ai_embedding_cache(cache_key,model,vector_json,updated_at) VALUES(?,?,?,?)",
                               (keys_by_text[missing[index]], model, json.dumps(vector, separators=(",", ":")), int(time.time())))
        except Exception:
            # Keep role matching available when the network, key, or provider is unavailable.
            return {}, "AI matching is temporarily unavailable; showing rules-based scores."

    scores = {}
    for pair_key, (role_text, profile_text) in pair_texts.items():
        role_vector, profile_vector = cached.get(role_text), cached.get(profile_text)
        if not role_vector or not profile_vector or len(role_vector) != len(profile_vector):
            continue
        cosine = sum(left * right for left, right in zip(role_vector, profile_vector))
        scores[pair_key] = max(0, min(100, round(cosine * 100)))
    if scores:
        return scores, "AI semantic fit is included for opted-in profiles; must-have eligibility remains rules-based."
    return {}, "AI matching is temporarily unavailable; showing rules-based scores."


def connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db():
    with connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                role TEXT NOT NULL CHECK(role IN ('student','employer')),
                password_salt BLOB NOT NULL,
                password_hash BLOB NOT NULL,
                company_name TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                profile_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ai_embedding_cache (
                cache_key TEXT PRIMARY KEY,
                model TEXT NOT NULL,
                vector_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS roles (
                id INTEGER PRIMARY KEY,
                employer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS assessments (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                skill TEXT NOT NULL,
                score INTEGER NOT NULL,
                total INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(user_id, skill)
            );
            CREATE TABLE IF NOT EXISTS code_assessment_attempts (
                attempt_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                started_at INTEGER NOT NULL,
                finished_at INTEGER,
                score INTEGER,
                raw_score INTEGER,
                penalty_points INTEGER NOT NULL DEFAULT 0,
                proctor_frames INTEGER NOT NULL DEFAULT 0,
                proctor_flags INTEGER NOT NULL DEFAULT 0,
                proctor_uncertain_frames INTEGER NOT NULL DEFAULT 0,
                proctor_last_frame_at INTEGER,
                proctor_summary TEXT NOT NULL DEFAULT '',
                proctor_issue TEXT NOT NULL DEFAULT '',
                proctor_share_consent INTEGER NOT NULL DEFAULT 0,
                live_proctor_consent INTEGER NOT NULL DEFAULT 0,
                ai_review_consent INTEGER NOT NULL DEFAULT 0,
                focus_loss_count INTEGER NOT NULL DEFAULT 0,
                hidden_tab_count INTEGER NOT NULL DEFAULT 0,
                paste_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash BLOB PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY,
                role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'submitted',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(role_id, student_id)
            );
            CREATE TABLE IF NOT EXISTS hidden_applications (
                role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                hidden_at INTEGER NOT NULL,
                PRIMARY KEY(role_id, student_id)
            );
            CREATE TABLE IF NOT EXISTS shortlists (
                role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                employer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at INTEGER NOT NULL,
                PRIMARY KEY(role_id, student_id)
            );
            CREATE TABLE IF NOT EXISTS dismissed_candidates (
                role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                employer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at INTEGER NOT NULL,
                PRIMARY KEY(role_id, student_id)
            );
            CREATE TABLE IF NOT EXISTS invitations (
                role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                employer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at INTEGER NOT NULL,
                PRIMARY KEY(role_id, student_id)
            );
            CREATE TABLE IF NOT EXISTS profile_views (
                id INTEGER PRIMARY KEY,
                student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                employer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                viewed_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY,
                role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                employer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                body TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversation_reads (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
                student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                last_read_message_id INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(user_id, role_id, student_id)
            );
            CREATE INDEX IF NOT EXISTS profile_views_student_idx ON profile_views(student_id, employer_id);
            CREATE INDEX IF NOT EXISTS messages_thread_idx ON messages(role_id, student_id, created_at);
        """)
        for column, definition in (("raw_score", "INTEGER"),
                                   ("penalty_points", "INTEGER NOT NULL DEFAULT 0"),
                                   ("proctor_uncertain_frames", "INTEGER NOT NULL DEFAULT 0"),
                                   ("proctor_frames", "INTEGER NOT NULL DEFAULT 0"),
                                   ("proctor_flags", "INTEGER NOT NULL DEFAULT 0"),
                                   ("proctor_last_frame_at", "INTEGER"),
                                   ("proctor_summary", "TEXT NOT NULL DEFAULT ''"),
                                   ("proctor_issue", "TEXT NOT NULL DEFAULT ''"),
                                   ("proctor_share_consent", "INTEGER NOT NULL DEFAULT 0"),
                                   ("live_proctor_consent", "INTEGER NOT NULL DEFAULT 0"),
                                   ("ai_review_consent", "INTEGER NOT NULL DEFAULT 0"),
                                   ("focus_loss_count", "INTEGER NOT NULL DEFAULT 0"),
                                   ("hidden_tab_count", "INTEGER NOT NULL DEFAULT 0"),
                                   ("paste_count", "INTEGER NOT NULL DEFAULT 0")):
            try:
                db.execute(f"ALTER TABLE code_assessment_attempts ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise


def seed_demo_data():
    """Add a small, realistic demo workspace once to a fresh database."""
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if db.execute("SELECT 1 FROM app_meta WHERE key='demo_seed_v1'").fetchone():
            demo_profiles = db.execute("SELECT u.email,p.user_id,p.profile_json FROM users u JOIN profiles p ON p.user_id=u.id WHERE u.email LIKE '%@skillmatch.demo'").fetchall()
            for row in demo_profiles:
                profile = json.loads(row["profile_json"])
                if "shareWithPeers" not in profile or "shareWithAiMatching" not in profile:
                    profile.setdefault("shareWithPeers", True)
                    profile.setdefault("shareWithAiMatching", True)
                    db.execute("UPDATE profiles SET profile_json=?,updated_at=? WHERE user_id=?", (json.dumps(profile), int(time.time()), row["user_id"]))
            return

        now = int(time.time())
        password = "SkillMatchDemo!"

        def add_user(email, role, company_name="", profile=None):
            salt = secrets.token_bytes(16)
            password_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
            cursor = db.execute(
                "INSERT INTO users(email,role,password_salt,password_hash,company_name,created_at) VALUES(?,?,?,?,?,?)",
                (email, role, salt, password_hash, company_name, now),
            )
            user_id = cursor.lastrowid
            if role == "student":
                db.execute("INSERT INTO profiles(user_id,profile_json,updated_at) VALUES(?,?,?)",
                           (user_id, json.dumps(profile or {}), now))
            return user_id

        employer_id = add_user("employer@skillmatch.demo", "employer", "Northstar Labs")
        profiles = [
            ("jamie@skillmatch.demo", {
                "name": "Jamie Chen", "school": "Simon Fraser University", "year": "3rd year",
                "educationLevel": "Bachelor of Science", "area": "Computing Science",
                "interest": "Backend engineering internship", "goal": "Build reliable backend services",
                "skills": ["Python", "JavaScript", "SQL", "Git", "Communication"],
                "projectTitle": "CampusConnect", "projectUrl": "https://github.com/topics/student-platforms",
                "projectDescription": "A student events app built with React and Python.",
                "experienceTitle": "Peer Tutor · SFU Computing Science",
                "experienceSummary": "Helped first-year students learn Python and debugging.",
                "shareWithEmployers": True, "shareWithPeers": True,
            }),
            ("alex@skillmatch.demo", {
                "name": "Alex Rivera", "school": "University of British Columbia", "year": "2nd year",
                "educationLevel": "Bachelor of Applied Science", "area": "Computer Engineering",
                "interest": "Software engineering co-op", "goal": "Frontend and full-stack development",
                "skills": ["Python", "JavaScript", "Git", "Communication"],
                "projectTitle": "StudyLoop", "projectUrl": "https://github.com/topics/study-planner",
                "projectDescription": "A collaborative study planner built for classmates.",
                "experienceTitle": "Web Developer · UBC Design Team",
                "experienceSummary": "Built student-facing pages and improved mobile layouts.",
                "shareWithEmployers": True, "shareWithPeers": True,
            }),
            ("taylor@skillmatch.demo", {
                "name": "Taylor Singh", "school": "University of Victoria", "year": "4th year",
                "educationLevel": "Bachelor of Science", "area": "Computer Science",
                "interest": "Data and backend engineering", "goal": "Work on data-driven products",
                "skills": ["Python", "JavaScript", "SQL", "Git", "Data analysis"],
                "projectTitle": "Open Data Explorer", "projectUrl": "https://github.com/topics/open-data",
                "projectDescription": "Interactive views of local open data.",
                "experienceTitle": "Research Assistant · UVic Systems Lab",
                "experienceSummary": "Prepared datasets and supported experiment tooling.",
                "shareWithEmployers": True, "shareWithPeers": True,
            }),
            ("morgan@skillmatch.demo", {
                "name": "Morgan Lee", "school": "Langara College", "year": "2nd year",
                "educationLevel": "College Diploma", "area": "Business Technology",
                "interest": "Office administration", "skills": ["Communication", "Figma"],
                "shareWithEmployers": True, "shareWithPeers": True,
            }),
        ]
        student_ids = {email: add_user(email, "student", profile=profile) for email, profile in profiles}

        role = {
            "roleTitle": "Software Engineering Intern", "companyName": "Northstar Labs",
            "team": "Product Engineering", "employment": "Internship", "location": "Vancouver, BC",
            "workMode": "Hybrid", "description": "Help build thoughtful tools for early-career teams.",
            "mustHaveSkills": "Python, JavaScript", "niceToHaveSkills": "SQL, Git",
            "education": "Currently enrolled in a bachelor’s degree", "experience": "Projects or coursework welcome",
            "otherRequirements": "Clear written communication and curiosity.",
        }
        role_id = db.execute("INSERT INTO roles(employer_id,role_json,created_at) VALUES(?,?,?)",
                             (employer_id, json.dumps(role), now)).lastrowid
        second_role = {
            "roleTitle": "Product Design Intern", "companyName": "Northstar Labs",
            "team": "Product Design", "employment": "Internship", "location": "Remote · Canada",
            "workMode": "Remote", "description": "Help shape simple, accessible product experiences.",
            "mustHaveSkills": "Figma", "niceToHaveSkills": "Communication, Research",
            "education": "Currently enrolled in a bachelor’s degree", "experience": "Projects or coursework welcome",
            "otherRequirements": "A portfolio or class project is welcome.",
        }
        db.execute("INSERT INTO roles(employer_id,role_json,created_at) VALUES(?,?,?)",
                   (employer_id, json.dumps(second_role), now))

        score_sets = {
            "jamie@skillmatch.demo": {"Python": 94, "JavaScript": 86, "SQL": 88, "Git": 91, "Communication": 83},
            "alex@skillmatch.demo": {"Python": 78, "JavaScript": 91, "Git": 84, "Communication": 88},
            "taylor@skillmatch.demo": {"Python": 89, "JavaScript": 73, "SQL": 93, "Git": 87, "Data analysis": 90},
            "morgan@skillmatch.demo": {"Communication": 86, "Figma": 91},
        }
        for email, scores in score_sets.items():
            for skill, score in scores.items():
                db.execute("INSERT INTO assessments(user_id,skill,score,total,updated_at) VALUES(?,?,?,?,?)",
                           (student_ids[email], skill, score, 100, now))

        jamie_id = student_ids["jamie@skillmatch.demo"]
        taylor_id = student_ids["taylor@skillmatch.demo"]
        alex_id = student_ids["alex@skillmatch.demo"]
        db.execute("INSERT INTO applications(role_id,student_id,status,created_at,updated_at) VALUES(?,?,'reviewing',?,?)",
                   (role_id, jamie_id, now - 86400, now))
        db.execute("INSERT INTO shortlists(role_id,student_id,employer_id,created_at) VALUES(?,?,?,?)",
                   (role_id, jamie_id, employer_id, now - 3600))
        db.execute("INSERT INTO invitations(role_id,student_id,employer_id,created_at) VALUES(?,?,?,?)",
                   (role_id, alex_id, employer_id, now - 1800))
        for student_id in (jamie_id, taylor_id):
            db.execute("INSERT INTO profile_views(student_id,employer_id,role_id,viewed_at) VALUES(?,?,?,?)",
                       (student_id, employer_id, role_id, now - 7200))
        db.execute("INSERT INTO messages(role_id,student_id,employer_id,sender_id,body,created_at) VALUES(?,?,?,?,?,?)",
                   (role_id, jamie_id, employer_id, employer_id,
                    "Hi Jamie, thanks for applying. We liked your CampusConnect project and would love to hear more about it.", now - 3500))
        db.execute("INSERT INTO messages(role_id,student_id,employer_id,sender_id,body,created_at) VALUES(?,?,?,?,?,?)",
                   (role_id, jamie_id, employer_id, jamie_id,
                    "Thanks! I’d be happy to share how we designed the project and split the work across the team.", now - 3300))
        db.execute("INSERT INTO app_meta(key,value) VALUES('demo_seed_v1',?)", (str(now),))


def seed_extended_demo_data():
    """Seed a larger, varied dataset once so the prototype can be explored end to end."""
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if db.execute("SELECT 1 FROM app_meta WHERE key='demo_seed_v2'").fetchone():
            for row in db.execute("SELECT u.id,p.profile_json FROM users u JOIN profiles p ON p.user_id=u.id WHERE u.email LIKE '%@skillmatch.demo'").fetchall():
                profile = json.loads(row["profile_json"])
                if "shareWithAiMatching" not in profile:
                    profile["shareWithAiMatching"] = True
                    db.execute("UPDATE profiles SET profile_json=?,updated_at=? WHERE user_id=?", (json.dumps(profile), int(time.time()), row["id"]))
            return

        now = int(time.time())
        password = "SkillMatchDemo!"
        first_names = ["Avery", "Jordan", "Riley", "Cameron", "Quinn", "Sasha", "Drew", "Reese", "Rowan", "Emerson", "Parker", "Skyler", "Dakota", "Finley", "Harper", "Kai", "Logan", "Milan", "Noah", "Sage"]
        last_names = ["Patel", "Kim", "Nguyen", "Brown", "Wilson", "Singh", "Martin", "Chen", "Garcia", "Shah", "Campbell", "Thompson", "Kaur", "Robinson", "Lee", "Anderson", "Ali", "Wong", "Taylor", "Clarke"]
        schools = ["Simon Fraser University", "University of British Columbia", "University of Victoria", "University of Calgary", "University of Waterloo", "Toronto Metropolitan University", "Langara College", "University of Alberta"]
        skill_sets = [
            ["Python", "JavaScript", "SQL", "Git", "Communication"],
            ["SQL", "Data analysis", "Python", "Research", "Communication"],
            ["Figma", "Research", "Communication", "Writing", "JavaScript"],
            ["Communication", "Leadership", "Writing", "Data analysis", "Research"],
            ["Python", "SQL", "Research", "Git", "Leadership"],
            ["Figma", "JavaScript", "Python", "Communication", "Git"],
        ]
        project_topics = ["student-platform", "data-visualization", "study-planner", "accessibility", "open-data", "portfolio", "campus-events", "task-management"]
        education_levels = ["Bachelor's degree", "Bachelor of Science", "Bachelor of Applied Science", "College Diploma", "Master's degree"]
        years = ["1st year", "2nd year", "3rd year", "4th year", "Graduate"]

        def get_or_add_user(email, role, company_name="", profile=None):
            existing = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if existing:
                return existing["id"]
            salt = secrets.token_bytes(16)
            password_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
            cursor = db.execute(
                "INSERT INTO users(email,role,password_salt,password_hash,company_name,created_at) VALUES(?,?,?,?,?,?)",
                (email, role, salt, password_hash, company_name, now),
            )
            user_id = cursor.lastrowid
            if role == "student":
                db.execute("INSERT INTO profiles(user_id,profile_json,updated_at) VALUES(?,?,?)",
                           (user_id, json.dumps(profile or {}), now))
            return user_id

        employer_specs = [
            ("employer@skillmatch.demo", "Northstar Labs", []),
            ("hiring@brightside.skillmatch.demo", "Brightside Analytics", [
                {"roleTitle": "Data Analyst Co-op", "team": "Insights", "employment": "Co-op", "location": "Vancouver, BC", "workMode": "Hybrid", "description": "Turn product and customer data into useful decisions.", "mustHaveSkills": "SQL, Data analysis", "niceToHaveSkills": "Python, Communication", "education": "Currently enrolled in college or university", "experience": "Relevant projects or coursework", "otherRequirements": "Curiosity and clear communication."},
                {"roleTitle": "Research Assistant", "team": "Applied Research", "employment": "Part-time", "location": "Remote · Canada", "workMode": "Remote", "description": "Support research projects with careful analysis and documentation.", "mustHaveSkills": "Research, Writing", "niceToHaveSkills": "Python, Data analysis", "education": "Currently enrolled in college or university", "experience": "", "otherRequirements": "Strong attention to detail."},
            ]),
            ("careers@morrow.skillmatch.demo", "Morrow Product Studio", [
                {"roleTitle": "Product Design Intern", "team": "Design", "employment": "Internship", "location": "Remote · Canada", "workMode": "Remote", "description": "Help make useful products feel clear and welcoming.", "mustHaveSkills": "Figma, Communication", "niceToHaveSkills": "Research, JavaScript", "education": "Currently enrolled in college or university", "experience": "Projects or coursework welcome", "otherRequirements": "A portfolio or class project is welcome."},
                {"roleTitle": "Community & Operations Intern", "team": "Operations", "employment": "Internship", "location": "Victoria, BC", "workMode": "Hybrid", "description": "Help coordinate programs and improve how teams work together.", "mustHaveSkills": "Communication, Leadership", "niceToHaveSkills": "Writing, Data analysis", "education": "", "experience": "", "otherRequirements": "Organized and comfortable working with people."},
            ]),
        ]
        employer_ids = {}
        role_ids = []
        for email, company, roles in employer_specs:
            employer_id = get_or_add_user(email, "employer", company)
            employer_ids[company] = employer_id
            for role in roles:
                full_role = {"companyName": company, **role}
                existing = db.execute("SELECT id FROM roles WHERE employer_id=? AND role_json=?", (employer_id, json.dumps(full_role))).fetchone()
                if existing:
                    role_ids.append((existing["id"], employer_id, company))
                else:
                    role_id = db.execute("INSERT INTO roles(employer_id,role_json,created_at) VALUES(?,?,?)",
                                         (employer_id, json.dumps(full_role), now)).lastrowid
                    role_ids.append((role_id, employer_id, company))
        # Include Northstar's pre-existing roles so the larger roster fills the original employer view too.
        for row in db.execute("SELECT id,employer_id,role_json FROM roles WHERE employer_id=?", (employer_ids["Northstar Labs"],)):
            if not any(role_id == row["id"] for role_id, _, _ in role_ids):
                role_ids.append((row["id"], row["employer_id"], json.loads(row["role_json"]).get("companyName", "Northstar Labs")))

        student_ids = []
        for number in range(1, 61):
            first = first_names[(number - 1) % len(first_names)]
            last = last_names[(((number - 1) // 20) * 3 + ((number - 1) % 20) * 7) % len(last_names)]
            name = f"{first} {last}"
            email = f"student{number:02d}@skillmatch.demo"
            skills = skill_sets[(number - 1) % len(skill_sets)][:3 + (number % 3)]
            topic = project_topics[(number - 1) % len(project_topics)]
            profile = {
                "name": name, "school": schools[(number - 1) % len(schools)], "year": years[(number - 1) % len(years)],
                "educationLevel": education_levels[(number - 1) % len(education_levels)],
                "area": ["Computing Science", "Data Science", "Interaction Design", "Business", "Computer Engineering", "Information Systems"][(number - 1) % 6],
                "interest": ["Software engineering", "Data & analytics", "Product design", "Research", "Business operations", "Product"][(number - 1) % 6],
                "goal": "Apply classroom learning to practical team projects.", "skills": skills,
                "projectTitle": f"{['Campus', 'Open', 'Bright', 'Study', 'Green', 'Community'][number % 6]} {['Connect', 'Insights', 'Planner', 'Design System', 'Explorer', 'Toolkit'][(number // 3) % 6]}",
                "projectUrl": f"https://github.com/topics/{topic}",
                "projectDescription": "A student project exploring practical, accessible digital tools.",
                "experienceTitle": "Student Research Assistant" if number % 3 == 0 else ("Peer Mentor · Campus Programs" if number % 3 == 1 else ""),
                "experienceSummary": "Collaborated on research, documentation, and project delivery." if number % 3 != 2 else "",
                "shareWithEmployers": number % 10 != 0, "shareWithPeers": number % 5 != 0,
                "shareWithAiMatching": True,
            }
            student_id = get_or_add_user(email, "student", profile=profile)
            student_ids.append((number, email, student_id, profile))
            for skill_index, skill in enumerate(skills):
                if (number + skill_index) % 4 != 0:
                    score = 52 + ((number * 13 + skill_index * 17) % 47)
                    db.execute("INSERT OR IGNORE INTO assessments(user_id,skill,score,total,updated_at) VALUES(?,?,?,?,?)",
                               (student_id, skill, score, 100, now - ((number + skill_index) % 20) * 86400))

        statuses = ["submitted", "reviewing", "interview", "rejected", "offer"]
        for role_index, (role_id, employer_id, company) in enumerate(role_ids):
            for number, email, student_id, profile in student_ids:
                # Spread applications and shortlists across every company and role.
                if (number * 3 + role_index * 5) % 17 == 0:
                    status = statuses[(number + role_index) % len(statuses)]
                    db.execute("INSERT OR IGNORE INTO applications(role_id,student_id,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                               (role_id, student_id, status, now - (number % 25 + 1) * 86400, now - (number % 5) * 3600))
                if (number + role_index * 2) % 11 == 0:
                    db.execute("INSERT OR IGNORE INTO shortlists(role_id,student_id,employer_id,created_at) VALUES(?,?,?,?)",
                               (role_id, student_id, employer_id, now - (number % 10 + 1) * 3600))
                if (number + role_index * 3) % 19 == 0:
                    db.execute("INSERT OR IGNORE INTO invitations(role_id,student_id,employer_id,created_at) VALUES(?,?,?,?)",
                               (role_id, student_id, employer_id, now - (number % 4 + 1) * 3600))
                if (number + role_index) % 7 == 0:
                    db.execute("INSERT INTO profile_views(student_id,employer_id,role_id,viewed_at) VALUES(?,?,?,?)",
                               (student_id, employer_id, role_id, now - (number % 14 + 1) * 3600))
                has_application = db.execute("SELECT 1 FROM applications WHERE role_id=? AND student_id=?", (role_id, student_id)).fetchone()
                has_shortlist = db.execute("SELECT 1 FROM shortlists WHERE role_id=? AND student_id=?", (role_id, student_id)).fetchone()
                if (has_application or has_shortlist) and number % 9 == 0:
                    db.execute("INSERT INTO messages(role_id,student_id,employer_id,sender_id,body,created_at) VALUES(?,?,?,?,?,?)",
                               (role_id, student_id, employer_id, employer_id,
                                f"Hi {profile['name'].split()[0]}, thanks for your interest in {json.loads(db.execute('SELECT role_json FROM roles WHERE id=?', (role_id,)).fetchone()['role_json']).get('roleTitle', 'the role')}. We'd be glad to hear more about your project work.", now - 1800))
                    if number % 2 == 0:
                        db.execute("INSERT INTO messages(role_id,student_id,employer_id,sender_id,body,created_at) VALUES(?,?,?,?,?,?)",
                                   (role_id, student_id, employer_id, student_id,
                                    "Thanks for reaching out. I would be happy to share more about my work.", now - 1200))

        db.execute("INSERT INTO app_meta(key,value) VALUES('demo_seed_v2',?)", (str(now),))


def seed_dashboard_demo_data():
    """Add enough job and application history to demonstrate both sides of the dashboard."""
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if db.execute("SELECT 1 FROM app_meta WHERE key='demo_seed_v3'").fetchone():
            return
        now = int(time.time())
        role_specs = [
            ("Northstar Labs", "Backend API Co-op", "Platform", "Co-op", "Python, SQL", "Git, Communication", "Build and improve APIs used by student-facing products."),
            ("Northstar Labs", "Frontend Product Intern", "Product Engineering", "Internship", "JavaScript, Communication", "Git, Python", "Build accessible product interfaces and collaborate with designers."),
            ("Northstar Labs", "QA Automation Intern", "Quality Engineering", "Internship", "Python, Git", "JavaScript, Communication", "Write test tools and help teams ship reliable software."),
            ("Northstar Labs", "Data Platform Intern", "Data Engineering", "Internship", "Python, SQL", "Git, Communication", "Support data pipelines and internal tools for product teams."),
            ("Northstar Labs", "Cloud Support Co-op", "Developer Platform", "Co-op", "Python, Communication", "SQL, Git", "Help diagnose technical issues and improve internal support tools."),
            ("Northstar Labs", "Software QA Co-op", "Quality Engineering", "Co-op", "JavaScript, Git", "Python, Communication", "Test web applications and improve release confidence."),
            ("Northstar Labs", "Developer Experience Intern", "Platform", "Internship", "Python, JavaScript", "Git, SQL", "Make development workflows clearer and easier for product teams."),
            ("Brightside Analytics", "Data Quality Analyst", "Analytics", "Internship", "SQL, Communication", "Python, Git", "Check datasets and communicate useful findings to partner teams."),
            ("Brightside Analytics", "Business Systems Intern", "Business Systems", "Internship", "SQL, Python", "Communication, Git", "Help connect data and internal software to team workflows."),
            ("Brightside Analytics", "Reporting Developer Co-op", "Reporting", "Co-op", "SQL, Git", "Python, Communication", "Build repeatable reports and support analytics tooling."),
            ("Morrow Product Studio", "Application Support Intern", "Product Operations", "Internship", "Python, Communication", "JavaScript, SQL", "Help investigate product issues and turn feedback into improvements."),
        ]
        employers = {row["company_name"]: row["id"] for row in db.execute("SELECT id,company_name FROM users WHERE role='employer'")}
        role_rows = []
        for company, title, team, employment, must_have, nice_to_have, description in role_specs:
            employer_id = employers.get(company)
            if not employer_id:
                continue
            role = {
                "roleTitle": title, "companyName": company, "team": team, "employment": employment,
                "location": "Vancouver, BC" if company == "Northstar Labs" else "Remote · Canada",
                "workMode": "Hybrid" if company == "Northstar Labs" else "Remote",
                "description": description, "mustHaveSkills": must_have, "niceToHaveSkills": nice_to_have,
                "education": "Currently enrolled in college or university",
                "experience": "Projects or coursework welcome",
                "otherRequirements": "Clear communication and curiosity.",
            }
            existing = db.execute("SELECT id FROM roles WHERE employer_id=? AND role_json=?", (employer_id, json.dumps(role))).fetchone()
            role_id = existing["id"] if existing else db.execute(
                "INSERT INTO roles(employer_id,role_json,created_at) VALUES(?,?,?)", (employer_id, json.dumps(role), now)
            ).lastrowid
            role_rows.append((role_id, employer_id, role))

        jamie = db.execute("SELECT id FROM users WHERE email='jamie@skillmatch.demo' AND role='student'").fetchone()
        if jamie:
            all_northstar = db.execute("SELECT r.id,r.role_json FROM roles r JOIN users u ON u.id=r.employer_id WHERE u.company_name='Northstar Labs'").fetchall()
            jamie_eligible = []
            profile = json.loads(db.execute("SELECT profile_json FROM profiles WHERE user_id=?", (jamie["id"],)).fetchone()["profile_json"])
            assessments = {row["skill"]: row["score"] for row in db.execute("SELECT skill,score FROM assessments WHERE user_id=?", (jamie["id"],))}
            for row in all_northstar:
                role = json.loads(row["role_json"])
                if not calculate_match(role, profile, assessments)["missingRequiredSkills"]:
                    jamie_eligible.append((row["id"], role))
            # Make Jamie's activity legible: six applications (including two rejections)
            # and six other eligible roles left in the recommendation list.
            base_role = next((item for item in jamie_eligible if item[1].get("roleTitle") == "Software Engineering Intern"), None)
            selected = [(base_role[0], base_role[1])] if base_role else []
            selected.extend((item[0], item[2]) for item in role_rows if not base_role or item[0] != base_role[0])
            for index, (role_id, role) in enumerate(selected[:6]):
                status = "reviewing" if index == 0 else ["rejected", "interview", "rejected", "submitted", "reviewing"][index - 1]
                db.execute("INSERT INTO applications(role_id,student_id,status,created_at,updated_at) VALUES(?,?,?,?,?) "
                           "ON CONFLICT(role_id,student_id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at",
                           (role_id, jamie["id"], status, now - (index + 2) * 86400, now - index * 3600))
            for row in db.execute("SELECT id,role_json FROM roles WHERE id NOT IN (SELECT role_id FROM applications WHERE student_id=?)", (jamie["id"],)):
                role = json.loads(row["role_json"])
                if not calculate_match(role, profile, assessments)["missingRequiredSkills"]:
                    db.execute("DELETE FROM shortlists WHERE role_id=? AND student_id=?", (row["id"], jamie["id"]))

        generated = db.execute("SELECT u.id,u.email,p.profile_json FROM users u JOIN profiles p ON p.user_id=u.id "
                               "WHERE u.role='student' AND u.email LIKE 'student%@skillmatch.demo'").fetchall()
        statuses = ["submitted", "reviewing", "interview", "rejected", "offer"]
        for role_index, (role_id, employer_id, role) in enumerate(role_rows):
            for student in generated:
                number = int(re.search(r"student(\d+)", student["email"]).group(1))
                student_profile = json.loads(student["profile_json"])
                assessments = {row["skill"]: row["score"] for row in db.execute("SELECT skill,score FROM assessments WHERE user_id=?", (student["id"],))}
                eligible = not calculate_match(role, student_profile, assessments)["missingRequiredSkills"]
                application = eligible and (number + role_index * 3) % 8 in (0, 1)
                if application:
                    db.execute("INSERT OR IGNORE INTO applications(role_id,student_id,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                               (role_id, student["id"], statuses[(number + role_index) % len(statuses)], now - (number % 21 + 1) * 86400, now - (number % 5) * 3600))
                shortlisted = eligible and (number * 2 + role_index) % 13 == 0
                if shortlisted:
                    db.execute("INSERT OR IGNORE INTO shortlists(role_id,student_id,employer_id,created_at) VALUES(?,?,?,?)",
                               (role_id, student["id"], employer_id, now - (number % 8 + 1) * 3600))
                if eligible and (number + role_index) % 17 == 0:
                    db.execute("INSERT OR IGNORE INTO invitations(role_id,student_id,employer_id,created_at) VALUES(?,?,?,?)",
                               (role_id, student["id"], employer_id, now - (number % 4 + 1) * 3600))
                if eligible and (number + role_index) % 5 == 0:
                    db.execute("INSERT INTO profile_views(student_id,employer_id,role_id,viewed_at) VALUES(?,?,?,?)",
                               (student["id"], employer_id, role_id, now - (number % 12 + 1) * 3600))
                if (application or shortlisted) and number % 11 == 0:
                    db.execute("INSERT INTO messages(role_id,student_id,employer_id,sender_id,body,created_at) VALUES(?,?,?,?,?,?)",
                               (role_id, student["id"], employer_id, employer_id,
                                f"Hi {json.loads(student['profile_json']).get('name', 'there').split()[0]}, thanks for your interest in {role['roleTitle']}. Could you tell us more about your recent project?", now - 900))
        db.execute("INSERT INTO app_meta(key,value) VALUES('demo_seed_v3',?)", (str(now),))


def json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def conversation_unread_count(db, user_id, role_id, student_id):
    read = db.execute("SELECT last_read_message_id FROM conversation_reads WHERE user_id=? AND role_id=? AND student_id=?",
                      (user_id, role_id, student_id)).fetchone()
    last_read_id = read["last_read_message_id"] if read else 0
    return db.execute("SELECT COUNT(*) FROM messages WHERE role_id=? AND student_id=? AND sender_id!=? AND id>?",
                      (role_id, student_id, user_id, last_read_id)).fetchone()[0]


def student_can_message(db, student_id, role_id):
    shortlisted = db.execute("SELECT 1 FROM shortlists WHERE role_id=? AND student_id=?", (role_id, student_id)).fetchone()
    if shortlisted:
        return True
    employer_id = db.execute("SELECT employer_id FROM roles WHERE id=?", (role_id,)).fetchone()
    if not employer_id:
        return False
    return db.execute("SELECT 1 FROM messages WHERE role_id=? AND student_id=? AND sender_id=? LIMIT 1",
                      (role_id, student_id, employer_id["employer_id"])).fetchone() is not None


def peer_similarity(left, right):
    def tokens(value):
        return {word for word in re.findall(r"[a-z0-9+#.]+", clean_text(value)) if len(word) > 2}

    def overlap(a, b):
        a_tokens, b_tokens = tokens(a), tokens(b)
        if not a_tokens or not b_tokens:
            return None
        if clean_text(a) == clean_text(b):
            return 100
        return round(100 * len(a_tokens & b_tokens) / len(a_tokens | b_tokens))

    scores = []
    shared = []
    interest_score = overlap(" ".join(str(left.get(k, "")) for k in ("interest", "goal")),
                             " ".join(str(right.get(k, "")) for k in ("interest", "goal")))
    if interest_score is not None:
        scores.append((interest_score, 30))
        if interest_score >= 20:
            shared.append("Similar interests")

    education_values = [key for key in ("educationLevel", "area", "school") if left.get(key) and right.get(key)]
    if education_values:
        education_score = round(100 * sum(clean_text(left[key]) == clean_text(right[key]) for key in education_values) / len(education_values))
        scores.append((education_score, 30))
        if left.get("area") and right.get("area") and clean_text(left["area"]) == clean_text(right["area"]):
            shared.append(f"Same field: {right['area']}")
        if left.get("school") and right.get("school") and clean_text(left["school"]) == clean_text(right["school"]):
            shared.append(f"Same school: {right['school']}")
        if left.get("educationLevel") and right.get("educationLevel") and clean_text(left["educationLevel"]) == clean_text(right["educationLevel"]):
            shared.append(f"Same education: {right['educationLevel']}")

    left_experience = " ".join(str(left.get(k, "")) for k in ("experienceTitle", "experienceSummary"))
    right_experience = " ".join(str(right.get(k, "")) for k in ("experienceTitle", "experienceSummary"))
    experience_score = overlap(left_experience, right_experience)
    if experience_score is not None:
        scores.append((experience_score, 25))
        if experience_score >= 15:
            shared.append("Related work experience")

    left_skills, right_skills = {skill_key(s) for s in split_skills(left.get("skills", []))}, {skill_key(s) for s in split_skills(right.get("skills", []))}
    if left_skills and right_skills:
        common_skills = left_skills & right_skills
        scores.append((round(100 * len(common_skills) / len(left_skills | right_skills)), 15))
        if common_skills:
            shared.append(f"Shared skills: {', '.join(sorted(common_skills)[:4])}")

    weight = sum(part[1] for part in scores)
    return (round(sum(score * item_weight for score, item_weight in scores) / weight) if weight else 0, shared)


class Handler(SimpleHTTPRequestHandler):
    server_version = "SkillMatchLocal/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        # Keep the console useful without logging emails, payloads, or cookies.
        print(f"[{self.log_date_time_string()}] {self.command} {self.path.split('?')[0]} - {args[1] if len(args) > 1 else ''}")

    def send_json(self, status, payload, headers=None):
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size > 8_000_000:
            raise ValueError("Request is too large")
        raw = self.rfile.read(size) if size else b"{}"
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value

    def session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            return cookie["sm_session"].value
        except (KeyError, AttributeError):
            return None

    def current_user(self, db):
        token = self.session_token()
        if not token:
            return None
        hashed = hashlib.sha256(token.encode()).digest()
        row = db.execute("""SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
                            WHERE s.token_hash=? AND s.expires_at>?""", (hashed, int(time.time()))).fetchone()
        return row

    def require_user(self, db, role=None):
        user = self.current_user(db)
        if not user:
            self.send_json(401, {"error": "Please log in to continue."})
            return None
        if role and user["role"] != role:
            self.send_json(403, {"error": f"This action requires a {role} account."})
            return None
        return user

    def do_GET(self):
        parsed = urlparse(self.path)
        requested = (ROOT / parsed.path.lstrip("/")).resolve()
        private_names = {DB_PATH.name, DB_PATH.name + "-wal", DB_PATH.name + "-shm"}
        if requested.name in private_names or parsed.path.startswith("/uploads/resumes/"):
            self.send_error(404)
            return
        if not parsed.path.startswith("/api/"):
            return super().do_GET()
        with connect() as db:
            if parsed.path == "/api/code-assessment/proctor-status":
                user = self.require_user(db, "student")
                if not user: return
                self.send_json(200, {"enabled": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
                                     "model": os.environ.get("SKILLMATCH_PROCTOR_MODEL", "gpt-4.1").strip() or "gpt-4.1",
                                     "sampleIntervalSeconds": 1,
                                     "message": "Live AI review is available for opted-in exams." if os.environ.get("OPENAI_API_KEY", "").strip() else "Live AI review is not configured on this server."})
                return
            if parsed.path == "/api/resume":
                user = self.require_user(db)
                if not user: return
                query = parse_qs(parsed.query)
                resume_id = query.get("resumeId", [""])[0]
                if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", resume_id):
                    self.send_json(404, {"error": "Resume not found."}); return
                candidate = None
                for row in db.execute("SELECT u.id,u.role,p.profile_json FROM users u JOIN profiles p ON p.user_id=u.id WHERE u.role='student'"):
                    profile = json.loads(row["profile_json"])
                    if profile.get("resumeId") == resume_id:
                        candidate = (row, profile); break
                if not candidate:
                    self.send_json(404, {"error": "Resume not found."}); return
                student, profile = candidate
                allowed = user["id"] == student["id"]
                if user["role"] == "employer":
                    role_id = query.get("roleId", [""])[0]
                    role_row = db.execute("SELECT role_json FROM roles WHERE id=? AND employer_id=?", (role_id, user["id"])).fetchone()
                    if role_row:
                        application = db.execute("SELECT 1 FROM applications WHERE role_id=? AND student_id=?", (role_id, student["id"])).fetchone()
                        role_data = json.loads(role_row["role_json"])
                        assessments = {r["skill"]: r["score"] for r in db.execute("SELECT skill,score FROM assessments WHERE user_id=?", (student["id"],))}
                        eligible_match = not calculate_match(role_data, profile, assessments)["missingRequiredSkills"]
                        allowed = bool(application) or (profile.get("shareWithEmployers", False) and eligible_match)
                if not allowed:
                    self.send_json(403, {"error": "You do not have access to this resume."}); return
                file_path = RESUME_DIR / f"{resume_id}.pdf"
                if not file_path.is_file():
                    self.send_json(404, {"error": "Resume file is no longer available."}); return
                payload = file_path.read_bytes()
                filename = re.sub(r"[^A-Za-z0-9._-]", "_", profile.get("resumeName", "Resume.pdf")) or "Resume.pdf"
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Content-Disposition", f'inline; filename="{filename}"')
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(payload)
                return
            if parsed.path == "/api/me":
                user = self.require_user(db)
                if not user:
                    return
                profile_row = db.execute("SELECT profile_json FROM profiles WHERE user_id=?", (user["id"],)).fetchone()
                profile = json.loads(profile_row["profile_json"]) if profile_row else {}
                scores = {r["skill"]: {"score": r["score"], "total": r["total"]}
                          for r in db.execute("SELECT skill,score,total FROM assessments WHERE user_id=?", (user["id"],))}
                self.send_json(200, {"user": {"email": user["email"], "role": user["role"], "companyName": user["company_name"]}, "profile": profile, "assessments": scores})
                return
            if parsed.path == "/api/roles":
                user = self.require_user(db)
                if not user:
                    return
                rows = db.execute("SELECT id,role_json,created_at FROM roles WHERE employer_id=? ORDER BY id DESC", (user["id"],)).fetchall()
                self.send_json(200, {"roles": [{"id": r["id"], **json.loads(r["role_json"]), "createdAt": r["created_at"]} for r in rows]})
                return
            if parsed.path == "/api/jobs":
                user = self.require_user(db, "student")
                if not user:
                    return
                profile_row = db.execute("SELECT profile_json FROM profiles WHERE user_id=?", (user["id"],)).fetchone()
                profile = json.loads(profile_row["profile_json"]) if profile_row else {}
                assessments = {r["skill"]: r["score"] for r in db.execute("SELECT skill,score FROM assessments WHERE user_id=?", (user["id"],))}
                rows = db.execute("SELECT r.id,r.role_json,r.employer_id,u.company_name FROM roles r JOIN users u ON u.id=r.employer_id ORDER BY r.id DESC").fetchall()
                jobs = []
                eligible_roles = []
                for row in rows:
                    role = json.loads(row["role_json"])
                    match = calculate_match(role, profile, assessments)
                    if match["missingRequiredSkills"]:
                        continue
                    application = db.execute("SELECT status FROM applications WHERE role_id=? AND student_id=?", (row["id"], user["id"])).fetchone()
                    hidden = db.execute("SELECT 1 FROM hidden_applications WHERE role_id=? AND student_id=?", (row["id"], user["id"])).fetchone()
                    if application and hidden:
                        continue
                    shortlisted = db.execute("SELECT 1 FROM shortlists WHERE role_id=? AND student_id=?", (row["id"], user["id"])).fetchone() is not None
                    invited = db.execute("SELECT 1 FROM invitations WHERE role_id=? AND student_id=?", (row["id"], user["id"])).fetchone() is not None
                    employer_messaged = db.execute("SELECT 1 FROM messages WHERE role_id=? AND student_id=? AND sender_id=? LIMIT 1", (row["id"], user["id"], row["employer_id"])).fetchone() is not None
                    jobs.append({"id": row["id"], **role, "companyName": role.get("companyName") or row["company_name"],
                                 "match": match, "applicationStatus": application["status"] if application else None,
                                 "shortlisted": shortlisted, "invited": invited, "employerMessaged": employer_messaged})
                    eligible_roles.append((row["id"], role))
                semantic_scores, ai_status = semantic_match_scores(
                    db, [(str(role_id), role, profile) for role_id, role in eligible_roles])
                for job in jobs:
                    if str(job["id"]) in semantic_scores:
                        job["match"] = calculate_match(job, profile, assessments, semantic_scores[str(job["id"])])
                jobs.sort(key=lambda job: job["match"]["score"], reverse=True)
                self.send_json(200, {"jobs": jobs, "matchingStatus": ai_status,
                                     "matchingMode": "ai-assisted" if semantic_scores else "rules-based"})
                return
            if parsed.path == "/api/student/activity":
                user = self.require_user(db, "student")
                if not user:
                    return
                view_count = db.execute("SELECT COUNT(DISTINCT employer_id) FROM profile_views WHERE student_id=?", (user["id"],)).fetchone()[0]
                applications = db.execute("""SELECT a.status,a.created_at,r.id AS role_id,r.role_json,u.company_name
                    FROM applications a JOIN roles r ON r.id=a.role_id JOIN users u ON u.id=r.employer_id
                    WHERE a.student_id=? ORDER BY a.updated_at DESC""", (user["id"],)).fetchall()
                shortlists = db.execute("""SELECT s.created_at,r.id AS role_id,r.role_json,u.company_name
                    FROM shortlists s JOIN roles r ON r.id=s.role_id JOIN users u ON u.id=s.employer_id
                    WHERE s.student_id=? ORDER BY s.created_at DESC""", (user["id"],)).fetchall()
                threads = db.execute("""SELECT m.role_id,m.employer_id,r.role_json,u.company_name,MAX(m.created_at) AS last_at,
                    (SELECT body FROM messages m2 WHERE m2.role_id=m.role_id AND m2.student_id=m.student_id ORDER BY m2.created_at DESC LIMIT 1) AS last_message
                    FROM messages m JOIN roles r ON r.id=m.role_id JOIN users u ON u.id=m.employer_id
                    WHERE m.student_id=? GROUP BY m.role_id,m.employer_id ORDER BY last_at DESC""", (user["id"],)).fetchall()
                exam = db.execute("SELECT score,raw_score,penalty_points,proctor_frames,proctor_flags,proctor_uncertain_frames,proctor_summary,proctor_issue,proctor_share_consent,finished_at,focus_loss_count,hidden_tab_count,paste_count FROM code_assessment_attempts WHERE user_id=? AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1", (user["id"],)).fetchone()
                python_exam = ({"score": exam["score"], "rawScore": exam["raw_score"], "penaltyPoints": exam["penalty_points"], "passed": round((exam["raw_score"] if exam["raw_score"] is not None else exam["score"]) * 5 / 100), "total": 5, "framesReviewed": exam["proctor_frames"], "uncertainFrames": exam["proctor_uncertain_frames"], "proctorIssue": exam["proctor_issue"],
                                "flags": exam["proctor_flags"], "events": exam["proctor_summary"].split("|") if exam["proctor_summary"] else [],
                                "sessionSignals": {"focusLosses": exam["focus_loss_count"], "tabHides": exam["hidden_tab_count"], "pasteCount": exam["paste_count"]},
                                "shareWithEmployers": bool(exam["proctor_share_consent"]), "finishedAt": exam["finished_at"]} if exam else None)
                self.send_json(200, {
                    "profileViews": view_count,
                    "pythonExam": python_exam,
                    "applications": [{"status": r["status"], "createdAt": r["created_at"], "roleId": r["role_id"], "canMessage": student_can_message(db, user["id"], r["role_id"]), **json.loads(r["role_json"]), "companyName": json.loads(r["role_json"]).get("companyName") or r["company_name"]} for r in applications if not db.execute("SELECT 1 FROM hidden_applications WHERE role_id=? AND student_id=?", (r["role_id"], user["id"])).fetchone()],
                    "shortlists": [{"createdAt": r["created_at"], "roleId": r["role_id"], **json.loads(r["role_json"]), "companyName": json.loads(r["role_json"]).get("companyName") or r["company_name"]} for r in shortlists],
                    "conversations": [{"roleId": r["role_id"], "roleTitle": json.loads(r["role_json"]).get("roleTitle", "Opportunity"), "companyName": json.loads(r["role_json"]).get("companyName") or r["company_name"], "lastMessage": r["last_message"] or "Start a conversation", "unreadCount": conversation_unread_count(db, user["id"], r["role_id"], user["id"])} for r in threads],
                })
                return
            if parsed.path == "/api/peers":
                user = self.require_user(db, "student")
                if not user:
                    return
                own_row = db.execute("SELECT profile_json FROM profiles WHERE user_id=?", (user["id"],)).fetchone()
                own_profile = json.loads(own_row["profile_json"]) if own_row else {}
                if not own_profile.get("shareWithPeers", False):
                    self.send_json(200, {"sharing": False, "peers": []})
                    return
                rows = db.execute("""SELECT u.id,p.profile_json FROM users u JOIN profiles p ON p.user_id=u.id
                    WHERE u.role='student' AND u.id!=?""", (user["id"],)).fetchall()
                peers = []
                for row in rows:
                    profile = json.loads(row["profile_json"])
                    if not profile.get("shareWithPeers", False):
                        continue
                    score, similarities = peer_similarity(own_profile, profile)
                    if not similarities:
                        continue
                    peers.append({"name": profile.get("name", "Job seeker"), "school": profile.get("school", ""),
                                  "year": profile.get("year", ""), "area": profile.get("area", ""),
                                  "educationLevel": profile.get("educationLevel", ""), "interest": profile.get("interest", ""),
                                  "experienceTitle": profile.get("experienceTitle", ""),
                                  "experienceSummary": profile.get("experienceSummary", ""),
                                  "skills": split_skills(profile.get("skills", [])), "projectTitle": profile.get("projectTitle", ""),
                                  "projectUrl": profile.get("projectUrl", ""), "similarity": score, "similarities": similarities})
                peers.sort(key=lambda peer: peer["similarity"], reverse=True)
                self.send_json(200, {"sharing": True, "peers": peers[:12]})
                return
            if parsed.path == "/api/messages":
                user = self.require_user(db)
                if not user:
                    return
                params = parse_qs(parsed.query)
                role_id = params.get("roleId", [""])[0]
                role_row = db.execute("SELECT r.employer_id,r.role_json,u.company_name FROM roles r JOIN users u ON u.id=r.employer_id WHERE r.id=?", (role_id,)).fetchone()
                student_email = params.get("studentEmail", [""])[0]
                student = db.execute("SELECT id,email FROM users WHERE email=? AND role='student'", (student_email,)).fetchone()
                if not role_row or not student:
                    self.send_json(404, {"error": "Conversation not found."}); return
                if user["role"] == "employer" and user["id"] != role_row["employer_id"] or user["role"] == "student" and user["id"] != student["id"]:
                    self.send_json(403, {"error": "You cannot access this conversation."}); return
                allowed = db.execute("SELECT 1 FROM applications WHERE role_id=? AND student_id=? UNION SELECT 1 FROM shortlists WHERE role_id=? AND student_id=?", (role_id, student["id"], role_id, student["id"])).fetchone()
                if user["role"] == "student" and not student_can_message(db, student["id"], role_id):
                    self.send_json(403, {"error": "You can message after the employer shortlists you or sends you a message."}); return
                if user["role"] == "employer" and not allowed:
                    self.send_json(403, {"error": "Messaging opens after an application or employer shortlist."}); return
                latest_id = db.execute("SELECT COALESCE(MAX(id),0) FROM messages WHERE role_id=? AND student_id=?", (role_id, student["id"])).fetchone()[0]
                db.execute("""INSERT INTO conversation_reads(user_id,role_id,student_id,last_read_message_id) VALUES(?,?,?,?)
                    ON CONFLICT(user_id,role_id,student_id) DO UPDATE SET last_read_message_id=MAX(conversation_reads.last_read_message_id,excluded.last_read_message_id)""",
                    (user["id"], role_id, student["id"], latest_id))
                rows = db.execute("SELECT m.sender_id,m.body,m.created_at,u.email FROM messages m JOIN users u ON u.id=m.sender_id WHERE m.role_id=? AND m.student_id=? ORDER BY m.created_at", (role_id, student["id"])).fetchall()
                role_data = json.loads(role_row["role_json"])
                role_data["companyName"] = role_data.get("companyName") or role_row["company_name"]
                self.send_json(200, {"role": {"id": role_id, **role_data}, "studentEmail": student["email"], "messages": [{"senderId": r["sender_id"], "body": r["body"], "createdAt": r["created_at"], "email": r["email"], "mine": r["sender_id"] == user["id"]} for r in rows]})
                return
            if parsed.path == "/api/matches":
                user = self.require_user(db, "employer")
                if not user:
                    return
                role_id = parse_qs(parsed.query).get("roleId", [""])[0]
                role_row = db.execute("SELECT role_json FROM roles WHERE id=? AND employer_id=?", (role_id, user["id"])).fetchone()
                if not role_row:
                    self.send_json(404, {"error": "Role not found"})
                    return
                role = json.loads(role_row["role_json"])
                students = db.execute("""SELECT u.id,u.email,p.profile_json FROM users u
                    JOIN profiles p ON p.user_id=u.id WHERE u.role='student'""").fetchall()
                eligible = []
                for student in students:
                    dismissed = db.execute("SELECT 1 FROM dismissed_candidates WHERE role_id=? AND student_id=? AND employer_id=?", (role_id, student["id"], user["id"])).fetchone()
                    if dismissed:
                        continue
                    profile = json.loads(student["profile_json"])
                    application = db.execute("SELECT status FROM applications WHERE role_id=? AND student_id=?", (role_id, student["id"])).fetchone()
                    if not application and not profile.get("shareWithEmployers", False):
                        continue
                    assessments = {r["skill"]: r["score"] for r in db.execute("SELECT skill,score FROM assessments WHERE user_id=?", (student["id"],))}
                    base_match = calculate_match(role, profile, assessments)
                    if base_match["missingRequiredSkills"] and not application:
                        continue
                    eligible.append((student, profile, application, assessments))
                semantic_scores, ai_status = semantic_match_scores(
                    db, [(student["email"], role, profile) for student, profile, _, _ in eligible])
                recommended, applicants = [], []
                for student, profile, application, assessments in eligible:
                    match = calculate_match(role, profile, assessments, semantic_scores.get(student["email"]))
                    exam_row = db.execute("SELECT score,raw_score,penalty_points,proctor_frames,proctor_flags,proctor_uncertain_frames,proctor_summary,proctor_issue,proctor_share_consent,focus_loss_count,hidden_tab_count,paste_count FROM code_assessment_attempts WHERE user_id=? AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1", (student["id"],)).fetchone()
                    shared_exam = ({"score": exam_row["score"], "rawScore": exam_row["raw_score"], "penaltyPoints": exam_row["penalty_points"], "passed": round((exam_row["raw_score"] if exam_row["raw_score"] is not None else exam_row["score"]) * 5 / 100), "total": 5,
                                    "proctoringShared": bool(exam_row["proctor_share_consent"]),
                                    "framesReviewed": exam_row["proctor_frames"] if exam_row["proctor_share_consent"] else 0,
                                    "uncertainFrames": exam_row["proctor_uncertain_frames"] if exam_row["proctor_share_consent"] else 0,
                                    "proctorIssue": exam_row["proctor_issue"] if exam_row["proctor_share_consent"] else "",
                                    "flags": exam_row["proctor_flags"] if exam_row["proctor_share_consent"] else 0,
                                    "events": exam_row["proctor_summary"].split("|") if exam_row["proctor_share_consent"] and exam_row["proctor_summary"] else [],
                                    "sessionSignals": {"focusLosses": exam_row["focus_loss_count"], "tabHides": exam_row["hidden_tab_count"], "pasteCount": exam_row["paste_count"]} if exam_row["proctor_share_consent"] else {"focusLosses": 0, "tabHides": 0, "pasteCount": 0}}
                                   if exam_row else None)
                    shortlisted = db.execute("SELECT 1 FROM shortlists WHERE role_id=? AND student_id=?", (role_id, student["id"])).fetchone() is not None
                    invited = db.execute("SELECT 1 FROM invitations WHERE role_id=? AND student_id=?", (role_id, student["id"])).fetchone() is not None
                    candidate = {"email": student["email"], "profile": profile, "applied": bool(application),
                                 "applicationStatus": application["status"] if application else None,
                                 "shortlisted": shortlisted, "invited": invited,
                                 "pythonExam": shared_exam,
                                 "unreadCount": conversation_unread_count(db, user["id"], role_id, student["id"]),
                                 "aiEnhanced": student["email"] in semantic_scores, **match}
                    (applicants if application else recommended).append(candidate)
                recommended.sort(key=lambda row: row["score"], reverse=True)
                applicants.sort(key=lambda row: row["score"], reverse=True)
                self.send_json(200, {"role": role, "matches": recommended, "applicants": applicants,
                                     "matchingMode": "ai-assisted" if semantic_scores else "rules-based",
                                     "matchingStatus": ai_status})
                return
            self.send_json(404, {"error": "Unknown API endpoint"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self.read_json()
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"error": "Invalid JSON request"})
            return
        if path == "/api/resume":
            with connect() as db:
                user = self.require_user(db, "student")
                if not user: return
                file_name = Path(str(body.get("fileName", "Resume.pdf"))).name
                encoded = str(body.get("data", ""))
                match = re.fullmatch(r"data:application/pdf;base64,([A-Za-z0-9+/=]+)", encoded)
                if not match:
                    self.send_json(400, {"error": "Upload a valid PDF resume."}); return
                try:
                    payload = base64.b64decode(match.group(1), validate=True)
                except (ValueError, base64.binascii.Error):
                    self.send_json(400, {"error": "The resume file could not be read."}); return
                if len(payload) > 5 * 1024 * 1024:
                    self.send_json(413, {"error": "Resume must be 5 MB or smaller."}); return
                if not payload.startswith(b"%PDF-"):
                    self.send_json(400, {"error": "The selected file is not a valid PDF."}); return
                resume_id = secrets.token_urlsafe(24)
                RESUME_DIR.mkdir(parents=True, exist_ok=True)
                (RESUME_DIR / f"{resume_id}.pdf").write_bytes(payload)
                row = db.execute("SELECT profile_json FROM profiles WHERE user_id=?", (user["id"],)).fetchone()
                profile = json.loads(row["profile_json"]) if row else {}
                previous_id = profile.get("resumeId")
                profile["resumeId"] = resume_id
                profile["resumeName"] = re.sub(r"[^A-Za-z0-9._ -]", "", file_name)[:100] or "Resume.pdf"
                db.execute("INSERT INTO profiles(user_id,profile_json,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET profile_json=excluded.profile_json,updated_at=excluded.updated_at", (user["id"], json.dumps(profile), int(time.time())))
                if previous_id and re.fullmatch(r"[A-Za-z0-9_-]{20,64}", previous_id):
                    (RESUME_DIR / f"{previous_id}.pdf").unlink(missing_ok=True)
                self.send_json(201, {"resumeId": resume_id, "fileName": profile["resumeName"]})
            return
        if path == "/api/register":
            email = str(body.get("email", "")).strip().lower()
            password = str(body.get("password", ""))
            role = body.get("role")
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
                self.send_json(400, {"error": "Enter a valid email address."}); return
            if len(password) < 8:
                self.send_json(400, {"error": "Use a password with at least 8 characters."}); return
            if role not in ("student", "employer"):
                self.send_json(400, {"error": "Choose Job seeker or Employer."}); return
            profile = body.get("profile", {}) if role == "student" else {}
            if role == "student" and not isinstance(profile, dict):
                self.send_json(400, {"error": "Job seeker profile must be an object."}); return
            salt = secrets.token_bytes(16)
            password_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
            token = secrets.token_urlsafe(32)
            try:
                with connect() as db:
                    cursor = db.execute("INSERT INTO users(email,role,password_salt,password_hash,company_name,created_at) VALUES(?,?,?,?,?,?)",
                                        (email, role, salt, password_hash, str(body.get("companyName", "")).strip(), int(time.time())))
                    user_id = cursor.lastrowid
                    if role == "student":
                        db.execute("INSERT INTO profiles(user_id,profile_json,updated_at) VALUES(?,?,?)", (user_id, json.dumps(profile), int(time.time())))
                    db.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)", (hashlib.sha256(token.encode()).digest(), user_id, int(time.time()) + SESSION_SECONDS))
            except sqlite3.IntegrityError:
                self.send_json(409, {"error": "An account with this email already exists. Log in instead."}); return
            cookie = f"sm_session={token}; HttpOnly; Path=/; SameSite=Lax; Max-Age={SESSION_SECONDS}"
            self.send_json(201, {"user": {"email": email, "role": role, "companyName": body.get("companyName", "")}}, {"Set-Cookie": cookie})
            return
        if path == "/api/login":
            email = str(body.get("email", "")).strip().lower()
            password = str(body.get("password", ""))
            with connect() as db:
                user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                if not user or not hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", password.encode(), user["password_salt"], PBKDF2_ROUNDS), user["password_hash"]):
                    self.send_json(401, {"error": "Email or password is incorrect."}); return
                token = secrets.token_urlsafe(32)
                db.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)", (hashlib.sha256(token.encode()).digest(), user["id"], int(time.time()) + SESSION_SECONDS))
                cookie = f"sm_session={token}; HttpOnly; Path=/; SameSite=Lax; Max-Age={SESSION_SECONDS}"
                self.send_json(200, {"user": {"email": user["email"], "role": user["role"], "companyName": user["company_name"]}}, {"Set-Cookie": cookie})
            return
        if path == "/api/logout":
            token = self.session_token()
            with connect() as db:
                if token:
                    db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).digest(),))
            self.send_json(200, {"ok": True}, {"Set-Cookie": "sm_session=; HttpOnly; Path=/; SameSite=Lax; Max-Age=0"})
            return
        with connect() as db:
            if path == "/api/applications":
                user = self.require_user(db, "student")
                if not user: return
                role_id = str(body.get("roleId", ""))
                role_row = db.execute("SELECT role_json FROM roles WHERE id=?", (role_id,)).fetchone()
                if not role_row:
                    self.send_json(404, {"error": "That job is no longer available."}); return
                profile_row = db.execute("SELECT profile_json FROM profiles WHERE user_id=?", (user["id"],)).fetchone()
                profile = json.loads(profile_row["profile_json"]) if profile_row else {}
                assessments = {r["skill"]: r["score"] for r in db.execute("SELECT skill,score FROM assessments WHERE user_id=?", (user["id"],))}
                match = calculate_match(json.loads(role_row["role_json"]), profile, assessments)
                if match["missingRequiredSkills"]:
                    self.send_json(409, {"error": "You need 60 points or more in every must-have skill to apply."}); return
                now = int(time.time())
                try:
                    db.execute("INSERT INTO applications(role_id,student_id,status,created_at,updated_at) VALUES(?,?,'submitted',?,?)", (role_id, user["id"], now, now))
                except sqlite3.IntegrityError:
                    self.send_json(409, {"error": "You already applied to this job."}); return
                self.send_json(201, {"status": "submitted", "roleId": role_id}); return
            if path == "/api/profile-views":
                employer = self.require_user(db, "employer")
                if not employer: return
                role_id, email = str(body.get("roleId", "")), str(body.get("studentEmail", "")).strip().lower()
                role = db.execute("SELECT id,role_json FROM roles WHERE id=? AND employer_id=?", (role_id, employer["id"])).fetchone()
                student = db.execute("SELECT id,email FROM users WHERE email=? AND role='student'", (email,)).fetchone()
                if not role or not student:
                    self.send_json(404, {"error": "Profile not found."}); return
                profile_row = db.execute("SELECT profile_json FROM profiles WHERE user_id=?", (student["id"],)).fetchone()
                application = db.execute("SELECT 1 FROM applications WHERE role_id=? AND student_id=?", (role_id, student["id"])).fetchone()
                profile = json.loads(profile_row["profile_json"]) if profile_row else {}
                if not application and not profile.get("shareWithEmployers", False):
                    self.send_json(403, {"error": "This candidate has not shared their profile."}); return
                if not application:
                    assessments = {r["skill"]: r["score"] for r in db.execute("SELECT skill,score FROM assessments WHERE user_id=?", (student["id"],))}
                    if calculate_match(json.loads(role["role_json"]), profile, assessments)["missingRequiredSkills"]:
                        self.send_json(403, {"error": "This candidate does not meet the role's must-have skills."}); return
                db.execute("INSERT INTO profile_views(student_id,employer_id,role_id,viewed_at) VALUES(?,?,?,?)", (student["id"], employer["id"], role_id, int(time.time())))
                self.send_json(201, {"ok": True}); return
            if path == "/api/shortlists":
                employer = self.require_user(db, "employer")
                if not employer: return
                role_id, email = str(body.get("roleId", "")), str(body.get("studentEmail", "")).strip().lower()
                role = db.execute("SELECT id,role_json FROM roles WHERE id=? AND employer_id=?", (role_id, employer["id"])).fetchone()
                student = db.execute("SELECT id,email FROM users WHERE email=? AND role='student'", (email,)).fetchone()
                if not role or not student:
                    self.send_json(404, {"error": "Candidate or role not found."}); return
                saved = bool(body.get("saved", True))
                if saved:
                    profile_row = db.execute("SELECT profile_json FROM profiles WHERE user_id=?", (student["id"],)).fetchone()
                    profile = json.loads(profile_row["profile_json"]) if profile_row else {}
                    application = db.execute("SELECT 1 FROM applications WHERE role_id=? AND student_id=?", (role_id, student["id"])).fetchone()
                    assessments = {r["skill"]: r["score"] for r in db.execute("SELECT skill,score FROM assessments WHERE user_id=?", (student["id"],))}
                    if not application and (not profile.get("shareWithEmployers", False) or calculate_match(json.loads(role["role_json"]), profile, assessments)["missingRequiredSkills"]):
                        self.send_json(403, {"error": "This candidate is not eligible to be shortlisted for the role."}); return
                    db.execute("INSERT OR IGNORE INTO shortlists(role_id,student_id,employer_id,created_at) VALUES(?,?,?,?)", (role_id, student["id"], employer["id"], int(time.time())))
                else:
                    db.execute("DELETE FROM shortlists WHERE role_id=? AND student_id=? AND employer_id=?", (role_id, student["id"], employer["id"]))
                self.send_json(200, {"saved": saved}); return
            if path == "/api/dismissals":
                employer = self.require_user(db, "employer")
                if not employer: return
                role_id, email = str(body.get("roleId", "")), str(body.get("studentEmail", "")).strip().lower()
                role = db.execute("SELECT id FROM roles WHERE id=? AND employer_id=?", (role_id, employer["id"])).fetchone()
                student = db.execute("SELECT id FROM users WHERE email=? AND role='student'", (email,)).fetchone()
                if not role or not student:
                    self.send_json(404, {"error": "Candidate or role not found."}); return
                db.execute("INSERT OR IGNORE INTO dismissed_candidates(role_id,student_id,employer_id,created_at) VALUES(?,?,?,?)",
                           (role_id, student["id"], employer["id"], int(time.time())))
                self.send_json(200, {"dismissed": True}); return
            if path == "/api/invitations":
                employer = self.require_user(db, "employer")
                if not employer: return
                role_id, email = str(body.get("roleId", "")), str(body.get("studentEmail", "")).strip().lower()
                role = db.execute("SELECT id,role_json FROM roles WHERE id=? AND employer_id=?", (role_id, employer["id"])).fetchone()
                student = db.execute("SELECT id FROM users WHERE email=? AND role='student'", (email,)).fetchone()
                if not role or not student:
                    self.send_json(404, {"error": "Candidate or role not found."}); return
                profile_row = db.execute("SELECT profile_json FROM profiles WHERE user_id=?", (student["id"],)).fetchone()
                profile = json.loads(profile_row["profile_json"]) if profile_row else {}
                assessments = {r["skill"]: r["score"] for r in db.execute("SELECT skill,score FROM assessments WHERE user_id=?", (student["id"],))}
                if not profile.get("shareWithEmployers", False) or calculate_match(json.loads(role["role_json"]), profile, assessments)["missingRequiredSkills"]:
                    self.send_json(403, {"error": "Only eligible candidates who shared their profile can be invited."}); return
                db.execute("INSERT OR IGNORE INTO invitations(role_id,student_id,employer_id,created_at) VALUES(?,?,?,?)", (role_id, student["id"], employer["id"], int(time.time())))
                self.send_json(201, {"invited": True}); return
            if path == "/api/application-status":
                employer = self.require_user(db, "employer")
                if not employer: return
                role_id, email = str(body.get("roleId", "")), str(body.get("studentEmail", "")).strip().lower()
                status = str(body.get("status", ""))
                if status not in ("reviewing", "interview", "rejected", "offer"):
                    self.send_json(400, {"error": "Choose a valid application status."}); return
                updated = db.execute("UPDATE applications SET status=?,updated_at=? WHERE role_id=? AND student_id=(SELECT id FROM users WHERE email=?) AND role_id IN (SELECT id FROM roles WHERE employer_id=?)", (status, int(time.time()), role_id, email, employer["id"]))
                if not updated.rowcount:
                    self.send_json(404, {"error": "Application not found."}); return
                if status != "rejected":
                    db.execute("DELETE FROM hidden_applications WHERE role_id=? AND student_id=(SELECT id FROM users WHERE email=?)", (role_id, email))
                self.send_json(200, {"status": status}); return
            if path == "/api/student/hide-application":
                student = self.require_user(db, "student")
                if not student: return
                role_id = str(body.get("roleId", ""))
                application = db.execute("SELECT status FROM applications WHERE role_id=? AND student_id=?", (role_id, student["id"])).fetchone()
                if not application:
                    self.send_json(404, {"error": "Application not found."}); return
                if application["status"] != "rejected":
                    self.send_json(409, {"error": "Only rejected applications can be removed from your profile."}); return
                db.execute("INSERT OR IGNORE INTO hidden_applications(role_id,student_id,hidden_at) VALUES(?,?,?)", (role_id, student["id"], int(time.time())))
                self.send_json(200, {"hidden": True}); return
            if path == "/api/messages":
                user = self.require_user(db)
                if not user: return
                role_id, email = str(body.get("roleId", "")), str(body.get("studentEmail", "")).strip().lower()
                message = str(body.get("message", "")).strip()
                role = db.execute("SELECT employer_id FROM roles WHERE id=?", (role_id,)).fetchone()
                student = db.execute("SELECT id FROM users WHERE email=? AND role='student'", (email,)).fetchone()
                if not role or not student:
                    self.send_json(404, {"error": "Conversation not found."}); return
                if user["role"] == "employer" and user["id"] != role["employer_id"] or user["role"] == "student" and user["id"] != student["id"]:
                    self.send_json(403, {"error": "You cannot message this conversation."}); return
                allowed = db.execute("SELECT 1 FROM applications WHERE role_id=? AND student_id=? UNION SELECT 1 FROM shortlists WHERE role_id=? AND student_id=?", (role_id, student["id"], role_id, student["id"])).fetchone()
                if user["role"] == "student" and not student_can_message(db, student["id"], role_id):
                    self.send_json(403, {"error": "You can message after the employer shortlists you or sends you a message."}); return
                if user["role"] == "employer" and not allowed:
                    self.send_json(403, {"error": "Messaging opens after an application or employer shortlist."}); return
                if not message or len(message) > 2000:
                    self.send_json(400, {"error": "Write a message of 1 to 2,000 characters."}); return
                cursor = db.execute("INSERT INTO messages(role_id,student_id,employer_id,sender_id,body,created_at) VALUES(?,?,?,?,?,?)", (role_id, student["id"], role["employer_id"], user["id"], message, int(time.time())))
                self.send_json(201, {"id": cursor.lastrowid, "message": message}); return
            if path == "/api/profile":
                user = self.require_user(db, "student")
                if not user: return
                profile = body.get("profile")
                if not isinstance(profile, dict):
                    self.send_json(400, {"error": "Profile must be an object."}); return
                db.execute("INSERT INTO profiles(user_id,profile_json,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET profile_json=excluded.profile_json,updated_at=excluded.updated_at", (user["id"], json.dumps(profile), int(time.time())))
                self.send_json(200, {"profile": profile}); return
            if path == "/api/roles":
                user = self.require_user(db, "employer")
                if not user: return
                role = body.get("role")
                if not isinstance(role, dict) or not str(role.get("roleTitle", "")).strip():
                    self.send_json(400, {"error": "Role title is required."}); return
                role.setdefault("companyName", user["company_name"])
                cursor = db.execute("INSERT INTO roles(employer_id,role_json,created_at) VALUES(?,?,?)", (user["id"], json.dumps(role), int(time.time())))
                self.send_json(201, {"id": cursor.lastrowid, **role}); return
            if path == "/api/code-assessment/start":
                user = self.require_user(db, "student")
                if not user: return
                if body.get("liveProctorConsent") is not True or body.get("aiReviewConsent") is not True:
                    self.send_json(400, {"error": "Camera proctoring and AI exam review consent are required to start this assessment."}); return
                if not os.environ.get("OPENAI_API_KEY", "").strip():
                    self.send_json(503, {"error": "This assessment requires AI services. Configure OPENAI_API_KEY on the server and restart it."}); return
                attempt_id = secrets.token_urlsafe(24)
                db.execute("INSERT INTO code_assessment_attempts(attempt_id,user_id,started_at,live_proctor_consent,ai_review_consent) VALUES(?,?,?,?,?)",
                           (attempt_id, user["id"], int(time.time()), 1, 1))
                self.send_json(201, {"attemptId": attempt_id, "durationSeconds": CODING_EXAM_SECONDS}); return
            if path == "/api/code-assessment/proctor-frame":
                user = self.require_user(db, "student")
                if not user: return
                if not os.environ.get("OPENAI_API_KEY", "").strip():
                    self.send_json(503, {"error": "Live AI review is not configured on this server."}); return
                attempt_id = str(body.get("attemptId", ""))
                frame = body.get("frame", "")
                if not isinstance(frame, str) or len(frame) > 280_000:
                    self.send_json(400, {"error": "The camera frame is too large."}); return
                encoded = re.fullmatch(r"data:image/jpeg;base64,([A-Za-z0-9+/]+={0,2})", frame)
                if not encoded:
                    self.send_json(400, {"error": "A JPEG camera frame is required."}); return
                try:
                    raw_frame = base64.b64decode(encoded.group(1), validate=True)
                except ValueError:
                    self.send_json(400, {"error": "The camera frame is invalid."}); return
                if not raw_frame or len(raw_frame) > 200_000:
                    self.send_json(400, {"error": "The camera frame must be under 200 KB."}); return
                now = int(time.time())
                attempt = db.execute("SELECT * FROM code_assessment_attempts WHERE attempt_id=? AND user_id=?", (attempt_id, user["id"])).fetchone()
                if not attempt or attempt["finished_at"] is not None or now - attempt["started_at"] > CODING_EXAM_SECONDS + 15:
                    self.send_json(400, {"error": "This exam attempt is no longer active."}); return
                if not attempt["live_proctor_consent"]:
                    self.send_json(403, {"error": "Live proctoring consent is required for this exam."}); return
                if attempt["proctor_frames"] >= 1200:
                    self.send_json(429, {"error": "The live review frame limit has been reached."}); return
                if attempt["proctor_last_frame_at"] and now - attempt["proctor_last_frame_at"] < 1:
                    self.send_json(429, {"error": "Please wait before sending the next camera frame."}); return
                observation = review_proctor_frame(frame)
                event = observation["event"] if observation else "uncertain"
                flagged_events = {"person_absent", "multiple_people", "phone_visible", "gaze_away"}
                flags = attempt["proctor_flags"] + (1 if event in flagged_events else 0)
                uncertain_frames = attempt["proctor_uncertain_frames"] + (1 if event == "uncertain" else 0)
                issue = observation["note"][:160] if event == "uncertain" and observation else ""
                summaries = [item for item in attempt["proctor_summary"].split("|") if item]
                if event in {"person_absent", "multiple_people", "phone_visible", "gaze_away"}:
                    summaries.append(event)
                db.execute("UPDATE code_assessment_attempts SET proctor_frames=proctor_frames+1,proctor_flags=?,proctor_uncertain_frames=?,proctor_last_frame_at=?,proctor_summary=?,proctor_issue=? WHERE attempt_id=?",
                           (flags, uncertain_frames, now, "|".join(summaries[-20:]), issue, attempt_id))
                self.send_json(200, {"event": event, "note": observation["note"] if observation else "Frame review unavailable."})
                return
            if path == "/api/code-assessment/submit":
                user = self.require_user(db, "student")
                if not user: return
                attempt_id = str(body.get("attemptId", ""))
                attempt = db.execute("SELECT * FROM code_assessment_attempts WHERE attempt_id=? AND user_id=?",
                                     (attempt_id, user["id"])).fetchone()
                now = int(time.time())
                if not attempt or attempt["finished_at"] is not None:
                    self.send_json(400, {"error": "This assessment attempt is invalid or already submitted."}); return
                if not attempt["live_proctor_consent"] or attempt["proctor_frames"] < 1:
                    self.send_json(400, {"error": "Live camera review must be active before this exam can be submitted."}); return
                if now - attempt["started_at"] > CODING_EXAM_SECONDS + 15:
                    db.execute("UPDATE code_assessment_attempts SET finished_at=? WHERE attempt_id=?", (now, attempt_id))
                    self.send_json(400, {"error": "The 20 minute exam window has ended. Start a new attempt to try again."}); return
                source = body.get("code", "")
                try:
                    result = grade_python_submission(source)
                except (ValueError, SyntaxError) as exc:
                    self.send_json(400, {"error": str(exc)[:240]}); return
                raw_score = round(result["passed"] * 100 / result["total"])
                signals_in = body.get("signals", {}) if isinstance(body.get("signals"), dict) else {}
                signals = {key: max(0, min(100, int(signals_in.get(key, 0) or 0)))
                           for key in ("focusLosses", "tabHides", "pasteCount")}
                consent = bool(attempt["ai_review_consent"])
                share_proctoring = attempt["proctor_frames"] > 0
                proctor_events = attempt["proctor_summary"].split("|") if attempt["proctor_summary"] else []
                penalty_points = (25 if signals["pasteCount"] > 0 else 0)
                penalty_points += 5 * sum((signals["focusLosses"] > 0, signals["tabHides"] > 0, attempt["proctor_flags"] > 0))
                penalty_points = min(raw_score, penalty_points)
                score = max(0, raw_score - penalty_points)
                proctor_signals = {"framesReviewed": attempt["proctor_frames"], "visualFlags": attempt["proctor_flags"],
                                   "visualEvents": proctor_events}
                ai_review = review_coding_attempt(source, result["passed"], {**signals, **proctor_signals}) if consent else None
                db.execute("UPDATE code_assessment_attempts SET finished_at=?,score=?,raw_score=?,penalty_points=?,proctor_share_consent=?,focus_loss_count=?,hidden_tab_count=?,paste_count=? WHERE attempt_id=?",
                           (now, score, raw_score, penalty_points, int(share_proctoring), signals["focusLosses"], signals["tabHides"], signals["pasteCount"], attempt_id))
                db.execute("INSERT INTO assessments(user_id,skill,score,total,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id,skill) DO UPDATE SET score=excluded.score,total=excluded.total,updated_at=excluded.updated_at",
                           (user["id"], "Python", score, result["total"], now))
                self.send_json(200, {"skill":"Python", "score":score, "total":result["total"],
                                     "passed":result["passed"], "rawScore":raw_score, "penaltyPoints":penalty_points, "durationSeconds":now-attempt["started_at"],
                                     "aiReview":ai_review, "aiReviewEnabled":bool(consent and os.environ.get("OPENAI_API_KEY")),
                                     "proctorFrames":attempt["proctor_frames"], "proctorFlags":attempt["proctor_flags"], "proctorUncertainFrames":attempt["proctor_uncertain_frames"], "proctorIssue":attempt["proctor_issue"],
                                     "proctorEvents":attempt["proctor_summary"].split("|") if attempt["proctor_summary"] else [],
                                     "sessionSignals":{"focusLosses":signals["focusLosses"], "tabHides":signals["tabHides"], "pasteCount":signals["pasteCount"]},
                                     "proctorSharedWithEmployers":share_proctoring}); return
            if path == "/api/assessments":
                user = self.require_user(db, "student")
                if not user: return
                skill = str(body.get("skill", "")).strip()
                score, total = body.get("score"), body.get("total")
                if skill.casefold() == "python":
                    self.send_json(400, {"error": "Python results must come from the coding exam grader."}); return
                if not skill or not isinstance(score, int) or not isinstance(total, int) or total < 1 or not 0 <= score <= 100:
                    self.send_json(400, {"error": "Assessment result is invalid."}); return
                db.execute("INSERT INTO assessments(user_id,skill,score,total,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id,skill) DO UPDATE SET score=excluded.score,total=excluded.total,updated_at=excluded.updated_at", (user["id"], skill, score, total, int(time.time())))
                self.send_json(200, {"skill": skill, "score": score, "total": total}); return
        self.send_json(404, {"error": "Unknown API endpoint"})

    def do_PUT(self):
        if urlparse(self.path).path != "/api/profile":
            self.send_json(404, {"error": "Unknown API endpoint"}); return
        try:
            body = self.read_json()
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self.send_json(400, {"error": "Invalid JSON request"}); return
        profile = body.get("profile")
        if not isinstance(profile, dict):
            self.send_json(400, {"error": "Profile must be an object."}); return
        with connect() as db:
            user = self.require_user(db, "student")
            if not user: return
            db.execute("INSERT INTO profiles(user_id,profile_json,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET profile_json=excluded.profile_json,updated_at=excluded.updated_at", (user["id"], json.dumps(profile), int(time.time())))
        self.send_json(200, {"profile": profile})


if __name__ == "__main__":
    init_db()
    seed_demo_data()
    seed_extended_demo_data()
    seed_dashboard_demo_data()
    port = int(os.environ.get("PORT", "8000"))
    print(f"SkillMatch running at http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
