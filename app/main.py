"""Streamlit chat: question -> Gemini picks a tool -> BigQuery -> table.

Run:  python -m streamlit run app/main.py
"""

import urllib.request

import streamlit as st

from agent import MODEL, decide
from tools import run_tool

CNB_RATES = "https://www.cnb.cz/cs/financni-trhy/devizovy-trh/kurzy-devizoveho-trhu/kurzy-devizoveho-trhu/denni_kurz.txt"


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


def cost_caption(d) -> None:
    """Short line under the answer, full breakdown in the tooltip."""
    total = d.input_tokens + d.output_tokens + d.thinking_tokens
    rate = usd_czk()
    fx = f"CNB rate {rate[0]:.3f} USD/CZK ({rate[1]})" if rate else "CNB rate unavailable"
    details = (
        f"input {d.input_tokens} · output {d.output_tokens} · thinking {d.thinking_tokens}  \n"
        f"${d.cost_usd:.6f} · {fx}  \n"
        "Estimate from the price list in agent.py."
    )
    st.caption(f"{total} tokens · {price(d.cost_usd)}", help=details)


def show_answer(entry: dict) -> None:
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
    cost_caption(d)


st.set_page_config(page_title="Film Agent", page_icon="🎬", layout="wide")

if "history" not in st.session_state:
    st.session_state.history = []

# Header stays on top while scrolling: Streamlit has no sticky option, so CSS on the
# container's key class. 3.75rem = height of Streamlit's own top toolbar.
background = "#0e1117" if st.context.theme.type == "dark" else "#ffffff"
st.markdown(f"""<style>
.st-key-header {{ position: sticky; top: 3.75rem; z-index: 999; background: {background};
                  padding: 0.5rem 0; border-bottom: 1px solid rgba(128, 128, 128, 0.2); }}
</style>""", unsafe_allow_html=True)

with st.container(key="header"):
    head, side = st.columns([6, 1], vertical_alignment="center")
    head.markdown(f"## 🎬 Film Agent\nPowered by `{MODEL}`")
    session = side.popover("💰 Session cost", use_container_width=True)  # filled at the end, after the new answer

for entry in st.session_state.history:
    with st.chat_message("user"):
        st.write(entry["question"])
    with st.chat_message("assistant"):
        show_answer(entry)

if question := st.chat_input("e.g. new horror films in cinemas"):
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
        show_answer(entry)
    st.session_state.history.append(entry)

with session:
    h = st.session_state.history
    st.metric("Total session cost", price(sum(e["decision"].cost_usd for e in h)))
    st.caption(f"{len(h)} queries · "
               f"{sum(e['decision'].input_tokens + e['decision'].output_tokens + e['decision'].thinking_tokens for e in h)} tokens")
