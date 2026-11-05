"""
doc-qa: ask questions about your own documents with Claude, and watch every
step as a trace in Arize Phoenix.

Each question produces one trace:

    ask  (CHAIN)            question in, answer out, flags
    ├── retrieve  (RETRIEVER)   which chunks were pulled, with scores
    ├── build_prompt  (CHAIN)   the exact context sent to the model
    └── Messages  (LLM)         the Claude call: prompt, response, tokens, latency

Usage:
    python app.py                       # interactive chat (one Phoenix session)
    python app.py "your question"       # one question, one trace
    python app.py --demo                # runs a few sample questions
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from anthropic import Anthropic
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from openinference.instrumentation import using_session
from openinference.instrumentation.anthropic import AnthropicInstrumentor
from openinference.semconv.trace import (
    DocumentAttributes,
    OpenInferenceSpanKindValues,
    SpanAttributes,
)
from opentelemetry.trace import Status, StatusCode
from rank_bm25 import BM25Okapi

# ---------------------------------------------------------------- settings
DOCS_DIR = Path(os.getenv("DOCS_DIR", Path(__file__).parent / "docs"))
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
PROJECT = os.getenv("PHOENIX_PROJECT", "doc-qa")
TOP_K = int(os.getenv("TOP_K", "5"))
CHUNK_WORDS = 220          # target chunk size
OVERLAP_WORDS = 40         # words carried into the next chunk of a long section
LOW_SCORE = 2.0            # top BM25 score below this = weak retrieval (flagged on the trace)

# ---------------------------------------------------------------- tracing
if os.getenv("ARIZE_SPACE_ID") and os.getenv("ARIZE_API_KEY"):
    from arize.otel import register as arize_register
    tracer_provider = arize_register(
        space_id=os.environ["ARIZE_SPACE_ID"],
        api_key=os.environ["ARIZE_API_KEY"],
        project_name=PROJECT,
        verbose=False,
    )
else:
    from phoenix.otel import register  # slow (~1 min): pulls in the whole Phoenix server package
    tracer_provider = register(project_name=PROJECT, verbose=False)
AnthropicInstrumentor().instrument(tracer_provider=tracer_provider)  # auto LLM spans
tracer = tracer_provider.get_tracer("doc-qa")

KIND = SpanAttributes.OPENINFERENCE_SPAN_KIND


# ---------------------------------------------------------------- ingestion
@dataclass
class Chunk:
    id: str
    source: str
    section: str
    text: str
    tokens: list[str] = field(default_factory=list)


def _is_heading(p: Paragraph) -> bool:
    text = p.text.strip()
    if not text or len(text) > 90:
        return False
    if p.style is not None and p.style.name.lower().startswith(("heading", "title")):
        return True
    runs = [r for r in p.runs if r.text.strip()]
    return bool(runs) and all(r.bold for r in runs)  # a short, fully bold line


def _blocks(doc: Document):
    """Yield paragraphs and tables in the order they appear in the document."""
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            yield Paragraph(child, doc)
        elif tag == "tbl":
            yield Table(child, doc)


def _table_text(t: Table) -> str:
    rows = []
    for row in t.rows:
        cells, seen = [], set()
        for c in row.cells:  # merged cells repeat; keep each once
            if id(c._tc) in seen:
                continue
            seen.add(id(c._tc))
            cells.append(" ".join(c.text.split()))
        rows.append(" | ".join(cells))
    return "\n".join(rows)


def _sections_from_docx(path: Path):
    doc = Document(path)
    section, buf = path.stem, []
    for block in _blocks(doc):
        if isinstance(block, Paragraph):
            if _is_heading(block):
                if buf:
                    yield section, "\n".join(buf)
                section, buf = block.text.strip(), []
            elif block.text.strip():
                buf.append(block.text.strip())
        else:
            buf.append(_table_text(block))
    if buf:
        yield section, "\n".join(buf)


def _sections_from_text(path: Path):
    section, buf = path.stem, []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            if buf:
                yield section, "\n".join(buf)
            section, buf = line.lstrip("# ").strip(), []
        elif line.strip():
            buf.append(line.strip())
    if buf:
        yield section, "\n".join(buf)


STOPWORDS = set("""a an and are as at be by can do does for from how i if in is it its me my of on or
should so that the this to was we what when where which who why will with you your""".split())


def tokenize(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS]


def _doc_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in {".docx", ".md", ".txt"})


def _sections(path: Path):
    return (_sections_from_docx if path.suffix.lower() == ".docx" else _sections_from_text)(path)


def load_toolkit(folder: Path) -> dict[str, str]:
    """Docs in docs/coaching are sent whole with every question instead of being searched."""
    return {
        p.stem.strip(): re.sub(r"_{5,}", "", "\n".join(f"{s}\n{t}" for s, t in _sections(p)))
        for p in _doc_files(folder)
    }


def load_chunks(docs_dir: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    files = _doc_files(docs_dir)
    if not files:
        sys.exit(f"No .docx, .md or .txt files found in {docs_dir}")
    for path in files:
        for section, text in _sections(path):
            words = text.split()
            step = CHUNK_WORDS - OVERLAP_WORDS
            for i, start in enumerate(range(0, max(len(words), 1), step)):
                piece = " ".join(words[start : start + CHUNK_WORDS])
                if not piece:
                    continue
                cid = f"{path.stem}::{len(chunks):03d}"
                # The section name is indexed too, so headings help retrieval.
                chunks.append(Chunk(cid, path.name, section, piece, tokenize(section + " " + piece)))
                if start + CHUNK_WORDS >= len(words):
                    break
    return chunks


# ---------------------------------------------------------------- the pipeline
SYSTEM_PROMPT = """You answer questions about how Chris Lewis works, usually asked by recruiters and hiring managers, using only the documents provided.

