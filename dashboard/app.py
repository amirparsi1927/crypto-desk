# dashboard/app.py — live dashboard van je crypto-desk (Streamlit)
import json
from pathlib import Path

import pandas as pd
import streamlit as st

START_KAPITAAL = 1000.0
STATE = Path(__file__).resolve().parent.parent / "desk_state.json"

st.set_page_config(page_title="Crypto Desk", page_icon="📊", layout="wide")
st.title("📊 Crypto Desk")

if not STATE.exists():
    st.info("Nog geen gegevens. Wacht tot de desk zijn eerste ronde op GitHub heeft gedraaid.")
    st.stop()

s = json.loads(STATE.read_text())
st.caption(f"Laatste ronde: {s.get('laatste_ronde', '–')} · Fear & Greed: {s.get('fear_greed', '–')} · "
           "💡 papiergeld, geen echte trades")

if s.get("gestopt"):
    st.error("🚨 Noodknop actief: de desk koopt niets meer en heeft alles verkocht.")

# ---- kerncijfers ----
waarde = s.get("waarde", s["kas"])
journaal = pd.DataFrame(s.get("journaal", []))
k1, k2, k3, k4 = st.columns(4)
k1.metric("Totale waarde", f"${waarde:,.2f}", f"{(waarde / START_KAPITAAL - 1) * 100:+.2f}% sinds start")
k2.metric("Kas", f"${s['kas']:,.2f}")
k3.metric("Open posities", len(s["posities"]))
if len(journaal):
    k4.metric("Afgesloten trades", len(journaal),
              f"winrate {(journaal.winst > 0).mean() * 100:.0f}% · ${journaal.winst.sum():+,.2f}", delta_color="off")
else:
    k4.metric("Afgesloten trades", 0)

# ---- waarde door de tijd ----
st.subheader("📈 Waarde van de desk")
historie = pd.DataFrame(s.get("historie", []))
if len(historie) > 1:
    historie["tijd"] = pd.to_datetime(historie["tijd"].str.replace(" UTC", ""), utc=True).dt.tz_convert("Europe/Amsterdam")
    st.line_chart(historie.set_index("tijd")["waarde"], y_label="$")
else:
    st.write("De grafiek verschijnt na een paar rondes.")

# ---- wat de bots deze ronde besloten ----
st.subheader("🤖 Laatste ronde: wat elke bot besloot")
for b in s.get("laatste_besluiten", []):
    kleur = {"KOOP": "🟢", "VERKOOP": "🔴"}.get(b["besluit"], "⚪")
    with st.container(border=True):
        st.markdown(f"**{kleur} {b['munt']}** · ${b['prijs']:,.2f} · eindbesluit **{b['besluit']}**")
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(f"**1 · Analist**  \n{b['regime']} · RSI {b['rsi']}")
        c2.markdown(f"**2 · Grok** ({b['grok']:+d})  \n{b['grok_reden']}")
        c3.markdown(f"**3 · Head trader**  \n{b['trader']}: {b['trader_reden']}")
        c4.markdown(f"**4 · Risico**  \n{b['besluit']}: {b['risk_reden']}")

# ---- open posities ----
st.subheader("💼 Open posities")
if s["posities"]:
    prijzen = s.get("prijzen", {})
    rijen = []
    for munt, p in s["posities"].items():
        nu = prijzen.get(munt, p["instap"])
        rijen.append({"Munt": munt, "Gekocht op": p["tijd"], "Instap": round(p["instap"], 2),
                      "Nu": nu, "Stop-loss": round(p["stop"], 2),
                      "Waarde": round(p["aantal"] * nu, 2),
                      "Resultaat %": round((nu / p["instap"] - 1) * 100, 2)})
    st.dataframe(pd.DataFrame(rijen), hide_index=True, use_container_width=True)
else:
    st.write("Geen open posities: de desk wacht op een goed moment.")

# ---- journaal ----
st.subheader("📒 Journaal (afgesloten trades)")
if len(journaal):
    st.dataframe(journaal.iloc[::-1], hide_index=True, use_container_width=True)
    st.write("Winst per marktregime:")
    st.dataframe(journaal.groupby("regime")["winst"].agg(aantal="count", winst="sum").round(2),
                 use_container_width=True)
else:
    st.write("Nog geen afgesloten trades.")
