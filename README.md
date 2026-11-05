# doc-qa

Ask questions about your own documents with Claude. Every question emits a
trace you can inspect in Arize Phoenix, a local trace viewer.

## Setup (once)

You need Python 3.10 or newer and an Anthropic API key from console.anthropic.com.

### Windows (PowerShell)

```powershell
cd doc-qa
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

Run `phoenix serve` in one PowerShell window and `python app.py --demo` in a
second one (activate `.venv` and set the key in each). If PowerShell blocks the
activate script, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

### Mac or Linux

```bash
cd doc-qa
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

## Run it

Terminal 1, start the trace viewer:

```bash
source .venv/bin/activate
phoenix serve
```

Terminal 2, ask questions:

```bash
source .venv/bin/activate
export ANTHROPIC_API_KEY=sk-ant-...
python app.py --demo                       # five sample questions
python app.py "What are the four eval types?"
python app.py                              # interactive chat
```

Then open http://localhost:6006 and click the **doc-qa** project.

### Chat in the browser

On Windows, put your keys in a `.env` file (one `NAME=value` per line:
`ANTHROPIC_API_KEY`, `ARIZE_SPACE_ID`, `ARIZE_API_KEY`) and double-click
`start.bat`. Anywhere else:

```bash
streamlit run chat.py
```

Opens a chat page at http://localhost:8501 with the sample questions in the
sidebar. Each browser tab is one trace session.

## What each trace shows

```
ask  (CHAIN)              question in, answer out, flags in Metadata
├── retrieve  (RETRIEVER)     the 5 chunks pulled, with scores, source file and section
├── build_prompt  (CHAIN)     the exact excerpts sent to Claude
└── messages.create  (LLM)    the Claude call: full prompt, response, tokens, cost, latency
```

The `ask` span carries metadata you can filter on in Phoenix:

| Flag | Meaning |
|---|---|
| `weak_retrieval` | Top retrieval score was low. The docs probably do not cover the question. |
| `no_citations` | The answer cited no excerpts. Check it for ungrounded claims. |
| `top_retrieval_score` | How strongly the best chunk matched. |
| `sources` | Which documents fed the answer. |

The fifth demo question, "What is the capital of France?", is deliberately off topic.
Its trace should show `weak_retrieval: true` and an answer that declines.

Each run of `app.py` is grouped as one **session** in Phoenix, so an
interactive chat appears as a single conversation.

## Adding documents

Drop `.docx`, `.md` or `.txt` files into `docs/` and run again. The index is
rebuilt on every start, so there is nothing else to do.

## Settings (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_MODEL` | `claude-sonnet-5-5` | Which Claude model answers |
| `TOP_K` | `5` | How many chunks are sent to Claude |
| `PHOENIX_PROJECT` | `doc-qa` | Project name in Phoenix |
| `DOCS_DIR` | `./docs` | Folder to index |
| `PHOENIX_COLLECTOR_ENDPOINT` | `http://localhost:6006` | Send traces to a hosted Phoenix instead |
| `ARIZE_SPACE_ID` + `ARIZE_API_KEY` | unset | When both are set, traces go to Arize AX instead of Phoenix (no `phoenix serve` needed) |

## How it works

Retrieval uses BM25 keyword search, so it runs with no embedding model or
extra API key. Documents are split at their headings, and long sections are
cut into roughly 220-word chunks with overlap. Swapping in vector embeddings
later only changes `DocQA.retrieve`; the trace structure stays the same.