You have two kinds of material:
- Chris's coaching toolkit, included in full below: the ACT Model (coaching one person), the ACT Group-Team Coaching Model, and Chris's Coaching Question Cards.
- Excerpts retrieved for the current question, numbered [1], [2], and so on.

Rules:
- Use only this material. If it does not contain the answer, say so plainly and do not guess. If the question has nothing to do with Chris's work, decline briefly.
- Cite inline: excerpts like [2] or [1][4], toolkit documents by name in brackets, like [Coaching Question Cards].
- When the question is about coaching, developing people or leading a team, show how Chris would actually coach it: name the ACT step (Awareness, Acceptance, Choice, Trust the Choice) or influence area (Mental, Emotional, Physical, Environmental, Social, Spiritual) that fits, and quote 1-3 matching questions from the Coaching Question Cards. Use the ACT Group-Team Coaching Model and the (Team) cards for team situations, and the ACT Model for one person.
- If the question is too vague to answer well or could mean several things, do not guess. Ask one short clarifying question in Chris's coaching style, adapted from the Coaching Question Cards (for example "What do you mean when you say ...?"), and stop there.
- Be concise and practical. Keep the documents' own terms and frameworks."""


class DocQA:
    def __init__(self, docs_dir: Path = DOCS_DIR):
        self.chunks = load_chunks(docs_dir)
        self.bm25 = BM25Okapi([c.tokens for c in self.chunks])
        self.toolkit = load_toolkit(docs_dir / "coaching")
        self.system = SYSTEM_PROMPT + "".join(
            f'\n\n<toolkit name="{name}">\n{text}\n</toolkit>' for name, text in self.toolkit.items()
        )
        self.client = Anthropic()

    def retrieve(self, question: str, k: int = TOP_K) -> list[tuple[Chunk, float]]:
        with tracer.start_as_current_span("retrieve") as span:
            span.set_attribute(KIND, OpenInferenceSpanKindValues.RETRIEVER.value)
            span.set_attribute(SpanAttributes.INPUT_VALUE, question)
            scores = self.bm25.get_scores(tokenize(question))
            ranked = sorted(zip(self.chunks, scores), key=lambda x: x[1], reverse=True)[:k]
            for i, (c, s) in enumerate(ranked):
                prefix = f"{SpanAttributes.RETRIEVAL_DOCUMENTS}.{i}"
                span.set_attribute(f"{prefix}.{DocumentAttributes.DOCUMENT_ID}", c.id)
                span.set_attribute(f"{prefix}.{DocumentAttributes.DOCUMENT_SCORE}", float(s))
                span.set_attribute(f"{prefix}.{DocumentAttributes.DOCUMENT_CONTENT}", c.text)
                span.set_attribute(
                    f"{prefix}.{DocumentAttributes.DOCUMENT_METADATA}",
                    json.dumps({"source": c.source, "section": c.section}),
                )
            return ranked

    def build_prompt(self, question: str, hits: list[tuple[Chunk, float]]) -> str:
        with tracer.start_as_current_span("build_prompt") as span:
            span.set_attribute(KIND, OpenInferenceSpanKindValues.CHAIN.value)
            context = "\n\n".join(
                f"[{i}] ({c.source} / {c.section})\n{c.text}" for i, (c, _) in enumerate(hits, 1)
            )
            prompt = f"<excerpts>\n{context}\n</excerpts>\n\nQuestion: {question}"
            span.set_attribute(SpanAttributes.INPUT_VALUE, question)
            span.set_attribute(SpanAttributes.OUTPUT_VALUE, prompt)
            return prompt

    def ask(self, question: str, history: list[dict] = ()) -> str:
        """history: earlier turns as {"role", "content"} dicts, so a reply to a clarifying question keeps its context."""
        history = list(history)[-6:]
        with tracer.start_as_current_span("ask") as span:
            span.set_attribute(KIND, OpenInferenceSpanKindValues.CHAIN.value)
            span.set_attribute(SpanAttributes.INPUT_VALUE, question)
            try:
                # Search with the previous question too, so a short follow-up still finds the right docs.
                prev = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
                hits = self.retrieve(f"{prev} {question}".strip())
                prompt = self.build_prompt(question, hits)
                resp = self.client.messages.create(  # traced automatically as an LLM span
                    model=MODEL,
                    max_tokens=1024,
                    # The toolkit is the same on every call, so cache it.
                    system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                    messages=history + [{"role": "user", "content": prompt}],
                )
                answer = "".join(b.text for b in resp.content if b.type == "text")

                # Flags you can filter on in Phoenix (metadata column).
                top = float(hits[0][1]) if hits else 0.0
                cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", answer)})
                cited_toolkit = [name for name in self.toolkit if f"[{name}]" in answer]
                span.set_attribute(SpanAttributes.METADATA, json.dumps({
                    "model": MODEL,
                    "top_retrieval_score": round(top, 2),
                    "weak_retrieval": top < LOW_SCORE,
                    "cited_excerpts": cited,
                    "cited_toolkit": cited_toolkit,
                    "no_citations": not cited and not cited_toolkit,
                    "sources": sorted({c.source for c, _ in hits}),
                }))
                span.set_attribute(SpanAttributes.OUTPUT_VALUE, answer)
                span.set_status(Status(StatusCode.OK))
                return answer
            except Exception as e:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                raise


