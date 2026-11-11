# Quality Report Triage Agent: Project Kickoff

Nov 11, 2026 · @Faisal

## Project summary

We will build an agent that reads unstructured quality reports from a switchgear factory and turns them into validated quality tickets. We build the core in one 6-hour session on a MacBook. Everything else in this doc becomes phase 2.

**The problem.** Quality engineers receive defect reports as emails, PDFs and free-text notes. They read each one, find the order and component, judge severity and type the details into the quality system by hand. Our working estimate: 10 to 20 minutes per report, with typos and missed fields.

**The solution.** An LLM extracts the fields. Python checks them against master data. A person approves uncertain or critical cases. A bot or an API call posts the approved ticket into the quality system.

**Business goals**

- Cut manual handling time per report by at least 70%
- Reach 90% or better field accuracy on the evaluation set
- Route 100% of critical defects to a human before posting
- Keep a full audit trail of every input, extraction and decision

**Portfolio goal.** Show UiPath, Python, API integration, AI agents, guardrails and testing in one end-to-end system for the Siemens Energy interview and beyond.

## Scope

The 6-hour build covers English email reports end to end, from text file to posted ticket. It runs on macOS with Python and SQLite, and needs no Docker or Windows.

| In scope (6-hour build) | Phase 2 |
| --- | --- |
| English reports as .txt and .eml files in an input folder | German reports, PDFs, scanned notes |
| One run command that processes the folder | Live inbox watcher and IMAP reader |
| Gemini extraction of 10 fixed fields with confidence scores | Model comparison and OpenAI fallback |
| Validation against fake master data | Bill of materials checks |
| Routing: critical or uncertain reports go to a person | Morning summary email |
| Streamlit review screen and a metrics page | Power BI dashboard (needs Windows) |
| Mock QM API in FastAPI, posting with retries and idempotency keys | Connection to a real SAP system |
| SQLite storage and audit log | PostgreSQL, Docker Compose, CI |
| Evaluation script on 20 labelled reports | UiPath bot for the legacy form (Studio Web or a Windows VM) |

Phase 2 items stay in the backlog. We pick them up after the core works.

## Stakeholders and roles

You play the client and product owner. I play the developer. The other roles are personas we design for.

| Role | Who | What they need |
| --- | --- | --- |
| Client and product owner | Faisal | Sets priorities, approves scope, signs off each milestone |
| Developer | Claude | Designs, builds, tests and documents the system |
| Quality engineer (persona) | Main user | Fewer manual entries, fast review of flagged reports |
| Quality manager (persona) | Dashboard user | Volumes, defect trends, processing time, accuracy |
| IT and security (persona) | Gatekeeper | Approved models, safe secrets, audit trail |
| Works council and data protection (persona) | Reviewer | No hidden monitoring of employees, GDPR-safe handling of names |

## Functional requirements

Each requirement has an ID so tests and tickets can point to it. Priority follows MoSCoW: Must, Should, Could. The 6-hour build covers FR-02 to FR-10, FR-12 and FR-13. FR-01 runs as a manual command for now. FR-11, FR-14 and FR-15 move to phase 2.

| ID | User story | Priority |
| --- | --- | --- |
| FR-01 | As a quality engineer, I drop an email or PDF into an inbox folder and the system picks it up within 1 minute | Must |
| FR-02 | As the system, I extract text from PDF and email bodies, including attachments | Must |
| FR-03 | As the system, I extract 10 fields into fixed JSON: order number, part number, supplier, defect type, defect description, quantity affected, severity, detected at (station), detected on (date), reporter | Must |
| FR-04 | As the system, I give a confidence score per field and per report | Must |
| FR-05 | As the system, I check order, part and supplier against master data and flag mismatches | Must |
| FR-06 | As the system, I send a report to human review when confidence is below threshold, a check fails, or severity is critical | Must |
| FR-07 | As a quality engineer, I see the original report next to the extracted fields, edit them, then approve or reject | Must |
| FR-08 | As the system, I post approved tickets to the QM API and store the returned ticket ID | Must |
| FR-09 | As the system, I never create the same ticket twice for the same report | Must |
| FR-10 | As the system, I log every input, model call, decision and posting | Must |
| FR-11 | As a UiPath bot, I enter approved tickets into a legacy web form with no API | Should |
| FR-12 | As a quality manager, I see volumes, defect types, processing time and accuracy on a dashboard | Should |
| FR-13 | As a developer, I run the evaluation set with one command and get an accuracy report | Must |
| FR-14 | As the system, I detect duplicate reports about the same defect and link them | Could |
| FR-15 | As a quality engineer, I get an email summary of open review items each morning | Could |

