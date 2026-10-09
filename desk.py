# desk.py — 6-bot crypto trading desk (papiergeld), draait elk uur via GitHub Actions
# Geheimen komen uit GitHub Secrets: XAI_API_KEY (Grok) en NTFY_TOPIC (meldingen)

import json, os, re, datetime as dt
import ccxt, pandas as pd, requests
from openai import OpenAI

grok = OpenAI(api_key=os.environ.get("XAI_API_KEY", ""), base_url="https://api.x.ai/v1")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# ⚙️ INSTELLINGEN — hier kun je aan draaien
MUNTEN           = ["BTC/USD", "ETH/USD", "SOL/USD"]
START_KAPITAAL   = 1000.0
RISICO_PER_TRADE = 0.01   # max 1% van je kapitaal verliezen per trade
MAX_POSITIE      = 0.25   # nooit meer dan 25% van je kapitaal in één munt
MAX_POSITIES     = 3
KOSTEN           = 0.004  # 0,4% beurskosten per order (aanname — check je beurs)
SLIPPAGE         = 0.001  # 0,1% slechtere prijs bij uitvoeren
DAGLIMIET        = 0.03   # 3% verlies op één dag → vandaag niets meer kopen
NOODKNOP         = 0.15   # 15% onder de hoogste stand → alles verkopen en stoppen
BESTAND          = "desk_state.json"

exchange = ccxt.kraken()


# ---------- geheugen van de desk ----------
def laad_state():
    if os.path.exists(BESTAND):
        with open(BESTAND) as f:
            return json.load(f)
    return {"kas": START_KAPITAAL, "posities": {}, "journaal": [], "piek": START_KAPITAAL,
            "dag": "", "dag_start": START_KAPITAAL, "gestopt": False}

def bewaar_state(s):
    with open(BESTAND, "w") as f:
        json.dump(s, f, indent=2)

def haal_data(munt):
    data = exchange.fetch_ohlcv(munt, timeframe="1h", limit=300)
    return pd.DataFrame(data, columns=["tijd", "open", "high", "low", "close", "volume"])

def portefeuillewaarde(s, prijzen):
    return s["kas"] + sum(p["aantal"] * prijzen.get(m, p["instap"]) for m, p in s["posities"].items())


# 🤖 BOT 1 — Marktanalist: indicatoren + marktregime
def marktanalist(df):
    c, h, l = df["close"], df["high"], df["low"]
    d = c.diff()
    rs = d.clip(lower=0).rolling(14).mean() / (-d.clip(upper=0)).rolling(14).mean()
    rsi = 100 - 100 / (1 + rs)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    a = {"prijs": float(c.iloc[-1]),
         "ma20": c.rolling(20).mean().iloc[-1],
         "ma50": c.rolling(50).mean().iloc[-1],
         "ma200": c.rolling(200).mean().iloc[-1],
         "rsi": rsi.iloc[-1],
         "atr": tr.rolling(14).mean().iloc[-1]}   # gemiddelde beweging per uur
    if a["prijs"] > a["ma200"] and a["ma50"] > a["ma200"]:
        a["regime"] = "TREND_OP"
    elif a["prijs"] < a["ma200"] and a["ma50"] < a["ma200"]:
        a["regime"] = "TREND_NEER"
    else:
        a["regime"] = "ZIJWAARTS"
    return a


# 🤖 BOT 2 — Sentiment: Grok (X + nieuws) + Fear & Greed-index
GROK_MODEL = "grok-4.6"
VRAAG = """Je bent de sentiment-analist van een crypto trading desk.
Zoek op X en in het nieuws van de afgelopen 24 uur naar de stemming over: {munten}.
Let op groot nieuws (hacks, regelgeving, ETF's, grote partijen die kopen of verkopen)
en of mensen op X extreem euforisch of juist in paniek zijn.
Geef per munt een score: 1 = duidelijk positief, 0 = neutraal of onduidelijk, -1 = duidelijk negatief.
Wees streng: geef alleen 1 of -1 bij echt duidelijk nieuws. Twijfel = 0.
Antwoord ALLEEN met JSON in precies deze vorm, zonder andere tekst:
{{"BTC/USD": {{"score": 0, "reden": "korte uitleg in het Nederlands"}}, "ETH/USD": {{...}}, "SOL/USD": {{...}}}}"""


