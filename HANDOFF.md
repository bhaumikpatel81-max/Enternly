# Building Enternly in VS Code with Claude — a step-by-step guide

This guide takes you from the prototype zip to a working application on your own machine, using VS Code and Claude as your coding partner, so you can test everything yourself before handing it to your developer. It assumes no prior coding experience. Take it slowly; each step is small.

## The big picture

You will install three things (VS Code, Docker, and Claude Code), open the project, let Claude read this entire plan, and then work through the remaining build one piece at a time — testing as you go. Claude does the typing; you make the decisions and run the tests.

## Part 1 — Install your tools

Install **VS Code** from code.visualstudio.com. This is the editor where everything happens.

Install **Docker Desktop** from docker.com. This runs the database and the app in self-contained boxes so you don't have to install Postgres or Python by hand. After installing, open Docker Desktop once and leave it running in the background.

Install **Claude Code**, Anthropic's coding tool that works inside VS Code. The current way to add it is from the Extensions panel in VS Code (the squares icon in the left sidebar) — search for "Claude" and install the official Anthropic extension, then sign in with your Claude account. Claude Code can read your whole project, write and edit files, and run commands for you, all from inside the editor.

## Part 2 — Open the project

Unzip the `enternly-ats` (or `one-click-hire`) folder somewhere easy to find, like your Desktop. In VS Code, choose File → Open Folder and pick that unzipped folder. You'll see the file tree on the left: `backend`, `frontend`, `database`, and the docs.

## Part 3 — Run the prototype to confirm it works

Open VS Code's built-in terminal (Terminal → New Terminal). Type this one line and press enter:

```
docker compose -f docker-compose.prod.yml --profile prod up --build
```

The first run takes a few minutes while it downloads and builds. When it settles, it will show an address. Open that address in your browser and you'll see the Enternly dashboard. Submit a test application, run the bot round, advance a candidate, look at the reports. This confirms the foundation works on your machine. To stop it, click in the terminal and press Ctrl+C.

## Part 4 — Bring Claude up to speed

This is the key step you asked about. Claude Code starts fresh and does not automatically know our long planning conversation. You give it the context by pointing it at the docs already in this project. In the Claude panel inside VS Code, paste a message like this:

> Read README.md, ARCHITECTURE.md, and HANDOFF.md in this project, then look through the backend, frontend, and database folders. This is "Enternly", an automated recruitment system for EnternsTech. The full design rationale is in those docs. Once you've read everything, summarise the current state back to me and propose the next build step.

Claude will read the files and give you a summary. If its summary matches what you expect, you know it has the context and you can start directing it. If you have the text of our planning conversation saved, you can also paste the key decisions in — but the three docs already capture the architecture, the config-driven principle, the assistive-bot rule, and the human-gate safety requirements, so they are usually enough.

## Part 5 — Build the remaining pieces, one at a time

The prototype runs the whole pipeline with stubbed external connections. The remaining work is replacing those stubs with real integrations. Direct Claude through them in this order, testing after each one before moving on. Do not do them all at once — one working integration beats four half-broken ones.

First, fill in the real configuration: ask Claude to help you replace the sample approval chains, email templates, and scoring weights in the seed with EnternsTech's real values. This is your domain knowledge; Claude just helps you put it in the right place.

Second, the Google Calendar and Meet integration, since auto-scheduling removes the biggest pain. Tell Claude: "Replace the schedule_meeting stub in connectors.py with a real Google Calendar API call that checks the panel's free/busy and creates an event with a Meet link." Claude will tell you what Google credentials it needs; you get those from your Google Workspace admin.

Third, Gmail for candidate and panel emails, by replacing the send_email stub the same way.

Fourth, the AI screening and the interview bot, by replacing the ai_screen and run_bot_interview stubs. This is where you decide, with your developer and legal, whether to use a managed AI service or a model on a company server. Keep the rule we built in: the bot scores and ranks, but a human always makes the advance/reject decision.

Fifth and last, the Darwinbox integration, by replacing the push_offer_to_darwin stub — you only need this once a candidate reaches the offer stage.

## Part 6 — Test like a recruiter, not like a programmer

After each change, run the app again with the Docker command and use the dashboard the way a recruiter would. Submit a real-looking application. Try to break it. If something is wrong, tell Claude exactly what you saw ("I submitted an application and the score didn't appear") and it will fix it. You are the quality check; you don't need to read the code to know whether it behaves correctly.

## Part 7 — Hand it to your developer

When the integrations work and you've piloted one real requisition, your developer takes over for hardening and deployment: securing the credentials properly, handling errors and edge cases, and deploying through your company pipeline. Everything you and Claude built is real, standard code in the layout the deployment pipeline expects, so the developer builds on it rather than starting over. Point them to ARCHITECTURE.md first.

## A few honest reminders

Work in small steps and test constantly — this is how non-programmers successfully build with Claude. When something breaks, the fix is usually one message away; describe what you saw plainly. Keep backups by copying the folder before big changes, or better, ask Claude to set up Git so you can undo anything. And protect the two safety rules as the system grows: a human makes every reject decision, and proctoring or recording waits for legal sign-off. Those aren't technical niceties — they're what keep the hiring process fair and you protected.