## Non-functional requirements

| ID | Area | Target |
| --- | --- | --- |
| NFR-01 | Speed | One report processed in under 30 seconds, review excluded |
| NFR-02 | Volume | 500 reports a day on a MacBook |
| NFR-03 | Reliability | Failed steps retry 3 times with backoff, then land in an error queue. No report gets lost |
| NFR-04 | Idempotency | Reprocessing a report creates no second ticket |
| NFR-05 | Cost | Under 0.01 EUR in model calls per report, tracked per run |
| NFR-06 | Maintainability | Modules separated, settings in config, unit tests on validation, routing and posting |
| NFR-07 | Portability | Runs on macOS with Python 3.11 or newer after one pip install. Docker comes in phase 2 |
| NFR-08 | Observability | Structured JSON logs, a run ID on every record, metrics on the dashboard |
| NFR-09 | Security | No secrets in code or Git. Personal names masked in logs |
| NFR-10 | Model swap | Changing the LLM provider means changing config, not code |

## Input data and sample data plan

We have no real factory data, so we generate a realistic synthetic set. Every record stays fictional: no real Siemens Energy orders, suppliers or names.

**Master data (CSV, seeded into PostgreSQL)**

- 50 production orders for switchgear bays, with customer project, bay type and due date
- 80 part numbers: breakers, disconnectors, current transformers, enclosures, gaskets, bolts
- 15 suppliers with country and contact role
- 12 test stations: incoming inspection, assembly, gas leak test, high voltage test, final inspection

**Report set (30 documents)**

- 20 plain-text emails and 10 .eml files, all in English
- Mix of clean and messy cases: missing order numbers, wrong part numbers, two defects in one email, forwarded chains, signatures
- 3 to 4 critical severity cases
- PDFs and German reports come in phase 2

**Evaluation set.** 20 of the 30 reports get labelled answers in a JSON file. We never use them for prompt tuning, only for scoring.

**How we generate it.** A Python script builds master data with Faker. Gemini writes each report from one master data record, so we know the correct answers up front. You check 10 by hand for realism.

## Architecture and tech stack

The 6-hour build runs as plain Python on your Mac: one pipeline command, a FastAPI mock and a Streamlit app. Each step reads from and writes to SQLite, so any step can restart without losing work.

&#91;embedded content: report flow · 7 steps, 1 decision\]

Low-confidence, failed or critical reports go to a person. Clean reports post straight to the QM API, In phase 2, a UiPath bot copies approved tickets into the legacy form.

| Layer | Choice | Why |
| --- | --- | --- |
| Language | Python 3.12 | Matches the job post, rich PDF and AI libraries |
| Ingestion | A run command over an input folder (watcher and IMAP in phase 2) | Simple, testable, no paid services |
| Parsing | Python email library (pdfplumber in phase 2) | Handles text and table PDFs |
| LLM | Gemini API free tier, behind one interface so the model can change later | You already know Gemini from Talk to Your Data |
| Structured output | Pydantic models + JSON schema mode | Rejects malformed answers before they reach the database |
| APIs | FastAPI for the pipeline API and the mock QM system | Auto docs, typed, fast to build |
| Queue | SQLite table with status column | Avoids extra infrastructure in v1 |
| Database | SQLite now, PostgreSQL 16 in phase 2 | No setup on a Mac. PostgreSQL later shows your strongest skill |
| Review UI | Streamlit | Fast to build, you know it |
| RPA | Phase 2: UiPath Studio Web or Studio on a Windows VM, with REFramework | Shows the exact tool the role asks for |
| Dashboard | Streamlit page now, Power BI in phase 2 (needs Windows) | One stack to run |
| Testing | pytest, pytest-cov, a custom eval script | Unit, integration and accuracy tests |
| Delivery | Public GitHub repo, ruff for linting. Docker and CI in phase 2 | Clean, public code from the first commit |

## Data model and API contracts

Five working tables and three master tables carry the whole flow, stored in one SQLite file for the 6-hour build. The `reports` table doubles as the work queue through its status column.

