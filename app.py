"""Web page: streamlit run app.py"""

import os
from dataclasses import asdict

import streamlit as st
from dotenv import load_dotenv

load_dotenv()
try:  # on Streamlit Community Cloud the key lives in st.secrets
    if "GEMINI_API_KEY" in st.secrets:
        os.environ["GEMINI_API_KEY"] = st.secrets["GEMINI_API_KEY"]
except Exception:
    pass

from opener import OpenerError, generate  # noqa: E402

MAX_RUNS_PER_SESSION = 15  # keeps one visitor from burning the shared free quota

st.set_page_config(page_title="Cold Opener", page_icon="✉️")
st.title("Cold Opener")
st.caption("Paste a company's website. Get a 3-line opener for a cold email to its founder, "
           "built only from what the site says. It drafts; it never sends.")


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def cached_generate(url: str, pitch: str) -> dict:
    return asdict(generate(url, pitch=pitch, use_cache=False))


with st.form("f"):
    url = st.text_input("Company website", placeholder="example.com")
    pitch = st.text_input("What you're offering (optional)", placeholder="e.g. we translate marketing sites into Spanish and German")
    go = st.form_submit_button("Draft opener", type="primary")

if go:
    st.session_state.runs = st.session_state.get("runs", 0) + 1
    if st.session_state.runs > MAX_RUNS_PER_SESSION:
        st.error("Demo limit reached for this session. Clone the repo to run it with your own free key.")
        st.stop()
    with st.spinner("Reading the site and drafting..."):
        try:
            r = cached_generate(url, pitch)
        except OpenerError as e:
            st.error(str(e))
            st.stop()
        except Exception as e:  # model quota, network, etc.
            st.error(f"Something went wrong: {e.__class__.__name__}: {e}")
            st.stop()

    greeting = f"Hi {r['founder'].split()[0]}," if r["founder"] else "Hi there,"
    st.subheader("Opener")
    st.code(greeting + "\n\n" + "\n".join(r["lines"]), language=None, wrap_lines=True)

    for w in r["warnings"]:
        st.warning(w)

    with st.expander("Why these lines? (evidence from the site)"):
        for i, (line, ev) in enumerate(zip(r["lines"], r["evidence"]), 1):
            st.markdown(f"**Line {i}:** {line}")
            st.markdown(f"> {ev}" if ev else "> _(no quote needed)_")
        st.markdown("**Pages read:** " + ", ".join(r["pages"]))
        u = r["usage"]
        st.caption(f"Model: {r['model']} · attempts: {r['attempts']} · "
                   f"tokens in/out: {u.get('input_tokens')}/{u.get('output_tokens')}")
