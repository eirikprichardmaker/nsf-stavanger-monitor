"""
NSF Stavanger – møtedokumentmonitor
Kjøres daglig av GitHub Actions. Sender e-post til eirikprichard@gmail.com
når nye dokumenter er lagt ut for overvåkede utvalg.
"""

import json
import os
import smtplib
import tempfile
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import anthropic
import pdfplumber
import requests

# ---------------------------------------------------------------------------
# Konfigurasjon
# ---------------------------------------------------------------------------
GRAPHQL_URL = "https://stavanger-elm.digdem.no/graphql"
BASE_URL = "https://stavanger-elm.digdem.no"

# Serveren sjekker nøyaktig disse headerne + at query bruker fragment-syntaks
HEADERS = {
    "Content-Type": "application/json",
    "apollo-require-preflight": "true",
    "accept": "*/*",
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua": '"HeadlessChrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
    "sec-ch-ua-mobile": "?0",
    "referer": "https://stavanger-elm.digdem.no/motekalender",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "HeadlessChrome/147.0.7727.15 Safari/537.36"
    ),
}

COMMISSIONS = {
    "cm8sijrej00pg0pyv76z9ebvf": "Utvalg for helse og velferd",
    "cm8sijsed017s0pyvatd8dhga": "Eldrerådet",
    "cm8sijsa9015v0pyv6fe4c88j": "Funksjonshemmedes råd",
}

KEYWORDS = [
    # Bemanning
    "bemanning", "turnusplan", "stillingsbrøk", "deltid", "heltid",
    "vikarbyrå", "overtid", "bemanningsplan",
    # Økonomi/kutt
    "budsjettkutt", "effektivisering", "omstilling", "nedbemanning",
    "innsparing", "budsjettreduksjon", "kutt",
    # Organisering
    "omorganisering", "privatisering", "konkurranseutsetting",
    "anbudsrunde", "outsourcing",
    # Arbeidsforhold
    "arbeidstid", "nattevakt", "vaktbelastning", "arbeidsmiljø",
    "arbeidsbelastning", "arbeidsvilkår",
    # Tariff/lønn
    "lønnsoppgjør", "tariff", "lønntillegg", "likelønn", "lønn",
    # Tjenester
    "hjemmesykepleie", "sykehjem", "BPA", "legevakt",
    "psykisk helse", "rus", "hjemmetjeneste", "omsorgstjeneste",
    # Medbestemmelse
    "høring", "partssamarbeid", "AMU", "tillitsvalgt",
    "medbestemmelse", "drøfting", "høringsinnspill",
]

STATE_FILE = Path("seen_documents.json")
LOOKBACK_DAYS = 7    # sjekk møter fra siste uke (for å fange opp nettopp publiserte)
LOOKAHEAD_DAYS = 90  # sjekk møter i de neste 90 dagene


