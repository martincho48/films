"""Streamlit chat: question -> Gemini picks a tool -> BigQuery -> table.

Run:  python -m streamlit run app/main.py
"""

import tomllib
import urllib.request
from pathlib import Path

import streamlit as st

from agent import MODEL, decide
from tools import run_tool

VERSION = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

# TMDB API terms require this notice wherever their data or images are shown
FOOTER = (
    f"Film Agent v{VERSION} · © 2026 Martin Bílek · "
    "Movie data and images from [TMDB](https://www.themoviedb.org). "
    "This product uses the TMDB API but is not endorsed or certified by TMDB. · "
    "Exchange rate: [ČNB](https://www.cnb.cz)"
)

CNB_RATES ="https://www.cnb.cz/cs/financni-trhy/devizovy-trh/kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/denni_kurz.txt"

# Same as theme.primaryColor in .streamlit/config.toml (CSS cannot read the theme)
ACCENT = "#D7263D"

# Clickable examples on the empty page: (button label, question sent to the agent)
EXAMPLES = [
    ("🎬 What's in cinemas now", "What's in cinemas now?"),
    ("🍿 Coming to cinemas next week", "What is coming to cinemas next week?"),
    ("⭐ Best rated films of all time", "Best rated films of all time"),
    ("😂 Popular comedies", "Popular comedies"),
]


@st.cache_data(ttl=6 * 3600)
def usd_czk() -> tuple[float, str] | None:
    """Today's USD/CZK from the Czech National Bank (rate, date). None if unavailable."""
    try:
        with urllib.request.urlopen(CNB_RATES, timeout=5) as r:
            lines = r.read().decode("utf-8").splitlines()
        # header "27.10.2026 #208", then "země|měna|množství|kód|kurz" rows
        for line in lines[2:]:
            country, currency, amount, code, rate = line.split("|")
            if code == "USD":
                return float(rate.replace(",", ".")) / int(amount), lines[0].split(" ")[0]
    except Exception:
        return None
    return None


def price(usd: float) -> str:
    rate = usd_czk()
    return f"{usd * rate[0]:.4f} CZK" if rate else f"${usd:.6f}"


def cost_caption(d, session_usd: float) -> None:
    """Short line under the answer incl. running session total, full breakdown in the tooltip."""
    total = d.input_tokens + d.output_tokens + d.thinking_tokens
    rate = usd_czk()
    fx = f"CNB rate {rate[0]:.3f} USD/CZK ({rate[1]})" if rate else "CNB rate unavailable"
    details = (
        f"input {d.input_tokens} · output {d.output_tokens} · thinking {d.thinking_tokens}  \n"
        f"${d.cost_usd:.6f} · {fx}"
    )
    st.caption(f"{total} tokens · {price(d.cost_usd)} · session {price(session_usd)}", help=details)


def show_answer(entry: dict, session_usd: float) -> None:
    d = entry["decision"]
    if d.tool is None:
        st.write(d.text)
    else:
        with st.expander("Call details"):
            st.code(f"{d.tool}({', '.join(f'{k}={v!r}' for k, v in d.args.items())})", language="python")
        if entry.get("error"):
            st.error(entry["error"])
        else:
            st.dataframe(entry["result"], hide_index=True)
    cost_caption(d, session_usd)


st.set_page_config(page_title="Film Agent", page_icon="🎬", layout="wide")

if "history" not in st.session_state:
    st.session_state.history = []

# One card per question + answer: rounded corners, shadow falling to the right (light from
# the left). The chat input gets the same corners and shadow plus an accent border.
st.markdown(f"""<style>
[class*="st-key-turn-"], .st-key-examples {{ border-radius: 14px; box-shadow: 6px 4px 16px rgba(0, 0, 0, 0.12); }}
.st-key-examples button {{ min-height: 5rem; }}
[data-testid="stChatInput"] {{ border: 2px solid {ACCENT}; border-radius: 14px;
                               box-shadow: 6px 4px 16px rgba(0, 0, 0, 0.12); }}
[data-testid="stChatInput"] > div {{ border-radius: 12px; }}
</style>""", unsafe_allow_html=True)

st.title("🎬 Film Agent", anchor=False)
st.markdown(f"#### Powered by `{MODEL}`")

# Example questions until the first question is asked
clicked = None
examples = st.empty()
if not st.session_state.history:
    with examples.container(border=True, key="examples"):
        st.markdown("**💡 Try a prompt**")
        st.caption("Click one to run it, or type your own question below.")
        # 2 x 2 grid of tall buttons
        for row in range(0, len(EXAMPLES), 2):
            for col, (label, q) in zip(st.columns(2), EXAMPLES[row:row + 2]):
                if col.button(label, use_container_width=True):
                    clicked = q

running_usd = 0.0  # session total up to and including each answer
for i, entry in enumerate(st.session_state.history):
    running_usd += entry["decision"].cost_usd
    with st.container(border=True, key=f"turn-{i}"):
        with st.chat_message("user"):
            st.write(entry["question"])
        with st.chat_message("assistant"):
            show_answer(entry, running_usd)

# st.bottom = area pinned to the bottom of the page; chat input first, Clear history goes below it
with st.bottom:
    typed = st.chat_input("💬 Ask anything about films… e.g. new horror films in cinemas")

if question := typed or clicked:
    examples.empty()
    with st.container(border=True, key=f"turn-{len(st.session_state.history)}"):
        with st.chat_message("user"):
            st.write(question)
        with st.chat_message("assistant"):
            with st.spinner("Thinking…"):
                entry = {"question": question, "decision": decide(question)}
            if entry["decision"].tool:
                with st.spinner("Loading from BigQuery…"):
                    try:
                        entry["result"] = run_tool(entry["decision"].tool, entry["decision"].args)
                    except Exception as e:  # show tool errors in the chat instead of crashing the app
                        entry["error"] = f"{type(e).__name__}: {e}"
            show_answer(entry, running_usd + entry["decision"].cost_usd)
    st.session_state.history.append(entry)

# At the end, so it shows up right after the first answer too
with st.bottom:
    if st.session_state.history and st.button("🗑️ Clear history"):
        st.session_state.history = []
        st.rerun()
    st.caption(FOOTER)
