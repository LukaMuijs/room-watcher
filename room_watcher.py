#!/usr/bin/env python3
# ROOM.nl watcher: stuurt een mail zodra er een nieuwe kamer/woning in Delft
# verschijnt met een huur van maximaal MAX_PER_PERSON euro per persoon.
#
# Meldingen gaan als GitHub-issue (GitHub mailt je daarover). Wil je liever zelf
# mailen via SMTP, zet dan SMTP_USER en SMTP_PASSWORD.
#
# Instellingen via omgevingsvariabelen:
#   GITHUB_TOKEN    Wordt door GitHub Actions automatisch ingevuld
#   SMTP_USER       Mailadres waarmee de mail verstuurd wordt
#   SMTP_PASSWORD   App-wachtwoord van dat account (NIET je gewone wachtwoord)
#   MAIL_TO         Ontvanger(s), komma-gescheiden. Standaard: lukamuijs@outlook.com
#   CITY            Standaard: Delft
#   MAX_PER_PERSON  Standaard: 650
#   SMTP_HOST       Standaard: smtp-mail.outlook.com (Gmail: smtp.gmail.com)
#   SMTP_PORT       Standaard: 587 (Gmail: 465)
#
# Gebruik:
#   python room_watcher.py            # één keer checken (voor cron / GitHub Actions)
#   python room_watcher.py --loop 300 # blijven draaien, elke 300 seconden checken
#   python room_watcher.py --debug    # laat de ruwe velden van één advertentie zien
#
import json
import os
import re
import smtplib
import sys
import time
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path

API_URL = "https://www.roommatch.nl/portal/object/frontend/getallobjects/format/json"
DETAIL_URL = "https://www.roommatch.nl/aanbod/studentenwoningen/details/{}"
SEEN_FILE = Path(__file__).with_name("seen.json")

MAIL_TO = os.environ.get("MAIL_TO") or "lukamuijs@outlook.com"
CITY = os.environ.get("CITY", "Delft")
MAX_PER_PERSON = float(os.environ.get("MAX_PER_PERSON", "650"))


def pick(obj, *paths):
    # Geef de eerste niet-lege waarde terug voor een lijst mogelijke veldnamen ("a.b" = genest).
    for path in paths:
        value = obj
        for key in path.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        if value not in (None, "", []):
            return value
    return None


def to_float(value):
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def fetch_listings():
    req = urllib.request.Request(
        API_URL,
        headers={"User-Agent": "Mozilla/5.0 (room-watcher)", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    return data.get("result", data) if isinstance(data, dict) else data


def city_of(obj):
    return str(pick(obj, "city.name", "gemeenteGeoLocatieNaam", "city", "plaats") or "")


def rent_of(obj):
    return to_float(pick(obj, "totalRent", "totaleHuur", "grossRent", "netRent", "nettohuur"))


def persons_of(obj):
    # Aantal personen voor deze woning. Valt terug op 1 als het niet te bepalen is.
    n = pick(obj, "maxPersons", "maximumAantalPersonen", "aantalPersonen",
             "maxHouseholdSize", "huishoudgrootteMax", "maximumHouseholdSize")
    n = to_float(n)
    if n and n >= 1:
        return int(n)
    # Anders zoeken in type/omschrijving, bv. "2-persoons" of "voor 3 personen"
    text = " ".join(str(pick(obj, p) or "") for p in
                    ("dwellingType.localizedName", "dwellingType.name", "objectType",
                     "description", "descriptionMarkdown", "omschrijving"))
    m = re.search(r"(\d+)\s*-?\s*(?:personen|persoons|persons|people)", text, re.I)
    return int(m.group(1)) if m else 1


def listing_id(obj):
    return str(pick(obj, "id", "urlKey", "objectId"))


def describe(obj):
    rent = rent_of(obj)
    persons = persons_of(obj)
    address = " ".join(str(pick(obj, p) or "") for p in ("street", "houseNumber", "houseNumberAddition")).strip()
    kind = pick(obj, "dwellingType.localizedName", "dwellingType.name", "objectType") or "Woning"
    area = pick(obj, "areaDwelling", "oppervlakte")
    closing = pick(obj, "closingDate", "sluitingsdatum")
    url_key = pick(obj, "urlKey")
    url = DETAIL_URL.format(url_key) if url_key else "https://www.roommatch.nl/aanbod/studentenwoningen"
    lines = [
        f"{kind} - {address}, {city_of(obj)}",
        f"Huur totaal: €{rent:.2f}  |  Personen: {persons}  |  Per persoon: €{rent / persons:.2f}",
    ]
    if area:
        lines.append(f"Oppervlakte: {area} m²")
    if closing:
        lines.append(f"Reageren kan tot: {closing}")
    lines.append(url)
    return "\n".join(lines)


def matches(obj):
    if city_of(obj).strip().lower() != CITY.lower():
        return False
    rent = rent_of(obj)
    return rent is not None and rent / persons_of(obj) <= MAX_PER_PERSON


def send_mail(subject, body):
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    recipients = [a.strip() for a in MAIL_TO.split(",") if a.strip()]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    host = os.environ.get("SMTP_HOST") or "smtp-mail.outlook.com"
    port = int(os.environ.get("SMTP_PORT") or "587")
    if port == 465:
        smtp = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        smtp = smtplib.SMTP(host, port, timeout=30)
        smtp.starttls()
    with smtp:
        smtp.login(user, password)
        smtp.sendmail(user, recipients, msg.as_string())


def create_issue(subject, body):
    repo = os.environ["GITHUB_REPOSITORY"]
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    data = json.dumps({"title": subject, "body": f"@{owner}\n\n{body}"}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    urllib.request.urlopen(req, timeout=30).close()


def notify(subject, body):
    if os.environ.get("SMTP_USER"):
        send_mail(subject, body)
    else:
        create_issue(subject, body)


def load_seen():
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def check_once():
    listings = fetch_listings()
    seen = load_seen()
    new = [o for o in listings if matches(o) and listing_id(o) not in seen]

    # Overzicht in de log, zodat je kunt controleren of huur en personen goed gelezen worden
    for o in listings:
        if city_of(o).strip().lower() == CITY.lower():
            rent = rent_of(o)
            print(f"  {pick(o, 'street')} {pick(o, 'houseNumber')}: huur {rent}, "
                  f"personen {persons_of(o)}, past {'JA' if matches(o) else 'nee'}")

    if new:
        body = f"{len(new)} nieuwe woning(en) op ROOM in {CITY} (max €{MAX_PER_PERSON:.0f} p.p.):\n\n"
        body += "\n\n".join(describe(o) for o in new)
        notify(f"ROOM: {len(new)} nieuwe woning(en) in {CITY}", body)
        print(body)
    else:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {len(listings)} advertenties, niks nieuws.")

    # Alleen verzonden matches als 'gezien' opslaan, zodat een advertentie die later
    # in prijs zakt alsnog gemeld wordt.
    seen.update(listing_id(o) for o in new)
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=0))


def main():
    args = sys.argv[1:]
    if "--debug" in args:
        listings = fetch_listings()
        print(f"{len(listings)} advertenties. Voorbeeld van de ruwe velden:")
        print(json.dumps(listings[0], indent=2, ensure_ascii=False)[:5000])
        return
    if "--loop" in args:
        interval = int(args[args.index("--loop") + 1]) if len(args) > args.index("--loop") + 1 else 300
        while True:
            try:
                check_once()
            except Exception as e:  # blijf draaien bij tijdelijke fouten
                print(f"Fout: {e}", file=sys.stderr)
            time.sleep(interval)
    check_once()


if __name__ == "__main__":
    main()
