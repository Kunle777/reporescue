# RepoRescue

### Find. Fix. Recover.

**RepoRescue is an AI-powered repository investigation workbench that helps developers understand unfamiliar codebases, investigate software failures, identify potential fixes, and validate results with execution evidence.**

Instead of relying on guesswork, RepoRescue brings repository context, AI-assisted investigation, guarded patching, and independent test execution into one workflow.

<p align="center">
  <a href="https://reporescue-frontend.vercel.app/"><strong>Explore RepoRescue →</strong></a>
</p>

---

## The Problem

Debugging software is more than finding an error message.

Developers often need to understand an unfamiliar codebase, trace the source of a failure, identify the files involved, evaluate possible solutions, and determine whether a proposed fix actually works.

This process can be time-consuming, especially when working with repositories that have little documentation or complex dependencies.

RepoRescue aims to make that investigation process more structured, explainable, and evidence-driven.

## What RepoRescue Does

RepoRescue connects AI-assisted reasoning with repository inspection and controlled execution.

Its investigation workflow is designed to help developers:

- Investigate public GitHub repositories.
- Understand code structure and relevant implementation details.
- Diagnose potential bugs and software failures.
- Explore explanations of unfamiliar code.
- Examine repository health and potential problems.
- Review proposed changes without blindly applying them.
- Run independent validation and inspect execution results.
- Keep investigation findings and supporting evidence together.

## Investigation Modes

RepoRescue provides different investigation experiences for different developer needs.

| Mode | Purpose |
|---|---|
| **General** | General-purpose repository investigation and understanding. |
| **Debug** | Focused investigation of bugs, failures, and unexpected behaviour. |
| **Explain** | Helps developers understand repository code and implementation concepts. |
| **Health** | Examines repository health and identifies potential concerns. |
| **Conversation** | Supports follow-up discussion grounded in an existing investigation and its available evidence. |

The available depth of analysis and execution depends on the selected mode, repository language, and supported runtime.

## How It Works

A typical RepoRescue investigation follows a structured workflow:

1. **Select a repository** – Provide a public GitHub repository URL or choose a registered repository.
2. **Choose an investigation mode** – Select the type of analysis that matches your objective.
3. **Describe the problem** – Explain what you want to investigate or understand.
4. **Analyse repository context** – RepoRescue retrieves relevant repository information for the investigation.
5. **Generate findings** – The AI-assisted workflow examines the available evidence and produces investigation results.
6. **Review potential fixes** – Where supported, proposed changes are subject to patch safeguards.
7. **Validate results** – Independent execution and test results help establish whether a proposed solution is supported by evidence.

The goal is not simply to generate a plausible answer. It is to help developers distinguish between an explanation, a proposed change, and a result that has actually been verified.

## Security and Reliability

Repository investigation involves executing code and processing potentially untrusted content. RepoRescue is designed with safeguards around these operations.

Key design considerations include:

- **Public repository scope:** The current product is designed for public GitHub repositories.
- **Controlled execution:** Repository execution is handled through an isolated execution environment rather than unrestricted host execution.
- **Guarded patches:** Proposed changes are subject to validation and safeguards rather than being blindly pushed into a repository.
- **Independent verification:** Test execution results are kept distinct from AI-generated explanations.
- **Request limits:** AI-powered operations are subject to usage limits to help control resource consumption.
- **Idempotency and reliability:** Request-handling safeguards help reduce duplicate operations.
- **Sensitive-data protection:** Provider-bound data and diagnostic information are subject to redaction safeguards.

These safeguards are intended to make repository investigation more controlled and trustworthy.

## Technology Stack

RepoRescue uses a modern web application and Python backend architecture.

| Layer | Technology |
|---|---|
| Frontend | React, TypeScript, Vite |
| Backend API | Python, FastAPI |
| AI integration | Meta AI API |
| Repository operations | Git |
| Database | PostgreSQL, Supabase |
| ORM and migrations | SQLAlchemy, Alembic |
| Background processing | Worker-based job processing |
| Execution | Docker-based isolated execution |
| Frontend hosting | Vercel |
| Backend hosting | Railway |