def fear_greed():
    try:
        fg = int(requests.get("https://api.alternative.me/fng/", timeout=10).json()["data"][0]["value"])
    except Exception:
        return 0, "onbekend"
    if fg >= 75:
        return -1, f"extreme hebzucht ({fg}/100)"
    if fg <= 25:
        return 1, f"extreme angst ({fg}/100)"
    return 0, f"neutraal ({fg}/100)"


def vraag_grok():
    r = grok.responses.create(model=GROK_MODEL,
                              input=VRAAG.format(munten=", ".join(MUNTEN)),
                              tools=[{"type": "x_search"}, {"type": "web_search"}])
    data = json.loads(re.search(r"\{.*\}", r.output_text, re.S).group())
    uitkomst = {}
    for m in MUNTEN:
        d = data.get(m, {})
        uitkomst[m] = (max(-1, min(1, int(d.get("score", 0)))), d.get("reden", "geen info"))
    return uitkomst


def sentiment_bot():
    fg_score, fg_uitleg = fear_greed()
    print(f"🧠 Fear & Greed: {fg_uitleg}")
    try:
        g = vraag_grok()
    except Exception as e:
        # Geen data = neutraal. Grok mag nooit gokken zonder te zoeken.
        print(f"⚠️ Grok gaf geen bruikbaar antwoord ({type(e).__name__}: {str(e)[:120]}) → neutraal")
        g = {m: (0, "geen data") for m in MUNTEN}
    uit = {}
    for m in MUNTEN:
        score, reden = g[m]
        uit[m] = max(-1, min(1, fg_score + score))
        print(f"   🐦 Grok over {m}: {score:+d} — {reden}")
    return uit


# 🤖 BOT 3 — Head trader: kiest strategie per regime + hoe overtuigd hij is
def head_trader(munt, a, sent, s):
    heeft = munt in s["posities"]
    r = a["regime"]
    if r == "TREND_OP":
        if not heeft and a["ma20"] > a["ma50"] and a["rsi"] < 70:
            return "KOOP", (2 if sent >= 0 else 1), "meegaan met trend omhoog"
        if heeft and a["ma20"] < a["ma50"]:
            return "VERKOOP", 0, "trend verzwakt"
    elif r == "ZIJWAARTS":
        if not heeft and a["rsi"] < 30:
            return "KOOP", (2 if sent > 0 else 1), "goedkoop in zijwaartse markt"
        if heeft and a["rsi"] > 65:
            return "VERKOOP", 0, "teruggeveerd, winst pakken"
    elif r == "TREND_NEER" and heeft:
        return "VERKOOP", 0, "trend omlaag, eruit"
    return "WACHT", 0, "geen signaal"


# 🤖 BOT 4 — Risicomanager: heeft altijd het laatste woord
def risicomanager(munt, voorstel, overtuiging, a, s, waarde):
    pos = s["posities"].get(munt)
    if s["gestopt"]:
        return ("VERKOOP" if pos else "WACHT"), 0, 0, "🚨 noodknop actief"
    if pos and a["prijs"] <= pos["stop"]:
        return "VERKOOP", 0, 0, "stop-loss geraakt"
    if voorstel != "KOOP":
        return voorstel, 0, 0, "akkoord"
    if waarde < s["dag_start"] * (1 - DAGLIMIET):
        return "WACHT", 0, 0, "daglimiet bereikt, vandaag niet meer kopen"
    if len(s["posities"]) >= MAX_POSITIES:
        return "WACHT", 0, 0, f"al {MAX_POSITIES} posities open"
    stop = a["prijs"] - 2 * a["atr"]                        # stop op 2x de normale beweging
    risico_geld = waarde * RISICO_PER_TRADE * overtuiging / 2
    aantal = risico_geld / (a["prijs"] - stop)              # positiegrootte uit het risico
    aantal = min(aantal,
                 waarde * MAX_POSITIE / a["prijs"],
                 s["kas"] / (a["prijs"] * (1 + KOSTEN + SLIPPAGE)))
    if aantal * a["prijs"] < 10:
        return "WACHT", 0, 0, "positie te klein"
    return "KOOP", aantal, stop, f"akkoord: ${aantal * a['prijs']:,.2f}, stop ${stop:,.2f}"