| Table | Key columns | Purpose |
| --- | --- | --- |
| reports | id, source\_type, file\_hash (unique), raw\_text, status, received\_at | One row per incoming document. The file hash blocks duplicates |
| extractions | id, report\_id, fields\_json, confidence\_json, model, prompt\_version, cost\_eur | Every model answer, kept for audit and evaluation |
| validations | id, extraction\_id, check\_name, passed, message | One row per rule result |
| reviews | id, report\_id, reviewer, action, edited\_fields\_json, reviewed\_at | Human decisions and edits |
| tickets | id, report\_id (unique), qm\_ticket\_id, posted\_via, posted\_at | The unique report\_id makes posting idempotent |
| master\_orders, master\_parts, master\_suppliers | IDs and attributes | Reference data for validation |

**Report status flow:** received, parsed, extracted, validated, in\_review, approved, rejected, posted, failed.

**Extraction schema (Pydantic)**

```json
{
  "order_number": "SG-2026-0412",
  "part_number": "CB-145-3150",
  "supplier": "Example Castings GmbH",
  "defect_type": "gas_leak",
  "defect_description": "Leak at flange seal during gas test",
  "quantity_affected": 2,
  "severity": "critical",
  "detected_at": "gas_leak_test",
  "detected_on": "2026-11-03",
  "reporter": "masked",
  "confidence": {"order_number": 0.97, "severity": 0.81}
}
```

**Mock QM API (FastAPI)**

| Method | Endpoint | Behaviour |
| --- | --- | --- |
| POST | /api/v1/notifications | Creates a ticket. Requires an `Idempotency-Key` header. Same key returns the same ticket |
| GET | /api/v1/notifications/{id} | Returns one ticket |
| GET | /api/v1/health | Health check |

The mock adds realistic trouble on purpose: 5% random 503 errors, 1 second latency spikes and a 60 calls per minute rate limit. That lets us prove the retry logic works.

**Legacy web form.** A small HTML form served by the mock, with no API. Only the UiPath bot uses it.

## AI design and guardrails

The LLM does one job: read text and fill a schema. Python code makes every decision about validity, routing and posting.

**Extraction step**

1. Clean the text: strip signatures, disclaimers and quoted replies.
2. Mask personal names and emails before the model sees them.
3. Send one prompt with the schema, 5 worked examples and the allowed values for defect type, severity and station.
4. Ask for JSON only, with a confidence value per field.
5. Parse with Pydantic. On a schema error, retry once with the error message. On a second failure, send to review.

**Routing rules (in code, not in the prompt)**

| Condition | Route |
| --- | --- |
| Severity is critical | Human review, always |
| Any required field missing or below 0.85 confidence | Human review |
| Order, part or supplier not found in master data | Human review with the mismatch shown |
| Part does not belong to the order's bill of materials | Human review |
| All checks pass, severity minor or major | Auto-approve and post |

The 0.85 threshold is a starting value. We tune it on the evaluation set before go-live.

**Guardrails**

- The agent can only create tickets. It cannot edit or close them, and it never touches other systems
- Prompts live in versioned files. Every extraction stores its prompt version and model name
- A daily cost cap stops model calls and alerts us if spend passes the limit
- Report text goes in a delimited block, and the prompt tells the model to ignore instructions inside it. This limits prompt injection from email content

**Why this counts as an agent.** The system observes an inbox, decides which tools to call (parser, model, validators, QM API, UiPath queue) and acts, with a person in the loop for risky cases. We keep the control flow in code so it stays testable.

## Security and compliance

We build it as if a real factory would run it, even with fake data.

- **Secrets.** API keys live in a `.env` file kept out of Git, loaded through pydantic-settings. The UiPath bot reads its login from an Orchestrator credential asset
- **Accounts.** The bot uses its own service account on the mock systems, never a personal login
- **Personal data.** We mask reporter names and emails before model calls and in logs. The database keeps the original only in the reports table
- **Model provider.** We send only masked text. The README notes that a real deployment needs a company-approved model or a private endpoint
- **Access.** The review screen requires a login. Reviewers can approve and edit, nobody can delete history
- **Audit trail.** Every model call, rule result, human action and posting gets a timestamp and a run ID
- **Works council note.** The dashboard reports on defects and processing time per report. It shows no per-person speed or volume. A German factory would need works council sign-off before tracking individuals
- **Retention.** Raw reports older than 90 days get purged by a scheduled job. Tickets and audit rows stay