## Architecture Overview

At a high level, RepoRescue separates the user interface, API, investigation orchestration, and execution responsibilities.

```text
Developer
    |
    v
React + TypeScript Frontend
    |
    v
FastAPI Backend
    |
    +------ PostgreSQL / Supabase
    |
    +------ Investigation Orchestrator
                |
                +------ Repository Context
                |
                +------ Meta AI Integration
                |
                +------ Patch Guard
                |
                +------ Background Worker
                            |
                            v
                    Isolated Execution
                            |
                            v
                    Test Results / Evidence
                            |
                            v
                    Investigation Report
```

This separation allows investigation requests, AI processing, repository operations, and execution results to be handled through distinct components.

## Getting Started

### Requirements

Before running RepoRescue locally, ensure you have:

- Python installed.
- Node.js and npm installed.
- Git installed.
- Docker installed and running for supported execution workflows.
- Access to a PostgreSQL database.
- A Meta API key for AI-powered functionality.

### 1. Clone the repository

```bash
git clone https://github.com/Kunle777/reporescue.git
cd reporescue
```

### 2. Set up the backend

```bash
cd reporescue/backend

python -m venv .venv
```

Activate the virtual environment.

**Windows / Git Bash:**

```bash
source .venv/Scripts/activate
```

**Linux / macOS:**

```bash
source .venv/bin/activate
```

Install the backend dependencies:

```bash
pip install -r requirements.txt
```

Configure the backend environment using the project's environment-variable configuration.

At minimum, the backend requires the appropriate database connection and Meta API credentials. Consult the backend configuration and deployment documentation for the complete variable list.

### 3. Set up the frontend

Open another terminal:

```bash
cd reporescue/reporescue/frontend
npm install
```

Start the development server:

```bash
npm run dev
```

### 4. Run the backend

From the backend directory, use the project's configured FastAPI application entry point to start the API server.

For example, where the application entry point is `app.main`:

```bash
uvicorn app.main:app --reload --port 8000
```

Configure the frontend API base URL to point to your local backend.

**Note:** Background workers, database migrations, and Docker execution must also be configured according to the project's deployment and environment setup.

---

## Current Scope and Limitations

RepoRescue is actively evolving. Its capabilities depend on the selected investigation mode and the repository being analysed.

Current product boundaries include:

- Private repository access is not part of the current public-repository workflow.
- Execution support depends on the repository language and available runtime.
- Not every investigation mode performs the same depth of code execution or testing.
- AI-generated findings and proposed fixes should be reviewed rather than treated as guaranteed correct.
- A successful explanation is not equivalent to a verified software fix.
- Independent test results are only as comprehensive as the tests and execution environment available.

The project prioritises transparent investigation results and evidence over unsupported claims of automatic bug fixing.

## Development

RepoRescue was developed during the **Meta Global AI Developer Hackathon**, with an initial product build completed in 10 days.

The project explores how AI can assist developers beyond code generation by combining repository understanding, structured investigation, guarded changes, and execution-based validation.

## Contributing

Contributions, feedback, and suggestions are welcome.

If you find a bug or have an idea that could improve RepoRescue:

1. Explore the application.
2. Check existing GitHub issues.
3. Open an issue describing the problem or suggestion.
4. For code contributions, submit a pull request with a clear explanation of the changes.

Please include reproduction steps and relevant evidence when reporting bugs.

## Links

- **Live application:** https://reporescue-frontend.vercel.app/
- **GitHub repository:** https://github.com/Kunle777/reporescue

---

## Built for Developers

RepoRescue is built around a simple idea:

**Understanding a problem is the first step. Verifying the solution is what matters.**

Explore RepoRescue and make your next repository investigation more structured.

**Find. Fix. Recover.**