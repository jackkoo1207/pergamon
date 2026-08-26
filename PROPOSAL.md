# PROPOSAL — SmartReviewAgent: an agentic EU-compliance document reviewer

## 1. The problem

We aim to move from Pergamon's questionnaire-driven manual creation to an **agent-driven** manual creation.

When the user have a complicated devices, questionnaire-based prompting is hard to capture all the characteristics of the device.

It is more convinient for the user to have a draft of technical documentation for the agent to see and give feedback.

In case the user do not understand some technical details, the agent can email the department to ask for specific informations

## 2. What the app does
The agent will take the customer's draft of technical documentation and cross-check it against the applicable EU regulations

It will ask the user for missing information, if the user is not sure it can email the responsible department directly. 


## 3. How to run it
Live at **https://pergamon-production.up.railway.app** (users: `test:123456`, `test2:654321`).

Test login:
```text
User:test
password:123456
```
## 4. Architecture and key decisions

**Stack:** Python Flask (`chatbot.py`) + single-page vanilla JS UI (`page.html`), Postgres (Railway plugin), **Hermes Agent** (Nous Research) as the agent runtime, DeepSeek v4-flash as the model, Docker on Railway.

**The 3 most important decisions:**

1. **Full agent runtime instead of raw LLM calls.** Each message runs a real agent session (`hermes chat -Q` with `--resume` for memory), giving the agent terminal, file, web, and memory tools. Trade-off considered: raw LLM calls would be 10× cheaper and faster, but the whole point is an agent that *does* things — run `psql` against the regs, read uploaded files from disk, call the Gmail API via curl. We accepted latency (up to 600s on heavy docs) and solved it with **async chat jobs** (POST returns instantly, client polls `/api/chat/result`) instead of a blocking request that would 504.

2. **Multi-tenant isolation without multi-container infra.** Railway's single-service model can't run per-customer containers cheaply, so isolation is enforced *in one container at three layers*: DB schema + restricted role per user (permission refused by the database, not by the agent's manners), a real Linux account per user (0700 home, `runuser`), and per-user Hermes state. Trade-off: same-container isolation is weaker than real containers — acceptable for this stage, and each layer was verified to actually deny access (see section 5). Real sandboxing is in section 7.

3. **Regulation texts as seed data in the DB, not in the prompt.** `shared.regulations` is seeded at startup from EUR-Lex-extracted texts; the agent queries them with `psql` and cites them. Trade-off: keeps prompts small and citations checkable, but the seed is static — a regulation update means a redeploy. (EUR-Lex sync is the planned fix.)

Also notable: uploads are stored as **full file bytes in Postgres** (`documents.container`), not just paths on a Railway volume — volumes proved unreliable across redeploys; the DB is the one thing guaranteed to persist.

## 5. How you worked with AI

I built this with **Hermes Agent** (Nous Research) as my coding partner, running on DeepSeek v4-flash, in an iterative loop: high-level task → agent implements (Dockerfile, entrypoint, chatbot, OAuth, UI) → I review the diff, deploy, and exercise the app live → agent fixes what broke. I did not treat any AI output as correct until I had run it.

**Prompts/task breakdowns that worked well:**
- *"Make the chatbot agentic: full tool use + multi-turn memory via `hermes chat -Q` + `--resume`"* — one sentence that converted a static Q&A bot into a tool-using agent.
- *"Per-user DB isolation: dedicated schema `u_<user>` + restricted role, swap the agent's DATABASE_URL for the scoped one (search_path forced), revoke public — the admin URL must never reach the agent"* — the security-relevant work was specified as an invariant, not as code.
- *"Uploads must survive redeploys"* — led to the `documents.container` full-bytes design.

**Where the AI was wrong or misleading, and how I caught it:**

 - **OAuth callback crashed** — the AI generated `/oauth2callback` with missing module-level imports (`NameError: json`, `urllib.error AttributeError`). Caught because the callback 500'd during live testing; fixed with module-level imports, twice (two different import bugs in the same endpoint).

**What I verified manually before calling it done:** 

- login + session flow with the test credentials on the live URL;
- uploading a real PDF and the agent answering with a cited ✅/❌/⚠️ checklist

## 6. Honest limitations

- **Static, English-only regulation seed** — 4 texts (PPWR, LVD, Annex VII/VIII); no RoHS/WEEE/GPSR full texts; no language coverage despite the EU's 23 official languages being core to Pergamon's product.
- Database currently only have 1 **regulation** document, PPWR is nit included
## 7. What's next (one more month)

1.  **Multi-language review** — start with the 7 countries HBM serves (NL/DE/BE/AT/ES/IT/FR).
2. More regulations document

## 8. Time spent

**~24 hours over 3 days** (Aug 24–26, 2026): company investigation/vetting (~4h), deploy pipeline + Docker/entrypoint (~5h), chatbot core + agent wiring (~6h), three-layer isolation (~4h), Gmail OAuth (~3h), UI + sessions (~2h), with debugging and live verification woven throughout. Estimate from commit history (44 commits), not a timesheet.
