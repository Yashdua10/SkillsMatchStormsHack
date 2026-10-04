# SkillMatch

SkillMatch is a local hackathon prototype with a single-page frontend and a small Python/SQLite API. It uses only the Python standard library.

## Run locally

From this folder, run:

```sh
python3 server.py
```

Then open <http://127.0.0.1:8000>. Keep the terminal running while using the app. The SQLite database is created as `skillmatch.sqlite3` the first time the server starts.

## Ready-to-use demo accounts

On the first server start after this update, SkillMatch adds a larger sample dataset once: 17 job roles across three demo employers, 60 additional candidate profiles, assessment scores, applications in different stages, shortlists, invitations, profile views, and conversations. The added demo candidates use `student01@skillmatch.demo` through `student60@skillmatch.demo`. Jamie's dashboard has six eligible recommendations and six applications, including two rejections. Seed data is versioned, so restarting the server will not create duplicate accounts.

All demo accounts use the password `SkillMatchDemo!`:

| Account | Email | What to explore |
| --- | --- | --- |
| Employer | `employer@skillmatch.demo` | Recommended candidates, applicants, profile views, shortlist, invitations, and chat |
| Employer | `hiring@brightside.skillmatch.demo` | Data Analyst Co-op and Research Assistant candidates |
| Employer | `careers@morrow.skillmatch.demo` | Product Design and Community & Operations candidates |
| Job seeker applicant | `jamie@skillmatch.demo` | Application status, shortlist, employer views, and an existing conversation |
| Job seeker recommended | `alex@skillmatch.demo` | Recommended jobs and an employer invitation |
| Job seeker recommended | `taylor@skillmatch.demo` | Recommended jobs and profile views |
| Job seeker for design role | `morgan@skillmatch.demo` | Product Design role fit |

## Demo flow

1. Use a ready-to-use demo login above, or select **Log in** → **Job seeker account** to create an account with a name, skills, optional education, project link, and optional work experience.
2. On the final signup step, opt in to **profile visibility** if you want your profile to appear in employer matches.
3. Take assessments for the selected skills from the job seeker dashboard. Scores are saved to SQLite and used by matching.
4. Job seeker accounts can browse eligible roles, apply, track application status, see employer shortlists and profile views, and message employers after applying or being shortlisted.
5. Log out, create or log in to an employer account, add a role and its requirements, then open its matches. **Recommended** lists eligible job seekers who have not applied; employers can invite them to apply, shortlist them, or remove them from that role’s candidate lists. **Applicants** lists candidates who have submitted an application. Use the **Shortlisted** filter to narrow either list.

Accounts use email and a password of at least eight characters. Passwords are stored as salted PBKDF2 hashes. Employer profile sharing is optional. Peer discovery is a separate opt-in; it compares education, interests, experience, and skills, and only shows a limited profile to other job seekers who also opted in.

## Skill assessments

The Python assessment is a 20-minute coding task. The server issues a timed attempt, runs the submission in a constrained child process, and grades it against five hidden test cases. The Python runner accepts a limited set of statements and built-in helpers; imports, file access, and attribute access are disabled. The score saved for Python comes from this grader, not a score sent by the browser. Other skill assessments still use the existing multiple-choice questions.

Before starting, candidates must consent to both live camera proctoring and AI review of their code and exam flags (focus changes, tab changes, and paste events). The browser requires camera access and sends a downscaled still up to once per second to the vision model; the stills are not saved. The model may flag no clearly visible person, multiple people, a clearly visible phone, or possible head/gaze direction away from the screen. Frames are reviewed individually and absence detection can still miss or misclassify a person. The database retains only a bounded set of brief event labels and counters. This is sampled frame analysis rather than a continuous video recording; checks can miss behavior between frames and can be mistaken. Because live proctoring is required for this exam, its brief flags appear to employers who can view the candidate profile; camera images are never shared. The Python score is shown to employers when they can view the candidate profile. The model does not identify anyone, decide that cheating occurred, change the hidden-test score, or automatically fail a candidate. Nonzero focus-change, hidden-tab, and paste counts are labeled as exam flags in the exam result, student dashboard, and employer match breakdown; any paste activity deducts 25 points once, and each of the focus-change, tab-change, and visual-flag types deducts 5 points once; deductions cannot exceed the hidden-test score. Focus changes and camera classifications can be mistaken or caused by ordinary interruptions. The app counts paste events but does not determine whether pasted code was copied from another person; code similarity review would need a separate plagiarism check. Camera permission and both AI consents are required to start this coding exam. Live review and code review both use `OPENAI_API_KEY`; without it, the timed Python exam cannot start, while non-Python multiple-choice assessments continue to work. `SKILLMATCH_EXAM_AI_MODEL` can change the default `gpt-4.1-mini` review model and `SKILLMATCH_PROCTOR_MODEL` can change the default vision model, which defaults to `gpt-4.1` for presence detection.

## Match scoring

Matches are scored on the server from the job seeker profile and the employer's role criteria. The default weights are 50% must-have skills, 15% preferred skills, 10% education, 10% experience, 10% role relevance, and 5% project/work evidence. Education and experience components are omitted when the role has no such requirement; remaining weights are normalized to the criteria present.

An assessment contributes its score for that skill. A self-listed skill with no assessment contributes 60 points. Job seekers with a must-have skill score below 60 are excluded from the match results; a selected but untested skill meets the current threshold, while an assessment below 60 does not. The result includes component scores and short reasons so employers can see how each eligible match was ranked. Experience and role relevance are profile-based signals, not independently verified facts.

Applications are scoped to a role and become visible to that role's employer. Applying shares the job seeker's profile with that employer for that role, even if the job seeker hasn't enabled general profile visibility. Profile-view totals count distinct employer accounts. Job seekers can see employer invitations and shortlists. Employers can message applicants or shortlisted candidates; job seekers can reply after being shortlisted or after the employer sends the first message. Job seekers can hide rejected applications from their own dashboard while the employer's application record remains available.

Retaking an assessment replaces that job seeker's saved score for the same skill. Role matches use the latest saved score the next time jobs or matches load.

## AI-assisted matching

SkillMatch can optionally add semantic similarity from OpenAI embeddings to employer and job seeker rankings. Add an API key in the server terminal before starting the app:

```sh
export OPENAI_API_KEY="your-api-key"
python3 server.py
```

`SKILLMATCH_AI_MODEL` can override the default `text-embedding-3-small` matching model. `SKILLMATCH_EXAM_AI_MODEL` can override the default `gpt-4.1-mini` exam-review model. Job seekers must opt in during registration or under **AI matching preferences** on their dashboard. The app sends the opted-in job seeker's skills, interests, goal, project summary, and experience summary together with the role text. It does not send their name, email, school, or resume. Embeddings are cached in SQLite by model and text fingerprint. Without a configured key, matching continues with the existing rules-based score.

The AI semantic-fit component is an additional 15-point-weight component in the normalized score. It cannot satisfy a missing must-have skill, change assessment verification, or change the eligibility gate. Both sides can see that a ranking used the AI signal, and the match breakdown keeps semantic relatedness separate from the overall weighted fit.

## Prototype limits

- The server binds to loopback (`127.0.0.1`) and is intended for local development only. Do not expose it to the public internet.
- Non-Python skill quizzes remain demo-grade and their browser-submitted scores are not independently graded. Python scores are graded by the constrained server runner described above; this local prototype is not a production code-execution service.
- Email verification, password reset, production deployment, and a hardened production security review are not included.
- `skillmatch.sqlite3` contains account and profile data. Keep it local and do not commit or share it.