# 🤖 BOT 5 — Uitvoerder: (nep)orders inclusief kosten en slippage
def uitvoerder(munt, besluit, aantal, stop, a, s, reden):
    nu = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    if besluit == "KOOP":
        prijs = a["prijs"] * (1 + SLIPPAGE)
        kosten = aantal * prijs * KOSTEN
        s["kas"] -= aantal * prijs + kosten
        s["posities"][munt] = {"aantal": aantal, "instap": prijs, "stop": stop, "kosten": kosten,
                               "regime": a["regime"], "tijd": nu}
    elif besluit == "VERKOOP":
        pos = s["posities"].pop(munt)
        prijs = a["prijs"] * (1 - SLIPPAGE)
        opbrengst = pos["aantal"] * prijs
        kosten = opbrengst * KOSTEN
        s["kas"] += opbrengst - kosten
        winst = opbrengst - kosten - pos["aantal"] * pos["instap"] - pos["kosten"]
        s["journaal"].append({"munt": munt, "in": pos["tijd"], "uit": nu,
                              "instap": round(pos["instap"], 2), "uitstap": round(prijs, 2),
                              "winst": round(winst, 2), "regime": pos["regime"], "reden": reden})


# 🤖 BOT 6 — Reviewer / boekhouder: journaal, prestaties, klonen-of-uitzetten
def reviewer(s, waarde):
    print("\n📒 REVIEWER")
    print(f"💼 Waarde ${waarde:,.2f} | kas ${s['kas']:,.2f} | open posities: {len(s['posities'])}")
    print(f"📈 Sinds start: {(waarde / START_KAPITAAL - 1) * 100:+.2f}% | hoogste stand ${s['piek']:,.2f}")
    if not s["journaal"]:
        print("Nog geen afgesloten trades.")
        return
    df = pd.DataFrame(s["journaal"])
    print(f"Afgesloten trades: {len(df)} | winrate {(df.winst > 0).mean() * 100:.0f}% "
          f"| totaal ${df.winst.sum():,.2f}")
    print("Winst per marktregime:")
    print(df.groupby("regime")["winst"].agg(aantal="count", winst="sum").round(2))
    if len(df) >= 20 and df.winst.sum() > 0:
        print("🧬 Advies: desk presteert goed over 20+ trades → kandidaat om te klonen")
    elif waarde < START_KAPITAAL * 0.9:
        print("⛔ Advies: meer dan 10% verlies → desk uitzetten en strategie herzien")


# 📱 Meldingen naar je telefoon (ntfy)
def meld(titel, tekst):
    print(f"📱 {titel}: {tekst}")
    if not NTFY_TOPIC:
        return
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=tekst.encode("utf-8"),
                      params={"title": titel}, timeout=10)
    except Exception as e:
        print(f"⚠️ melding mislukt: {e}")


# ---------- één handelsronde ----------
def sentiment_met_redenen():
    fg_score, fg_uitleg = fear_greed()
    print(f"🧠 Fear & Greed: {fg_uitleg}")
    try:
        g = vraag_grok()
    except Exception as e:
        # Geen data = neutraal. Grok mag nooit gokken zonder te zoeken.
        print(f"⚠️ Grok gaf geen bruikbaar antwoord ({type(e).__name__}: {str(e)[:120]}) → neutraal")
        g = {m: (0, "geen data") for m in MUNTEN}
    for m in MUNTEN:
        print(f"   🐦 Grok over {m}: {g[m][0]:+d} — {g[m][1]}")
    return fg_uitleg, {m: (max(-1, min(1, fg_score + g[m][0])), g[m][0], g[m][1]) for m in MUNTEN}


