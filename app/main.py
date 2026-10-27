"""Streamlit chat: question -> Gemini picks a tool -> BigQuery -> table.

Run:  streamlit run app/main.py
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


def cost_label(d) -> str:
    tokens = f"tokeny: vstup {d.input_tokens}, výstup {d.output_tokens}, thinking {d.thinking_tokens}"
    rate = usd_czk()
    if rate is None:
        return f"{tokens} · ≈ ${d.cost_usd:.6f} (kurz ČNB nedostupný)"
    czk, day = rate
    return f"{tokens} · ≈ {d.cost_usd * czk * 100:.4f} haléře (${d.cost_usd:.6f}, kurz ČNB {czk:.3f} ze dne {day})"

st.set_page_config(page_title="films", layout="wide")
st.title("films")
st.caption(f"Model `{MODEL}` vybere funkci, data se načtou z BigQuery a modelu se neposílají.")

if "history" not in st.session_state:
    st.session_state.history = []


def show(entry: dict) -> None:
    with st.chat_message("user"):
        st.write(entry["question"])
    with st.chat_message("assistant"):
        d = entry["decision"]
        st.caption(cost_label(d))
        if d.tool is None:
            st.write(d.text)
            return
        st.code(f"{d.tool}({', '.join(f'{k}={v!r}' for k, v in d.args.items())})", language="python")
        if entry.get("error"):
            st.error(entry["error"])
        else:
            st.dataframe(entry["result"], hide_index=True)


for entry in st.session_state.history:
    show(entry)

if question := st.chat_input("Např. najdi mi nějaké komedie"):
    entry = {"question": question, "decision": decide(question)}
    if entry["decision"].tool:
        try:
            entry["result"] = run_tool(entry["decision"].tool, entry["decision"].args)
        except Exception as e:  # show tool errors in the chat instead of crashing the app
            entry["error"] = f"{type(e).__name__}: {e}"
    st.session_state.history.append(entry)
    show(entry)

total_usd = sum(e["decision"].cost_usd for e in st.session_state.history)
rate = usd_czk()
st.sidebar.metric("Útrata za tuto session", f"{total_usd * rate[0]:.4f} Kč" if rate else f"${total_usd:.6f}")
st.sidebar.caption(f"{len(st.session_state.history)} dotazů, odhad podle ceníku v agent.py")