# ---------------------------------------------------------------------------
# GraphQL-hjelper
# ---------------------------------------------------------------------------
def gql(query: str, variables: dict = None, operation: str = None) -> dict:
    body = {"query": query}
    if variables:
        body["variables"] = variables
    if operation:
        body["operationName"] = operation
    resp = requests.post(GRAPHQL_URL, json=body, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data and not data.get("data"):
        raise RuntimeError(f"GraphQL feil: {data['errors']}")
    return data.get("data", {})


# ---------------------------------------------------------------------------
# Hent møter
# ---------------------------------------------------------------------------
_YEAR_MEETINGS_QUERY = """query GetYearMeetings($where: MeetingListWhere) {
  meetings(where: $where, orderBy: date_ASC) {
    ...YearMeeting
    __typename
  }
}

fragment YearMeeting on Meeting {
  id
  date
  endDateTime
  internalStatus
  status
  meetingName
  published
  commission {
    id
    __typename
  }
  __typename
}"""


def get_upcoming_meetings() -> list:
    now = datetime.now(timezone.utc)
    date_gt = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    date_lt = (now + timedelta(days=LOOKAHEAD_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    data = gql(
        _YEAR_MEETINGS_QUERY,
        variables={"where": {"searchTerm": "", "date_gt": date_gt, "date_lt": date_lt}},
        operation="GetYearMeetings",
    )
    meetings = data.get("meetings", [])
    return [
        m for m in meetings
        if m["commission"]["id"] in COMMISSIONS and m.get("published")
    ]


_MODAL_MEETING_QUERY = """query ModalMeeting($id: ID!) {
  meeting(where: {id: $id}) {
    id
    date
    location
    synced
    mergedPdf {
      id
      url
      archiveId
      error
      inProgress
      __typename
    }
    commission {
      id
      name
      __typename
    }
    documents(orderBy: order_ASC) {
      id
      title
      type
      azureCustomLink
      classified
      registrationCode
      journalArchiveId
      __typename
    }
    proceedings {
      ...ModalMeetingProceeding
      __typename
    }
    streams {
      id
      title
      url
      status
      startTime
      receivingVideo
      __typename
    }
    __typename
  }
}

fragment ModalMeetingProceeding on Proceeding {
  id
  title
  sequenceNumber
  classified
  type
  order
  agendaOrder
  agendaGroupType
  decisionProposal {
    id
    HTMLDocumentId
    markStats {
      type
      count
      parentChanged
      __typename
    }
    __typename
  }
  documents {
    id
    title
    azureCustomLink
    classified
    registrationCode
    type
    __typename
  }
  publicProposalsCount
  relatedProceedings {
    id
    sequenceNumber
    meeting {
      id
      date
      commission {
        id
        name
        __typename
      }
      __typename
    }
    protocol {
      id
      azureCustomLink
      __typename
    }
    __typename
  }
  __typename
}"""


def get_meeting_details(meeting_id: str) -> dict:
    data = gql(
        _MODAL_MEETING_QUERY,
        variables={"id": meeting_id},
        operation="ModalMeeting",
    )
    return data.get("meeting")


# ---------------------------------------------------------------------------
# PDF-nedlasting og tekstutvinning
# ---------------------------------------------------------------------------
def download_pdf(doc_id: str) -> bytes:
    url = f"{BASE_URL}/api/doc/{doc_id}"
    resp = requests.get(
        url,
        headers={"User-Agent": HEADERS["User-Agent"]},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content


def extract_text(pdf_bytes: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        tmp_path = f.name
    try:
        with pdfplumber.open(tmp_path) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        text = "\n".join(pages)
        return text[:20000]
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Claude-analyse
# ---------------------------------------------------------------------------
def analyse_document(title: str, text: str, commission_name: str) -> tuple[str, list[str]]:
    """Returnerer (analyse_tekst, liste_av_funne_nøkkelord)."""
    found = [kw for kw in KEYWORDS if kw.lower() in text.lower()]

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = f"""Du er assistent for en hovedtillitsvalgt (HTV) i Norsk Sykepleierforbund (NSF) i Stavanger kommune.

Utvalg: {commission_name}
Dokument: {title}
NSF-relevante nøkkelord funnet: {", ".join(found) if found else "ingen"}

Saksdokument:
{text}

Gi en analyse i to deler:
1. SAMMENDRAG: 2-3 setninger om hva saken handler om.
2. KONSEKVENS: Hva betyr dette i praksis for sykepleiere og ansatte i helse og velferd i Stavanger?

Vær konkret og kortfattet. Maks 200 ord totalt."""

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text, found


def assess_action(title: str, text: str, keywords: list[str], commission_name: str) -> str:
    """Vurderer om HTV bør handle, kun for flaggede saker."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = f"""Du er HTV for NSF i Stavanger. Saken «{title}» i {commission_name} inneholder disse NSF-relevante nøkkelordene: {", ".join(keywords)}.

Bør du som HTV gjøre noe konkret? (f.eks. sende høringssvar, kontakte leder, møte opp, varsle NSF-tillitsvalgte)
Svar i maks 2 setninger. Hvis ingen umiddelbar handling er nødvendig, skriv «Ingen umiddelbar handling nødvendig.»

Relevante utdrag fra dokumentet:
{text[:4000]}"""

    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


# ---------------------------------------------------------------------------
# E-post
# ---------------------------------------------------------------------------
def send_email(meeting: dict, new_proceedings: list) -> None:
    gmail_user = os.environ["GMAIL_USER"]
    gmail_password = os.environ["GMAIL_APP_PASSWORD"]

    commission_name = COMMISSIONS.get(meeting["commission"]["id"], "Ukjent utvalg")
    date_utc = datetime.fromisoformat(meeting["date"].replace("Z", "+00:00"))
    # Norsk dato, ingen tidssone-konvertering (møtetid er lokal tid i kilden)
    MONTHS_NO = [
        "januar", "februar", "mars", "april", "mai", "juni",
        "juli", "august", "september", "oktober", "november", "desember",
    ]
    date_str = f"{date_utc.day}. {MONTHS_NO[date_utc.month - 1]} {date_utc.year}"

    subject = f"[NSF-monitor] Nye dokumenter – {commission_name}, {date_str}"

    flagged = [(p, info) for p, info in new_proceedings if info.get("keywords")]
    unflagged = [(p, info) for p, info in new_proceedings if not info.get("keywords")]

    lines = [
        f"Nye saksdokumenter er lagt ut for møte i {commission_name}.",
        f"Møtedato: {date_str}",
        f"Sted: {meeting.get('location') or 'Ikke oppgitt'}",
        "",
    ]

    if flagged:
        lines += [
            "=" * 60,
            "FLAGGEDE SAKER (krever din vurdering som HTV/NSF):",
            "=" * 60,
        ]
        for proc, info in flagged:
            lines.append(f"\n⚠  Sak {proc['sequenceNumber']}: {proc['title']}")
            lines.append(f"   Flagget for: {', '.join(info['keywords'])}")
            if info.get("analysis"):
                lines.append("")
                lines.append(info["analysis"])
            if info.get("action"):
                lines.append("")
                lines.append(f"Handlingsanbefaling: {info['action']}")
            lines.append("")
            lines.append("Dokumenter:")
            for doc in info.get("docs", []):
                lines.append(f"  • {doc['title']}")
                lines.append(f"    {BASE_URL}/api/doc/{doc['id']}")
            lines.append("")

    if unflagged:
        lines += [
            "=" * 60,
            "ØVRIGE SAKER (ingen NSF-nøkkelord funnet):",
            "=" * 60,
        ]
        for proc, info in unflagged:
            lines.append(f"• Sak {proc['sequenceNumber']}: {proc['title']}")
            for doc in info.get("docs", []):
                lines.append(f"  → {BASE_URL}/api/doc/{doc['id']}")
        lines.append("")

    body = "\n".join(lines)

    msg = MIMEMultipart()
    msg["From"] = gmail_user
    msg["To"] = gmail_user
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(gmail_user, gmail_password)
        smtp.send_message(msg)

    print(f"E-post sendt: {subject}")


# ---------------------------------------------------------------------------
# Tilstandshåndtering
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Hovedløkke
# ---------------------------------------------------------------------------
def main() -> None:
    state = load_state()
    meetings = get_upcoming_meetings()
    print(f"Fant {len(meetings)} kommende/nylige møter for overvåkede utvalg")

    for meeting in meetings:
        meeting_id = meeting["id"]
        commission_name = COMMISSIONS.get(meeting["commission"]["id"], "")
        print(f"\nSjekker {commission_name} – møte {meeting_id}")

        details = get_meeting_details(meeting_id)
        if not details:
            print("  Ingen detaljer tilgjengelig – hopper over")
            continue

        seen_docs: set = set(state.get(meeting_id, []))
        new_proceedings = []
        all_new_doc_ids = []

        for proc in details.get("proceedings", []):
            if proc.get("classified"):
                continue

            new_docs = [
                d for d in proc.get("documents", [])
                if d["id"] not in seen_docs and not d.get("classified")
            ]
            if not new_docs:
                continue

            print(f"  Ny(e) dokument(er) i sak {proc['sequenceNumber']}: {proc['title']}")

            combined_text = ""
            for doc in new_docs:
                try:
                    pdf_bytes = download_pdf(doc["id"])
                    text = extract_text(pdf_bytes)
                    combined_text += f"\n\n--- {doc['title']} ---\n{text}"
                except Exception as e:
                    print(f"    Klarte ikke laste ned {doc['id']}: {e}")

            info: dict = {"docs": new_docs, "keywords": [], "analysis": None, "action": None}

            if combined_text.strip():
                try:
                    analysis, keywords = analyse_document(
                        proc["title"], combined_text, commission_name
                    )
                    info["keywords"] = keywords
                    info["analysis"] = analysis
                    if keywords:
                        info["action"] = assess_action(
                            proc["title"], combined_text, keywords, commission_name
                        )
                except Exception as e:
                    print(f"    Analyse feilet: {e}")

            new_proceedings.append((proc, info))
            all_new_doc_ids.extend(d["id"] for d in new_docs)

        if new_proceedings:
            try:
                send_email(details, new_proceedings)
            except Exception as e:
                print(f"  E-post feilet: {e}")
            state.setdefault(meeting_id, [])
            state[meeting_id] = list(set(state[meeting_id]) | set(all_new_doc_ids))
        else:
            print("  Ingen nye dokumenter")
            if meeting_id not in state:
                state[meeting_id] = []

    save_state(state)
    print("\nFerdig. Tilstand lagret.")


if __name__ == "__main__":
    main()