# ---------------------------------------------------------------- CLI
DEMO_QUESTIONS = [
    "How does Chris take an AI product from idea to launch?",
    "What is Chris's approach to coaching a team?",
    "How does Chris decide what goes on a roadmap?",
    "How does Chris debug an AI feature that gives bad answers?",
    "What is the capital of France?",  # not in the docs: should decline, and the trace should show weak retrieval
]


def main() -> None:
    ap = argparse.ArgumentParser(description="Ask your docs; every question is traced to Phoenix.")
    ap.add_argument("question", nargs="*", help="a question (omit for interactive mode)")
    ap.add_argument("--demo", action="store_true", help="run a few sample questions")
    args = ap.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY first (see README).")

    qa = DocQA()
    print(f"Indexed {len(qa.chunks)} chunks from {DOCS_DIR}. Model: {MODEL}. Tracing to {'Arize' if os.getenv('ARIZE_SPACE_ID') and os.getenv('ARIZE_API_KEY') else 'Phoenix'} project: {PROJECT}\n")

    session = str(uuid.uuid4())
    with using_session(session):  # groups this run's traces under one session in Phoenix
        if args.demo:
            questions = DEMO_QUESTIONS
        elif args.question:
            questions = [" ".join(args.question)]
        else:
            questions = None

        if questions:
            for q in questions:
                print(f"Q: {q}\nA: {qa.ask(q)}\n")
        else:
            print("Ask a question (blank line to quit).")
            history: list[dict] = []
            while True:
                try:
                    q = input("\n> ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not q:
                    break
                a = qa.ask(q, history)
                history += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
                print("\n" + a)

    tracer_provider.force_flush()
    print("\nTraces sent. Open http://localhost:6006 to view them.")


if __name__ == "__main__":
    main()