def draai_desk():
    s = laad_state()
    data = {m: marktanalist(haal_data(m)) for m in MUNTEN}
    prijzen = {m: a["prijs"] for m, a in data.items()}
    waarde = portefeuillewaarde(s, prijzen)

    vandaag = dt.date.today().isoformat()
    if s["dag"] != vandaag:
        s["dag"], s["dag_start"] = vandaag, waarde
    s["piek"] = max(s["piek"], waarde)
    if waarde < s["piek"] * (1 - NOODKNOP) and not s["gestopt"]:
        s["gestopt"] = True
        meld("🚨 NOODKNOP", f"Desk staat {NOODKNOP:.0%} onder de top (${waarde:,.2f}). Alles wordt verkocht.")

    fg_uitleg, sentimenten = sentiment_met_redenen()
    print()

    besluiten = []
    for munt, a in data.items():
        sent, grok_score, grok_reden = sentimenten[munt]
        voorstel, overtuiging, uitleg = head_trader(munt, a, sent, s)
        besluit, aantal, stop, reden = risicomanager(munt, voorstel, overtuiging, a, s, waarde)
        uitvoerder(munt, besluit, aantal, stop, a, s, reden if besluit == "VERKOOP" else uitleg)
        print(f"{munt} [{a['regime']}] ${a['prijs']:,.2f} RSI {a['rsi']:.0f} | sentiment {sent:+d}")
        print(f"   trader: {voorstel} ({uitleg}) → risk: {besluit} ({reden})")
        besluiten.append({"munt": munt, "prijs": round(a["prijs"], 2), "regime": a["regime"],
                          "rsi": round(float(a["rsi"]), 1), "grok": grok_score, "grok_reden": grok_reden,
                          "sentiment": sent, "trader": voorstel, "trader_reden": uitleg,
                          "besluit": besluit, "risk_reden": reden})
        if besluit == "KOOP":
            meld(f"🟢 KOOP {munt}", f"${aantal * a['prijs']:,.2f} op ${a['prijs']:,.2f} — {uitleg}. Stop ${stop:,.2f}")
        elif besluit == "VERKOOP":
            t = s["journaal"][-1]
            meld(f"{'✅' if t['winst'] > 0 else '🔴'} VERKOOP {munt}",
                 f"op ${t['uitstap']:,.2f} — {reden}. Resultaat ${t['winst']:+,.2f}")

    waarde = portefeuillewaarde(s, prijzen)
    s["laatste_ronde"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    s["waarde"] = round(waarde, 2)
    s["fear_greed"] = fg_uitleg
    s["laatste_besluiten"] = besluiten
    s["prijzen"] = {m: round(p, 2) for m, p in prijzen.items()}
    s.setdefault("historie", []).append({"tijd": s["laatste_ronde"], "waarde": s["waarde"]})
    s["historie"] = s["historie"][-2000:]
    reviewer(s, waarde)
    bewaar_state(s)


if __name__ == "__main__":
    try:
        draai_desk()
    except Exception as e:
        meld("⚠️ Desk-fout", f"{type(e).__name__}: {str(e)[:200]}")
        raise
def draai_desk():
    s = laad_state()
    data = {m: marktanalist(haal_data(m)) for m in MUNTEN}
    prijzen = {m: a["prijs"] for m, a in data.items()}
    waarde = portefeuillewaarde(s, prijzen)

    vandaag = dt.date.today().isoformat()
    if s["dag"] != vandaag:
        s["dag"], s["dag_start"] = vandaag, waarde
    s["piek"] = max(s["piek"], waarde)
    if waarde < s["piek"] * (1 - NOODKNOP) and not s["gestopt"]:
        s["gestopt"] = True
        meld("🚨 NOODKNOP", f"Desk staat {NOODKNOP:.0%} onder de top (${waarde:,.2f}). Alles wordt verkocht.")

    sentimenten = sentiment_bot()
    print()

    for munt, a in data.items():
        voorstel, overtuiging, uitleg = head_trader(munt, a, sentimenten[munt], s)
        besluit, aantal, stop, reden = risicomanager(munt, voorstel, overtuiging, a, s, waarde)
        uitvoerder(munt, besluit, aantal, stop, a, s, reden if besluit == "VERKOOP" else uitleg)
        print(f"{munt} [{a['regime']}] ${a['prijs']:,.2f} RSI {a['rsi']:.0f} | sentiment {sentimenten[munt]:+d}")
        print(f"   trader: {voorstel} ({uitleg}) → risk: {besluit} ({reden})")
        if besluit == "KOOP":
            meld(f"🟢 KOOP {munt}", f"${aantal * a['prijs']:,.2f} op ${a['prijs']:,.2f} — {uitleg}. Stop ${stop:,.2f}")
        elif besluit == "VERKOOP":
            t = s["journaal"][-1]
            meld(f"{'✅' if t['winst'] > 0 else '🔴'} VERKOOP {munt}",
                 f"op ${t['uitstap']:,.2f} — {reden}. Resultaat ${t['winst']:+,.2f}")

    waarde = portefeuillewaarde(s, prijzen)
    s["laatste_ronde"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    s["waarde"] = round(waarde, 2)
    reviewer(s, waarde)
    bewaar_state(s)


if __name__ == "__main__":
    try:
        draai_desk()
    except Exception as e:
        meld("⚠️ Desk-fout", f"{type(e).__name__}: {str(e)[:200]}")
        raise
