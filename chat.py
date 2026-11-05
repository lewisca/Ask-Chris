"""Browser chat UI for doc-qa. Run: streamlit run chat.py"""
import os
import uuid

import streamlit as st
from openinference.instrumentation import using_session

from app import DEMO_QUESTIONS, MODEL, DocQA  # importing app also sets up tracing

st.set_page_config(page_title="Ask Chris", page_icon="💬")
if not os.getenv("ANTHROPIC_API_KEY"):
    st.error("ANTHROPIC_API_KEY is not set. Set it in the terminal, then restart `streamlit run chat.py`.")
    st.stop()


@st.cache_resource
def get_qa() -> DocQA:
    return DocQA()  # index built once per server start


qa = get_qa()
if "session" not in st.session_state:
    st.session_state.session = str(uuid.uuid4())  # one trace session per browser tab
    st.session_state.messages = []

with st.sidebar:
    st.title("Ask Chris")
    st.caption(f"{len(qa.chunks)} chunks indexed · {MODEL}")
    st.write("Try one:")
    clicked = next((q for q in DEMO_QUESTIONS if st.button(q, width="stretch")), None)
    if st.button("New chat", type="primary"):
        st.session_state.clear()
        st.rerun()

for m in st.session_state.messages:
    st.chat_message(m["role"]).markdown(m["content"])

if question := st.chat_input("Ask about your documents") or clicked:
    st.session_state.messages.append({"role": "user", "content": question})
    st.chat_message("user").markdown(question)
    with st.chat_message("assistant"), st.spinner("Searching the docs..."):
        with using_session(st.session_state.session):
            answer = qa.ask(question)
        st.markdown(answer)
    st.session_state.messages.append({"role": "assistant", "content": answer})