## Testing and evaluation

A green pipeline run proves nothing about the data. We test the code and score the AI separately.

| Test type | What it covers | Tool |
| --- | --- | --- |
| Unit | Parsers, masking, validators, routing rules, idempotency key logic | pytest |
| Integration | Full flow from inbox file to posted ticket against the mock QM API | pytest with the mock API running locally |
| Failure | 503 errors, timeouts, rate limits, bad JSON from the model, a crash mid-run | pytest with fault injection in the mock |
| Idempotency | Same file dropped twice, a rerun after a crash. Expect exactly one ticket | pytest |
| AI evaluation | Field accuracy, exact match per field, critical recall, review rate, cost per report | Custom eval script on the 20 labelled reports |
| RPA | Bot handles a slow page, a missing field and a changed button label | Phase 2, together with the bot |
| Acceptance | You run 5 unseen reports through the system and sign off | Demo session |

**Key AI metric.** Critical recall must be 100%: the system can never auto-post a critical defect. A missed minor field costs little. A missed critical case breaks trust.

The eval script prints a table per run and saves it, so we can compare prompt versions and models over time.

## Milestones and plan

We build the core in one 6-hour session, split into 6 one-hour blocks. You can run them in one day or across two evenings. I write the code, you run it, test it and push it to GitHub.

| Hour | What we build | Done when |
| --- | --- | --- |
| 1 | Repo, virtual env, `.env` with the Gemini key, fake master data, 30 reports, 20 labelled answers | Data files exist and load into SQLite |
| 2 | Gemini extraction into the Pydantic schema, with confidence scores and name masking | 5 sample reports extract to valid JSON |
| 3 | Validation rules, routing rules, report status flow, duplicate protection | Critical and broken reports land in review |
| 4 | Mock QM API in FastAPI with random errors, posting with retries and idempotency keys | Same report posted twice gives one ticket |
| 5 | Streamlit review screen (approve, edit, reject) and a metrics page | You approve a flagged report and it posts |
| 6 | Eval script, unit tests on the core rules, README with the diagram, push to GitHub | Accuracy report printed, repo public |

Phase 2 (UiPath bot, PDFs, PostgreSQL, Docker, CI, Power BI) gets planned after the core works.

## Risks, assumptions and open questions

**Risks**

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Synthetic reports look too clean, so accuracy looks better than reality | High | Add messy cases on purpose, review 30 samples by hand |
| LLM output changes between model versions | Medium | Pin the model version, rerun eval on every change |
| UiPath Studio desktop and Power BI Desktop need Windows, and you work on a Mac | Medium | Build the Python API path first. In phase 2, test UiPath Studio Web or use a Windows VM |
| Free API quota runs out during eval runs | Low | Cache model answers by file hash and prompt version |
| 6 hours runs out before the core works | Medium | Each hour has a done check. If one runs over, we cut the metrics page first |

**Assumptions**

- You build on a MacBook with Python 3.11 or newer
- A free Gemini API key from Google AI Studio covers the volume
- You have about 6 hours in total, with me writing most of the code
- All data stays fake, so the free tier's data terms are fine

**Client decisions**

| Question | Decision |
| --- | --- |
| Language | English only for now |
| Model | Gemini, because the free tier covers it |
| Dashboard | Streamlit first, Power BI later on a Windows machine |
| Repo | Public on GitHub from day one |
| Timeline | One 6-hour build for the core, phase 2 later |

## Acceptance criteria and definition of done

You sign off v1 when every box below is ticked in the demo.

- [ ] After `pip install -r requirements.txt`, the project runs on a clean Mac in under 10 minutes
- [ ] One run command turns every report in the input folder into a posted ticket or a review item
- [ ] Field accuracy reaches 90% or more on the 20-report evaluation set
- [ ] No critical defect gets auto-posted (100% critical recall)
- [ ] Running the same folder twice creates no duplicate tickets
- [ ] Posting survives the mock API's random errors through retries
- [ ] The Streamlit screen lets you approve, edit and reject flagged reports
- [ ] The metrics page shows volume, defect types, review rate and accuracy
- [ ] Unit tests pass on validation, routing and posting
- [ ] The README covers setup, architecture, design choices and the phase 2 list

**Definition of done for each task:** code reviewed, tests passing, docs updated, merged to main.
