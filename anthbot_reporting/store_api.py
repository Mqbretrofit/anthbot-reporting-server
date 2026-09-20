from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
from html import escape
import json
import logging
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any, Literal
from urllib.parse import quote

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import app as core


router = APIRouter()

STORE_SCHEMA = "anthbot-community-voice-store-v1"
_CURRENCY_RE = re.compile(r"^[a-zA-Z]{3}$")
_SESSION_RE = re.compile(r"^cs_[A-Za-z0-9_]+$")
_LICENSE_RE = re.compile(r"^abv1\.([A-Za-z0-9_-]+)\.([0-9a-f]{64})$")
_PAIR_RE = re.compile(r"^abp_[A-Za-z0-9_-]{24,128}$")
_CLIENT_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,512}$")
_STRIPE_WEBHOOK_TOLERANCE_SECONDS = 300
_STORE_PAIR_TTL_SECONDS = 7 * 24 * 60 * 60
_STORE_BROWSER_COOKIE = "anthbot_voice_store_client"
_STORE_BROWSER_COOKIE_MAX_AGE = 365 * 24 * 60 * 60
_STRIPE_REFUND_RECONCILE_SECONDS = 5 * 60
_STANDARD_VOICE_PACK_PRICE_AMOUNT = 799
_STANDARD_VOICE_PACK_CURRENCY = "eur"
_CUSTOM_VOICE_STARTING_PRICE_AMOUNT = 2499
_ANALYTICS_UNIQUE_RETENTION_DAYS = 35
_ANALYTICS_AGGREGATE_RETENTION_DAYS = 400
_ANALYTICS_ALLOWED_PATHS = {
    "/",
    "/store",
    "/store/success",
    "/privacy",
    "/terms",
    "/refunds",
    "/home-assistant",
    "/models/genie-1000",
    "/models/m9-pro",
    "/models/mgc1000",
    "/voice-packs",
}
_ANALYTICS_LANGUAGES = {
    "hu", "en", "de", "fr", "es", "it", "pt", "nl", "pl", "cs", "sk",
    "ro", "da", "sv", "no", "fi", "zh-CN", "zh-TW", "tr", "th", "vi",
    "ko", "km",
}
_ANALYTICS_PUBLIC_HTML = {
    "public_site.html",
    "public_terms.html",
    "public_refunds.html",
    "public_privacy.html",
    "store.html",
    "store_success.html",
}
_PUBLIC_SITE_BASE_URL = "https://anthbotmap.com"
_LEGACY_PUBLIC_HOSTS = {"reports.mqbretrofithungary.online"}
_SEO_PAGES: dict[str, dict[str, Any]] = {
    "public_site.html": {
        "path": "/",
        "title": "ANTHBOT Map for Home Assistant – Maps, Zones & Voice Packs",
        "description": (
            "ANTHBOT Map is an independent Home Assistant integration for ANTHBOT "
            "robotic lawn mowers, with live maps, zones, schedules, mowing history, "
            "diagnostics and voice packs."
        ),
        "index": True,
    },
    "store.html": {
        "path": "/store",
        "title": "ANTHBOT Voice Packs & Custom Voices | ANTHBOT Map",
        "description": (
            "Browse ANTHBOT community voice packs and request custom mower voices "
            "for supported models, with automatic ANTHBOT Map integration."
        ),
        "index": True,
    },
    "public_terms.html": {
        "path": "/terms",
        "title": "Terms of Service | ANTHBOT Map",
        "description": "Terms of Service for ANTHBOT Map digital services and voice packs.",
        "index": True,
    },
    "public_refunds.html": {
        "path": "/refunds",
        "title": "Refund Policy | ANTHBOT Map",
        "description": "Refund Policy for ANTHBOT Map digital products and services.",
        "index": True,
    },
    "public_privacy.html": {
        "path": "/privacy",
        "title": "Privacy Policy | ANTHBOT Map",
        "description": "Privacy and data protection information for ANTHBOT Map services.",
        "index": True,
    },
    "store_success.html": {
        "path": "/store/success",
        "title": "ANTHBOT Voice Purchase | ANTHBOT Map",
        "description": "ANTHBOT voice pack purchase confirmation.",
        "index": False,
    },
}

_PUBLIC_TYPOGRAPHY_STYLE = """
<style id="anthbot-compact-typography">
html{font-size:14px}
body{font-size:.92rem}
h1,.hero h1,.legal h1{font-size:clamp(1.85rem,3.7vw,2.95rem)!important;line-height:1.08!important}
.hero p,.lead,.intro{font-size:.92rem!important;line-height:1.55!important}
.section h2,.custom h2{font-size:clamp(1.35rem,2.5vw,1.7rem)!important}
.feature h3,.legal h2,.policy-body h2,.request h2{font-size:1rem!important}
.links a,.navlinks a{font-size:12px!important}
.brand{font-size:.92rem}
.tag,.pill,.repo-meta,.footer,.small,.status,.form-status{font-size:.76rem!important}
.feature p,.bullet span,.desc,.custom-text,.legal p,.policy-body p,.policy-body li{font-size:.84rem!important;line-height:1.5!important}
.price-big,.custom-price strong{font-size:1.5rem!important}
.price{font-size:1.2rem!important}
.topic-title strong,.demo-title strong{font-size:1.05rem!important}
.tile strong,.schedule-card strong{font-size:.76rem!important}
.tile span,.rule span,.schedule-card small{font-size:.68rem!important}
@media(max-width:760px){
  html{font-size:13.5px}
  h1,.hero h1,.legal h1{font-size:clamp(1.7rem,8vw,2.45rem)!important}
  .section h2,.custom h2{font-size:1.42rem!important}
}
</style>
"""

_SEO_LANDING_I18N = json.loads(r'''{"en":{"common":{"features":"Features","models":"Models","voiceStore":"Voice Store","support":"Support","terms":"Terms","privacy":"Privacy","language":"Language","tagModelAware":"Model-aware","tagIndependent":"Independent community project","back":"Back to ANTHBOT Map","project":"Project","platform":"Platform","license":"License","cardSubtitle":"Anthbot Map Card · Home Assistant","cloudLive":"CLOUD LIVE","builtTitle":"Built around the same project as the main site.","noticeTitle":"Independent project / trademark notice","noticeBody":"ANTHBOT is a trademark of its respective owner. ANTHBOT Map and MQB Retrofit Hungary are independent and are not official ANTHBOT products unless explicitly stated otherwise.","github":"ANTHBOT Map GitHub","home":"Home","voicePackStore":"Voice Pack Store","refunds":"Refunds","rights":"All rights reserved."},"pages":{"/home-assistant":{"title":"ANTHBOT Home Assistant Integration | ANTHBOT Map","description":"Connect supported ANTHBOT robotic lawn mowers to Home Assistant with ANTHBOT Map: live map, zones, native schedules, mower controls, history and diagnostics.","eyebrow":"Home Assistant integration","heading":"ANTHBOT in Home Assistant with ANTHBOT Map","lead":"ANTHBOT Map is an independent, open-source Home Assistant integration and Lovelace map card for supported ANTHBOT robotic lawn mowers.","cta":"View ANTHBOT Map on GitHub","sections":[["What the integration adds","ANTHBOT Map connects Home Assistant to the ANTHBOT cloud, creates a native lawn_mower entity, mirrors supported ANTHBOT app schedules, and provides model-aware controls instead of forcing every mower through one generic command path."],["Live map and lawn data","Where supported by the mower family, the card can render the lawn boundary, mowing zones, No-Go areas, mower position, live path and mowing coverage. A dedicated WebSocket live-map transport keeps high-frequency geometry out of Home Assistant Recorder."],["Schedules and automations","Native app schedules can be mirrored into Home Assistant and, on supported models, edited with write-back. Per-mower next-mow data, native mower events and timed mow/park overrides can be used in Home Assistant automations."],["Model-aware design","Genie, M-series, N8 and Pion/MGC devices use separated model routing. Capabilities remain conservative when a command or protocol detail has not been confirmed."]]},"/models/genie-1000":{"title":"ANTHBOT Genie 1000 Home Assistant Support | ANTHBOT Map","description":"ANTHBOT Genie 1000 support in Home Assistant with ANTHBOT Map, including live map/path data, zones, schedules, history, mower controls and diagnostics.","eyebrow":"Supported mower","heading":"ANTHBOT Genie 1000 + Home Assistant","lead":"The Genie family is supported by ANTHBOT Map and has been directly hardware-tested by the project, including Genie 1000 schedule loading.","cta":"Install / documentation","sections":[["Direct hardware validation","The public ANTHBOT Map project documents direct real-device testing for the Genie family. Genie 1000 native app schedule loading has also been verified on real hardware."],["Maps, zones and mowing history","ANTHBOT Map keeps Genie-specific map/path diagnostics isolated from other mower families and exposes supported lawn boundary, zones, No-Go geometry, live mower position, path and historical mowing data."],["Native scheduling","The integration mirrors the mower's native ANTHBOT app schedule into Home Assistant and supports the model-specific schedule path rather than translating it through M-series behavior."],["Home Assistant controls","Supported operations include mower status and common mowing controls, with model-specific routing plus Battery Saver and diagnostic tooling where the underlying device capabilities are available."]]},"/models/m9-pro":{"title":"ANTHBOT M9 Pro Home Assistant Integration | ANTHBOT Map","description":"ANTHBOT M9 Pro support for Home Assistant with control, status, live map, path, zones, mowing history, schedules and diagnostics.","eyebrow":"Directly hardware-tested","heading":"ANTHBOT M9 Pro + Home Assistant","lead":"ANTHBOT Map includes a dedicated M-series implementation, and M9 Pro control, status, map, path, zone and history handling have been directly hardware-tested by the project.","cta":"View M9 Pro integration documentation","sections":[["Live map architecture","Real-device M9 Pro validation confirmed live WebSocket path updates, Home Assistant restart and reconnect handling, snapshot restore and reduced Recorder churn."],["Zones and mowing data","The M-series path supports map, path, zone and history handling while keeping model-specific decoding separate from Genie and N8."],["Native schedule write-back","Creating an M9 Pro schedule from the ANTHBOT Map card has been verified on real hardware, with the created rule appearing in the ANTHBOT app."],["Home Assistant automation","Mower state, next-mow information, lifecycle/schedule events and supported controls can be used in dashboards and automations."]]},"/models/mgc1000":{"title":"ANTHBOT MGC1000 / Pion Home Assistant Support | ANTHBOT Map","description":"ANTHBOT MGC1000 and Pion-family support in ANTHBOT Map for Home Assistant: isolated model detection, status normalization, native schedules and start routing.","eyebrow":"Pion / MGC family","heading":"ANTHBOT MGC1000 + Home Assistant","lead":"ANTHBOT Map has a dedicated Pion/MGC model family for identifiers such as MGC500, MGC750 and MGC1000, instead of treating these mowers as Genie.","cta":"Follow Pion / MGC development","sections":[["Dedicated model handling","The integration includes isolated Pion/MGC detection and a flat-shadow normalization layer for Home Assistant status data."],["Confirmed status data","The current implementation exposes confirmed cutting height, mowing progress and area, rain state, Wi-Fi/IP, path payload and firmware data when supplied by the mower/cloud."],["Native schedules and start routing","Pion/MGC uses its own native schedule shape and start path. The integration preserves its one-appointment-per-day/full-lawn schedule behavior instead of applying Genie-only payloads."],["Conservative capability policy","Unverified Pion/MGC setting writes and curpath decoding remain intentionally disabled until protocol and hardware behavior are confirmed."]]},"/voice-packs":{"title":"ANTHBOT Voice Packs for Genie Mowers | ANTHBOT Map","description":"ANTHBOT community voice packs and custom mower voices for compatible ANTHBOT Genie robots, integrated with the ANTHBOT Map ecosystem.","eyebrow":"Community voice packs","heading":"ANTHBOT voice packs and custom mower voices","lead":"The ANTHBOT Map ecosystem includes optional Community voice packs for compatible ANTHBOT Genie robots, with ready-made packs and custom voice requests.","cta":"Open the Voice Pack Store","sections":[["Ready-made Community packs","Available voice packs are listed in the ANTHBOT Community Voice Store. Compatibility is shown with the pack and can vary by mower model or firmware."],["Custom voice requests","A separate custom-voice workflow is available for requests that are not covered by the ready-made catalogue."],["ANTHBOT Map integration","Purchased voice entitlements can be linked to ANTHBOT Map so compatible installed systems can recognize the purchased pack without exposing paid download URLs publicly."],["Independent project","Community voice packs and ANTHBOT Map are independent project features. ANTHBOT is a trademark of its respective owner; this site does not imply official ANTHBOT endorsement."]]}}},"hu":{"common":{"features":"Funkciók","models":"Modellek","voiceStore":"Hangbolt","support":"Támogatás","terms":"Feltételek","privacy":"Adatvédelem","language":"Nyelv","tagModelAware":"Modellfüggő","tagIndependent":"Független közösségi projekt","back":"Vissza az ANTHBOT Maphez","project":"Projekt","platform":"Platform","license":"Licenc","cardSubtitle":"Anthbot Map kártya · Home Assistant","cloudLive":"ÉLŐ FELHŐ","builtTitle":"Ugyanarra a projektre épül, mint a főoldal.","noticeTitle":"Független projekt / védjegy","noticeBody":"Az ANTHBOT a mindenkori jogosult védjegye. Az ANTHBOT Map és az MQB Retrofit Hungary független projekt; nem hivatalos ANTHBOT-termék, kivéve ha ezt külön jelezzük.","github":"ANTHBOT Map GitHub","home":"Főoldal","voicePackStore":"Hangcsomagbolt","refunds":"Visszatérítések","rights":"Minden jog fenntartva."},"pages":{"/home-assistant":{"title":"ANTHBOT Home Assistant integráció | ANTHBOT Map","description":"Támogatott ANTHBOT robotfűnyírók csatlakoztatása a Home Assistanthoz ANTHBOT Mappel: élő térkép, zónák, natív ütemezések, vezérlés, előzmények és diagnosztika.","eyebrow":"Home Assistant integráció","heading":"ANTHBOT a Home Assistantban az ANTHBOT Mappel","lead":"Az ANTHBOT Map egy független, nyílt forráskódú Home Assistant integráció és Lovelace térképkártya a támogatott ANTHBOT robotfűnyírókhoz.","cta":"ANTHBOT Map megnyitása GitHubon","sections":[["Mit ad az integráció?","Az ANTHBOT Map összekapcsolja a Home Assistantot az ANTHBOT felhővel, natív lawn_mower entitást hoz létre, tükrözi a támogatott ANTHBOT appos ütemezéseket, és modellenként kezeli a vezérlést."],["Élő térkép és gyepadatok","A támogatott modelleknél a kártya megjeleníti a gyephatárt, nyírási zónákat, tiltott területeket, a robot helyzetét, élő útvonalát és a nyírás lefedettségét. A külön WebSocket-útvonal a nagyfrekvenciás geometriát távol tartja a Home Assistant Recordertől."],["Ütemezések és automatizálások","A natív appos ütemezések tükrözhetők a Home Assistantba, támogatott modelleken pedig vissza is írhatók. A következő nyírás adatai, a robotesemények és az időzített nyírás/parkolás felülírások automatizálásokban is használhatók."],["Modellfüggő kialakítás","A Genie, M-széria, N8 és Pion/MGC eszközök külön modellútvonalat használnak. Az integráció nem engedélyez olyan funkciót, amelynek parancsa vagy protokollja még nincs megerősítve."]]},"/models/genie-1000":{"title":"ANTHBOT Genie 1000 Home Assistant támogatás | ANTHBOT Map","description":"ANTHBOT Genie 1000 támogatás Home Assistantban ANTHBOT Mappel: élő térkép és útvonal, zónák, ütemezések, előzmények, vezérlés és diagnosztika.","eyebrow":"Támogatott fűnyíró","heading":"ANTHBOT Genie 1000 + Home Assistant","lead":"A Genie családot támogatja az ANTHBOT Map, és a projekt közvetlenül valódi hardveren is tesztelte, beleértve a Genie 1000 ütemezéseinek betöltését.","cta":"Telepítés / dokumentáció","sections":[["Közvetlen hardveres ellenőrzés","Az ANTHBOT Map projekt valódi eszközön végzett Genie-teszteket dokumentál. A Genie 1000 natív appos ütemezésének betöltése is igazolt valódi hardveren."],["Térkép, zónák és nyírási előzmények","Az ANTHBOT Map a Genie-specifikus térkép- és útvonaldiagnosztikát elkülönítve kezeli, és támogatás esetén megjeleníti a gyephatárt, zónákat, tiltott területeket, a robot élő helyzetét, útvonalát és korábbi nyírási adatokat."],["Natív ütemezés","Az integráció a robot natív ANTHBOT appos ütemezését tükrözi a Home Assistantba, és a modell saját ütemezési útvonalát használja az M-szériás viselkedés átalakítása helyett."],["Home Assistant vezérlés","A támogatott műveletek közé tartozik a robot állapota és az alapvető nyírásvezérlés, modellfüggő útvonalon, valamint az elérhető Battery Saver és diagnosztikai eszközök."]]},"/models/m9-pro":{"title":"ANTHBOT M9 Pro Home Assistant integráció | ANTHBOT Map","description":"ANTHBOT M9 Pro támogatás Home Assistantban: vezérlés, állapot, élő térkép, útvonal, zónák, nyírási előzmények, ütemezések és diagnosztika.","eyebrow":"Közvetlenül hardveren tesztelve","heading":"ANTHBOT M9 Pro + Home Assistant","lead":"Az ANTHBOT Map külön M-szériás megvalósítást tartalmaz; az M9 Pro vezérlését, állapotát, térképét, útvonalát, zónáit és előzményeit a projekt közvetlenül valódi hardveren is tesztelte.","cta":"M9 Pro integráció dokumentációja","sections":[["Élőtérkép-architektúra","A valódi M9 Pro eszközön végzett teszt igazolta az élő WebSocket útvonalfrissítéseket, a Home Assistant újraindítás és újracsatlakozás kezelését, a snapshot-visszaállítást és a Recorder-terhelés csökkentését."],["Zónák és nyírási adatok","Az M-szériás útvonal kezeli a térképet, útvonalat, zónákat és előzményeket, miközben a modellfüggő dekódolás külön marad a Genie és N8 családtól."],["Natív ütemezés-visszaírás","Az M9 Pro ütemezés létrehozása az ANTHBOT Map kártyáról valódi hardveren igazolt; a létrehozott szabály megjelenik az ANTHBOT appban."],["Home Assistant automatizálás","A robot állapota, következő nyírása, életciklus- és ütemezési eseményei, valamint a támogatott vezérlések használhatók műszerfalakon és automatizálásokban."]]},"/models/mgc1000":{"title":"ANTHBOT MGC1000 / Pion Home Assistant támogatás | ANTHBOT Map","description":"ANTHBOT MGC1000 és Pion család támogatása ANTHBOT Mapben: elkülönített modellfelismerés, állapotnormalizálás, natív ütemezések és indítási útvonal.","eyebrow":"Pion / MGC család","heading":"ANTHBOT MGC1000 + Home Assistant","lead":"Az ANTHBOT Map külön Pion/MGC modellcsaládot kezel az MGC500, MGC750 és MGC1000 azonosítókhoz, így ezeket a robotokat nem Genie-ként kezeli.","cta":"Pion / MGC fejlesztés követése","sections":[["Külön modellkezelés","Az integráció elkülönített Pion/MGC felismerést és flat-shadow normalizálási réteget tartalmaz a Home Assistant állapotadatokhoz."],["Megerősített állapotadatok","A jelenlegi megvalósítás a robot/felhő által szolgáltatott adatokból elérhetővé teszi a megerősített vágási magasságot, nyírási haladást és területet, esőállapotot, Wi-Fi/IP adatokat, útvonal-payloadot és firmware-adatokat."],["Natív ütemezések és indítás","A Pion/MGC saját natív ütemezési formát és indítási útvonalat használ. Az integráció megtartja a napi egy időpont/teljes gyep működést, és nem alkalmaz Genie-specifikus payloadokat."],["Óvatos képességkezelés","A még nem igazolt Pion/MGC beállításírás és curpath-dekódolás szándékosan letiltva marad, amíg a protokoll és a hardver viselkedése nincs megerősítve."]]},"/voice-packs":{"title":"ANTHBOT hangcsomagok Genie robotokhoz | ANTHBOT Map","description":"ANTHBOT közösségi hangcsomagok és egyedi robothangok kompatibilis ANTHBOT Genie robotokhoz, az ANTHBOT Map rendszerébe integrálva.","eyebrow":"Közösségi hangcsomagok","heading":"ANTHBOT hangcsomagok és egyedi robothangok","lead":"Az ANTHBOT Map rendszer opcionális közösségi hangcsomagokat kínál kompatibilis ANTHBOT Genie robotokhoz, kész csomagokkal és egyedi hangigényléssel.","cta":"Hangcsomagbolt megnyitása","sections":[["Kész közösségi csomagok","Az elérhető hangcsomagok az ANTHBOT Community Hangboltban jelennek meg. A kompatibilitás csomagonként látható, és modellenként vagy firmware-verziónként eltérhet."],["Egyedi hangigénylés","Külön egyedihang-folyamat érhető el azokra az igényekre, amelyeket a kész katalógus nem fed le."],["ANTHBOT Map integráció","A megvásárolt hangjogosultság összekapcsolható az ANTHBOT Mappel, így a kompatibilis telepített rendszer felismerheti a megvásárolt csomagot anélkül, hogy a fizetős letöltési URL nyilvánossá válna."],["Független projekt","A közösségi hangcsomagok és az ANTHBOT Map független projektfunkciók. Az ANTHBOT a mindenkori jogosult védjegye; az oldal nem állít hivatalos ANTHBOT jóváhagyást."]]}}},"de":{"common":{"features":"Funktionen","models":"Modelle","voiceStore":"Voice Store","support":"Support","terms":"Bedingungen","privacy":"Datenschutz","language":"Sprache","tagModelAware":"Modellabhängig","tagIndependent":"Unabhängiges Community-Projekt","back":"Zurück zu ANTHBOT Map","project":"Projekt","platform":"Plattform","license":"Lizenz","cardSubtitle":"Anthbot Map Card · Home Assistant","cloudLive":"CLOUD LIVE","builtTitle":"Basiert auf demselben Projekt wie die Hauptseite.","noticeTitle":"Unabhängiges Projekt / Markenhinweis","noticeBody":"ANTHBOT ist eine Marke des jeweiligen Rechteinhabers. ANTHBOT Map und MQB Retrofit Hungary sind unabhängig und keine offiziellen ANTHBOT-Produkte, sofern nicht ausdrücklich anders angegeben.","github":"ANTHBOT Map GitHub","home":"Startseite","voicePackStore":"Voice-Pack-Store","refunds":"Erstattungen","rights":"Alle Rechte vorbehalten."},"pages":{"/home-assistant":{"title":"ANTHBOT Home Assistant Integration | ANTHBOT Map","description":"Unterstützte ANTHBOT Mähroboter mit ANTHBOT Map in Home Assistant integrieren: Live-Karte, Zonen, native Zeitpläne, Steuerung, Verlauf und Diagnose.","eyebrow":"Home Assistant Integration","heading":"ANTHBOT in Home Assistant mit ANTHBOT Map","lead":"ANTHBOT Map ist eine unabhängige Open-Source-Integration für Home Assistant mit Lovelace-Kartenansicht für unterstützte ANTHBOT Mähroboter.","cta":"ANTHBOT Map auf GitHub öffnen","sections":[["Was die Integration bietet","ANTHBOT Map verbindet Home Assistant mit der ANTHBOT Cloud, erstellt eine native lawn_mower-Entität, spiegelt unterstützte ANTHBOT-App-Zeitpläne und nutzt modellabhängige Steuerpfade."],["Live-Karte und Rasen-Daten","Bei unterstützten Modellen zeigt die Karte Rasenbegrenzung, Mähzonen, No-Go-Bereiche, Roboterposition, Live-Pfad und Mähabdeckung. Ein eigener WebSocket-Transport hält hochfrequente Geometriedaten aus dem Home Assistant Recorder heraus."],["Zeitpläne und Automationen","Native App-Zeitpläne können in Home Assistant gespiegelt und bei unterstützten Modellen zurückgeschrieben werden. Nächster Mähtermin, Roboterereignisse und zeitgesteuerte Mäh-/Park-Overrides lassen sich in Automationen verwenden."],["Modellabhängiges Design","Genie-, M-Serie-, N8- und Pion/MGC-Geräte verwenden getrennte Modellpfade. Nicht bestätigte Befehle oder Protokolldetails bleiben deaktiviert."]]},"/models/genie-1000":{"title":"ANTHBOT Genie 1000 Home Assistant Support | ANTHBOT Map","description":"ANTHBOT Genie 1000 Unterstützung in Home Assistant mit Live-Karte, Zonen, Zeitplänen, Verlauf, Steuerung und Diagnose.","eyebrow":"Unterstützter Mäher","heading":"ANTHBOT Genie 1000 + Home Assistant","lead":"Die Genie-Familie wird von ANTHBOT Map unterstützt und direkt auf echter Hardware getestet, einschließlich des Ladens nativer Genie-1000-Zeitpläne.","cta":"Installation / Dokumentation","sections":[["Direkte Hardware-Prüfung","Das öffentliche ANTHBOT-Map-Projekt dokumentiert Tests an echten Genie-Geräten. Auch das Laden nativer Genie-1000-App-Zeitpläne wurde auf realer Hardware bestätigt."],["Karten, Zonen und Mähverlauf","ANTHBOT Map trennt Genie-spezifische Karten-/Pfad-Diagnosen von anderen Modellfamilien und zeigt, soweit unterstützt, Begrenzung, Zonen, No-Go-Geometrie, Live-Position, Pfad und historische Mähdaten."],["Native Zeitplanung","Die Integration spiegelt den nativen ANTHBOT-App-Zeitplan des Mähers in Home Assistant und nutzt den modellspezifischen Zeitplanpfad statt M-Serie-Verhalten umzusetzen."],["Home Assistant Steuerung","Unterstützte Funktionen umfassen Status und grundlegende Mähsteuerung mit modellabhängigem Routing sowie Battery-Saver- und Diagnosewerkzeuge, sofern vom Gerät unterstützt."]]},"/models/m9-pro":{"title":"ANTHBOT M9 Pro Home Assistant Integration | ANTHBOT Map","description":"ANTHBOT M9 Pro in Home Assistant: Steuerung, Status, Live-Karte, Pfad, Zonen, Mähverlauf, Zeitpläne und Diagnose.","eyebrow":"Direkt auf Hardware getestet","heading":"ANTHBOT M9 Pro + Home Assistant","lead":"ANTHBOT Map enthält eine eigene M-Serie-Implementierung; Steuerung, Status, Karte, Pfad, Zonen und Verlauf des M9 Pro wurden direkt auf realer Hardware getestet.","cta":"M9-Pro-Dokumentation öffnen","sections":[["Live-Karten-Architektur","Tests am echten M9 Pro bestätigten Live-WebSocket-Pfadupdates, Neustart- und Reconnect-Verhalten von Home Assistant, Snapshot-Wiederherstellung und geringere Recorder-Belastung."],["Zonen und Mähdaten","Der M-Serie-Pfad unterstützt Karte, Pfad, Zonen und Verlauf und hält die modellspezifische Dekodierung von Genie und N8 getrennt."],["Native Zeitplan-Rückschreibung","Das Erstellen eines M9-Pro-Zeitplans über die ANTHBOT-Map-Karte wurde auf realer Hardware bestätigt; die Regel erscheint in der ANTHBOT App."],["Home Assistant Automation","Mäherstatus, nächster Mähtermin, Lebenszyklus-/Zeitplanereignisse und unterstützte Steuerungen können in Dashboards und Automationen genutzt werden."]]},"/models/mgc1000":{"title":"ANTHBOT MGC1000 / Pion Home Assistant Support | ANTHBOT Map","description":"ANTHBOT MGC1000 und Pion-Familie in ANTHBOT Map: getrennte Modellerkennung, Statusnormalisierung, native Zeitpläne und Start-Routing.","eyebrow":"Pion / MGC Familie","heading":"ANTHBOT MGC1000 + Home Assistant","lead":"ANTHBOT Map behandelt MGC500, MGC750 und MGC1000 als eigene Pion/MGC-Modellfamilie statt sie als Genie zu behandeln.","cta":"Pion-/MGC-Entwicklung verfolgen","sections":[["Eigene Modellbehandlung","Die Integration enthält eine getrennte Pion/MGC-Erkennung und eine Flat-Shadow-Normalisierung für Home-Assistant-Statusdaten."],["Bestätigte Statusdaten","Die aktuelle Implementierung stellt bestätigte Schnitthöhe, Mähfortschritt und -fläche, Regenstatus, Wi-Fi/IP, Pfad-Payload und Firmware-Daten bereit, wenn sie vom Mäher bzw. der Cloud geliefert werden."],["Native Zeitpläne und Start-Routing","Pion/MGC verwendet ein eigenes natives Zeitplanformat und einen eigenen Startpfad. Die Integration erhält das Verhalten mit einem Termin pro Tag und vollständiger Rasenfläche, statt Genie-Payloads anzuwenden."],["Konservative Funktionspolitik","Nicht verifizierte Pion/MGC-Schreibzugriffe und curpath-Dekodierung bleiben deaktiviert, bis Protokoll und Hardwareverhalten bestätigt sind."]]},"/voice-packs":{"title":"ANTHBOT Voice Packs für Genie Mäher | ANTHBOT Map","description":"Community-Voice-Packs und individuelle Stimmen für kompatible ANTHBOT Genie Roboter im ANTHBOT-Map-Ökosystem.","eyebrow":"Community Voice Packs","heading":"ANTHBOT Voice Packs und individuelle Mäherstimmen","lead":"Das ANTHBOT-Map-Ökosystem bietet optionale Community-Voice-Packs für kompatible ANTHBOT Genie Roboter sowie individuelle Sprachwünsche.","cta":"Voice-Pack-Store öffnen","sections":[["Fertige Community-Pakete","Verfügbare Voice Packs stehen im ANTHBOT Community Voice Store. Die Kompatibilität wird pro Paket angegeben und kann je nach Modell oder Firmware variieren."],["Individuelle Sprachwünsche","Für Wünsche außerhalb des fertigen Katalogs steht ein separater Custom-Voice-Ablauf bereit."],["ANTHBOT Map Integration","Erworbene Voice-Berechtigungen können mit ANTHBOT Map verknüpft werden, sodass kompatible Installationen das Paket erkennen, ohne kostenpflichtige Download-URLs öffentlich freizugeben."],["Unabhängiges Projekt","Community Voice Packs und ANTHBOT Map sind unabhängige Projektfunktionen. ANTHBOT ist eine Marke des jeweiligen Rechteinhabers; diese Seite behauptet keine offizielle ANTHBOT-Freigabe."]]}}},"fr":{"common":{"features":"Fonctions","models":"Modèles","voiceStore":"Boutique vocale","support":"Assistance","terms":"Conditions","privacy":"Confidentialité","language":"Langue","tagModelAware":"Selon le modèle","tagIndependent":"Projet communautaire indépendant","back":"Retour à ANTHBOT Map","project":"Projet","platform":"Plateforme","license":"Licence","cardSubtitle":"Carte Anthbot Map · Home Assistant","cloudLive":"CLOUD LIVE","builtTitle":"Basé sur le même projet que le site principal.","noticeTitle":"Projet indépendant / marque","noticeBody":"ANTHBOT est une marque de son propriétaire respectif. ANTHBOT Map et MQB Retrofit Hungary sont indépendants et ne sont pas des produits ANTHBOT officiels, sauf indication explicite.","github":"ANTHBOT Map GitHub","home":"Accueil","voicePackStore":"Boutique de voix","refunds":"Remboursements","rights":"Tous droits réservés."},"pages":{"/home-assistant":{"title":"Intégration ANTHBOT Home Assistant | ANTHBOT Map","description":"Connectez les robots tondeuses ANTHBOT pris en charge à Home Assistant avec ANTHBOT Map : carte en direct, zones, programmations natives, commandes, historique et diagnostics.","eyebrow":"Intégration Home Assistant","heading":"ANTHBOT dans Home Assistant avec ANTHBOT Map","lead":"ANTHBOT Map est une intégration Home Assistant indépendante et open source avec une carte Lovelace pour les robots tondeuses ANTHBOT pris en charge.","cta":"Voir ANTHBOT Map sur GitHub","sections":[["Ce qu'ajoute l'intégration","ANTHBOT Map relie Home Assistant au cloud ANTHBOT, crée une entité lawn_mower native, reflète les programmations prises en charge de l'application ANTHBOT et utilise des commandes adaptées au modèle."],["Carte en direct et données de pelouse","Selon le modèle, la carte affiche la limite de pelouse, les zones de tonte, les zones interdites, la position du robot, le trajet en direct et la couverture. Un transport WebSocket dédié évite d'enregistrer la géométrie haute fréquence dans Recorder."],["Programmations et automatisations","Les programmations natives de l'application peuvent être reflétées dans Home Assistant et réécrites sur les modèles pris en charge. Le prochain passage, les événements du robot et les dérogations temporisées peuvent servir dans les automatisations."],["Conception adaptée au modèle","Les appareils Genie, série M, N8 et Pion/MGC utilisent des chemins séparés. Les fonctions non confirmées restent désactivées."]]},"/models/genie-1000":{"title":"Support ANTHBOT Genie 1000 Home Assistant | ANTHBOT Map","description":"Support du Genie 1000 dans Home Assistant avec carte en direct, zones, programmations, historique, commandes et diagnostics.","eyebrow":"Tondeuse prise en charge","heading":"ANTHBOT Genie 1000 + Home Assistant","lead":"La famille Genie est prise en charge par ANTHBOT Map et a été testée directement sur du matériel réel, y compris le chargement des programmations du Genie 1000.","cta":"Installation / documentation","sections":[["Validation matérielle directe","Le projet public ANTHBOT Map documente des tests réels sur la famille Genie. Le chargement des programmations natives du Genie 1000 a également été validé sur du matériel réel."],["Cartes, zones et historique de tonte","ANTHBOT Map sépare les diagnostics carte/trajet propres à Genie et expose, lorsque disponible, limite de pelouse, zones, géométrie No-Go, position en direct, trajet et données historiques."],["Programmation native","L'intégration reflète la programmation native de l'application ANTHBOT dans Home Assistant et utilise le chemin propre au modèle au lieu de convertir le comportement de la série M."],["Commandes Home Assistant","Les opérations prises en charge incluent l'état du robot et les commandes de tonte courantes, avec routage par modèle et outils Battery Saver/diagnostics lorsque disponibles."]]},"/models/m9-pro":{"title":"Intégration ANTHBOT M9 Pro Home Assistant | ANTHBOT Map","description":"ANTHBOT M9 Pro dans Home Assistant : commandes, état, carte en direct, trajet, zones, historique, programmations et diagnostics.","eyebrow":"Testé directement sur matériel","heading":"ANTHBOT M9 Pro + Home Assistant","lead":"ANTHBOT Map possède une implémentation dédiée à la série M ; commandes, état, carte, trajet, zones et historique du M9 Pro ont été testés directement sur du matériel réel.","cta":"Documentation de l'intégration M9 Pro","sections":[["Architecture de carte en direct","Les tests sur un M9 Pro réel ont validé les mises à jour de trajet WebSocket, la gestion des redémarrages/reconnexions Home Assistant, la restauration du snapshot et la réduction de la charge Recorder."],["Zones et données de tonte","Le chemin série M gère carte, trajet, zones et historique tout en séparant le décodage spécifique du modèle de Genie et N8."],["Réécriture native des programmations","La création d'une programmation M9 Pro depuis la carte ANTHBOT Map a été validée sur du matériel réel ; la règle apparaît dans l'application ANTHBOT."],["Automatisation Home Assistant","L'état du robot, le prochain passage, les événements de cycle/programmation et les commandes prises en charge peuvent être utilisés dans les tableaux de bord et automatisations."]]},"/models/mgc1000":{"title":"Support ANTHBOT MGC1000 / Pion Home Assistant | ANTHBOT Map","description":"Support des MGC1000 et Pion dans ANTHBOT Map : détection séparée, normalisation d'état, programmations natives et routage de démarrage.","eyebrow":"Famille Pion / MGC","heading":"ANTHBOT MGC1000 + Home Assistant","lead":"ANTHBOT Map traite MGC500, MGC750 et MGC1000 comme une famille Pion/MGC dédiée au lieu de les traiter comme Genie.","cta":"Suivre le développement Pion / MGC","sections":[["Gestion dédiée du modèle","L'intégration comprend une détection Pion/MGC séparée et une couche de normalisation flat-shadow pour les états Home Assistant."],["Données d'état confirmées","L'implémentation actuelle expose hauteur de coupe, progression et surface tondues, état de pluie, Wi-Fi/IP, payload de trajet et firmware lorsque ces données sont fournies."],["Programmations natives et démarrage","Pion/MGC utilise son propre format de programmation et son propre chemin de démarrage. L'intégration conserve le fonctionnement un rendez-vous par jour/pelouse complète sans appliquer de payloads Genie."],["Politique de capacités prudente","Les écritures de réglages Pion/MGC et le décodage curpath non vérifiés restent désactivés jusqu'à confirmation du protocole et du matériel."]]},"/voice-packs":{"title":"Packs vocaux ANTHBOT pour Genie | ANTHBOT Map","description":"Packs vocaux communautaires et voix personnalisées pour robots ANTHBOT Genie compatibles dans l'écosystème ANTHBOT Map.","eyebrow":"Packs vocaux communautaires","heading":"Packs vocaux ANTHBOT et voix personnalisées","lead":"L'écosystème ANTHBOT Map propose des packs vocaux communautaires optionnels pour les robots ANTHBOT Genie compatibles, ainsi que des demandes de voix personnalisées.","cta":"Ouvrir la boutique de voix","sections":[["Packs communautaires prêts à l'emploi","Les packs disponibles sont listés dans l'ANTHBOT Community Voice Store. La compatibilité est affichée par pack et peut varier selon le modèle ou le firmware."],["Demandes de voix personnalisées","Un flux séparé permet de demander une voix qui n'est pas couverte par le catalogue prêt à l'emploi."],["Intégration ANTHBOT Map","Les droits vocaux achetés peuvent être liés à ANTHBOT Map afin que les installations compatibles reconnaissent le pack sans exposer publiquement les URL de téléchargement payantes."],["Projet indépendant","Les packs communautaires et ANTHBOT Map sont des fonctions d'un projet indépendant. ANTHBOT est une marque de son propriétaire respectif ; ce site ne prétend pas à une approbation officielle."]]}}},"es":{"common":{"features":"Funciones","models":"Modelos","voiceStore":"Tienda de voz","support":"Soporte","terms":"Términos","privacy":"Privacidad","language":"Idioma","tagModelAware":"Según el modelo","tagIndependent":"Proyecto comunitario independiente","back":"Volver a ANTHBOT Map","project":"Proyecto","platform":"Plataforma","license":"Licencia","cardSubtitle":"Tarjeta Anthbot Map · Home Assistant","cloudLive":"NUBE EN VIVO","builtTitle":"Basado en el mismo proyecto que el sitio principal.","noticeTitle":"Proyecto independiente / marca","noticeBody":"ANTHBOT es una marca de su respectivo titular. ANTHBOT Map y MQB Retrofit Hungary son independientes y no son productos oficiales de ANTHBOT salvo indicación expresa.","github":"ANTHBOT Map GitHub","home":"Inicio","voicePackStore":"Tienda de voces","refunds":"Reembolsos","rights":"Todos los derechos reservados."},"pages":{"/home-assistant":{"title":"Integración ANTHBOT Home Assistant | ANTHBOT Map","description":"Conecta cortacéspedes ANTHBOT compatibles a Home Assistant con ANTHBOT Map: mapa en vivo, zonas, horarios nativos, controles, historial y diagnósticos.","eyebrow":"Integración Home Assistant","heading":"ANTHBOT en Home Assistant con ANTHBOT Map","lead":"ANTHBOT Map es una integración independiente y de código abierto para Home Assistant con tarjeta Lovelace para cortacéspedes ANTHBOT compatibles.","cta":"Ver ANTHBOT Map en GitHub","sections":[["Qué añade la integración","ANTHBOT Map conecta Home Assistant con la nube de ANTHBOT, crea una entidad lawn_mower nativa, refleja horarios compatibles de la app ANTHBOT y usa controles específicos por modelo."],["Mapa en vivo y datos del césped","Cuando el modelo lo permite, la tarjeta muestra límite del césped, zonas, áreas No-Go, posición del robot, ruta en vivo y cobertura. Un transporte WebSocket dedicado evita guardar geometría de alta frecuencia en Recorder."],["Horarios y automatizaciones","Los horarios nativos de la app pueden reflejarse en Home Assistant y reescribirse en modelos compatibles. Próxima siega, eventos del robot y anulaciones temporizadas pueden usarse en automatizaciones."],["Diseño específico por modelo","Genie, serie M, N8 y Pion/MGC usan rutas separadas. Las funciones no confirmadas permanecen desactivadas."]]},"/models/genie-1000":{"title":"Soporte ANTHBOT Genie 1000 Home Assistant | ANTHBOT Map","description":"Soporte de Genie 1000 en Home Assistant con mapa en vivo, zonas, horarios, historial, controles y diagnósticos.","eyebrow":"Cortacésped compatible","heading":"ANTHBOT Genie 1000 + Home Assistant","lead":"La familia Genie es compatible con ANTHBOT Map y ha sido probada directamente en hardware real, incluida la carga de horarios del Genie 1000.","cta":"Instalación / documentación","sections":[["Validación directa en hardware","El proyecto público ANTHBOT Map documenta pruebas en dispositivos Genie reales. La carga de horarios nativos del Genie 1000 también se verificó en hardware real."],["Mapas, zonas e historial de siega","ANTHBOT Map mantiene separados los diagnósticos de mapa/ruta de Genie y expone, cuando está disponible, límite, zonas, geometría No-Go, posición en vivo, ruta e historial."],["Programación nativa","La integración refleja el horario nativo de la app ANTHBOT en Home Assistant y usa la ruta específica del modelo en lugar de convertir el comportamiento de la serie M."],["Controles de Home Assistant","Las operaciones compatibles incluyen estado y controles comunes de siega, con enrutamiento por modelo y herramientas Battery Saver/diagnóstico cuando están disponibles."]]},"/models/m9-pro":{"title":"Integración ANTHBOT M9 Pro Home Assistant | ANTHBOT Map","description":"ANTHBOT M9 Pro en Home Assistant: control, estado, mapa en vivo, ruta, zonas, historial, horarios y diagnósticos.","eyebrow":"Probado directamente en hardware","heading":"ANTHBOT M9 Pro + Home Assistant","lead":"ANTHBOT Map incluye una implementación específica para la serie M; control, estado, mapa, ruta, zonas e historial del M9 Pro se han probado directamente en hardware real.","cta":"Documentación de M9 Pro","sections":[["Arquitectura de mapa en vivo","Las pruebas en un M9 Pro real confirmaron actualizaciones de ruta WebSocket, reinicio/reconexión de Home Assistant, restauración de snapshot y menor carga de Recorder."],["Zonas y datos de siega","La ruta de la serie M admite mapa, ruta, zonas e historial manteniendo la decodificación específica separada de Genie y N8."],["Reescritura nativa de horarios","La creación de un horario M9 Pro desde la tarjeta ANTHBOT Map se verificó en hardware real y la regla aparece en la app ANTHBOT."],["Automatización en Home Assistant","Estado del robot, próxima siega, eventos de ciclo/horario y controles compatibles pueden usarse en paneles y automatizaciones."]]},"/models/mgc1000":{"title":"Soporte ANTHBOT MGC1000 / Pion Home Assistant | ANTHBOT Map","description":"Soporte MGC1000 y Pion en ANTHBOT Map: detección separada, normalización de estado, horarios nativos y ruta de inicio.","eyebrow":"Familia Pion / MGC","heading":"ANTHBOT MGC1000 + Home Assistant","lead":"ANTHBOT Map trata MGC500, MGC750 y MGC1000 como una familia Pion/MGC dedicada en lugar de tratarlos como Genie.","cta":"Seguir desarrollo Pion / MGC","sections":[["Gestión dedicada del modelo","La integración incluye detección Pion/MGC separada y una capa de normalización flat-shadow para los estados de Home Assistant."],["Datos de estado confirmados","La implementación actual expone altura de corte, progreso y superficie, lluvia, Wi-Fi/IP, payload de ruta y firmware cuando el robot o la nube los proporcionan."],["Horarios nativos e inicio","Pion/MGC usa su propio formato de horario y ruta de inicio. La integración conserva el comportamiento de una cita al día/césped completo sin aplicar payloads Genie."],["Política conservadora de capacidades","Las escrituras de ajustes Pion/MGC y la decodificación curpath no verificadas permanecen desactivadas hasta confirmar protocolo y hardware."]]},"/voice-packs":{"title":"Paquetes de voz ANTHBOT para Genie | ANTHBOT Map","description":"Paquetes de voz comunitarios y voces personalizadas para robots ANTHBOT Genie compatibles dentro del ecosistema ANTHBOT Map.","eyebrow":"Paquetes de voz comunitarios","heading":"Paquetes de voz ANTHBOT y voces personalizadas","lead":"El ecosistema ANTHBOT Map ofrece paquetes de voz comunitarios opcionales para robots ANTHBOT Genie compatibles y solicitudes de voz personalizada.","cta":"Abrir tienda de voces","sections":[["Paquetes comunitarios preparados","Los paquetes disponibles aparecen en la ANTHBOT Community Voice Store. La compatibilidad se indica por paquete y puede variar según el modelo o firmware."],["Solicitudes de voz personalizada","Existe un flujo separado para solicitar voces que no estén cubiertas por el catálogo preparado."],["Integración con ANTHBOT Map","Los derechos de voz comprados pueden vincularse a ANTHBOT Map para que las instalaciones compatibles reconozcan el paquete sin exponer públicamente URL de descarga de pago."],["Proyecto independiente","Los paquetes comunitarios y ANTHBOT Map son funciones de un proyecto independiente. ANTHBOT es una marca de su respectivo titular; este sitio no implica aprobación oficial."]]}}},"it":{"common":{"features":"Funzioni","models":"Modelli","voiceStore":"Negozio voci","support":"Supporto","terms":"Termini","privacy":"Privacy","language":"Lingua","tagModelAware":"Specifico per modello","tagIndependent":"Progetto community indipendente","back":"Torna ad ANTHBOT Map","project":"Progetto","platform":"Piattaforma","license":"Licenza","cardSubtitle":"Scheda Anthbot Map · Home Assistant","cloudLive":"CLOUD LIVE","builtTitle":"Basato sullo stesso progetto del sito principale.","noticeTitle":"Progetto indipendente / marchio","noticeBody":"ANTHBOT è un marchio del rispettivo titolare. ANTHBOT Map e MQB Retrofit Hungary sono indipendenti e non sono prodotti ufficiali ANTHBOT salvo indicazione esplicita.","github":"ANTHBOT Map GitHub","home":"Home","voicePackStore":"Negozio pacchetti voce","refunds":"Rimborsi","rights":"Tutti i diritti riservati."},"pages":{"/home-assistant":{"title":"Integrazione ANTHBOT Home Assistant | ANTHBOT Map","description":"Collega i robot tagliaerba ANTHBOT supportati a Home Assistant con ANTHBOT Map: mappa live, zone, programmi nativi, controlli, cronologia e diagnostica.","eyebrow":"Integrazione Home Assistant","heading":"ANTHBOT in Home Assistant con ANTHBOT Map","lead":"ANTHBOT Map è un'integrazione Home Assistant indipendente e open source con scheda Lovelace per i robot tagliaerba ANTHBOT supportati.","cta":"Apri ANTHBOT Map su GitHub","sections":[["Cosa aggiunge l'integrazione","ANTHBOT Map collega Home Assistant al cloud ANTHBOT, crea un'entità lawn_mower nativa, replica i programmi supportati dell'app ANTHBOT e usa controlli specifici per modello."],["Mappa live e dati del prato","Dove supportato, la scheda mostra confine del prato, zone, aree No-Go, posizione del robot, percorso live e copertura. Un trasporto WebSocket dedicato evita di salvare geometrie ad alta frequenza nel Recorder."],["Programmi e automazioni","I programmi nativi dell'app possono essere replicati in Home Assistant e riscritti sui modelli supportati. Prossimo taglio, eventi del robot e override temporizzati possono essere usati nelle automazioni."],["Design specifico per modello","Genie, serie M, N8 e Pion/MGC usano percorsi separati. Le funzioni non confermate restano disabilitate."]]},"/models/genie-1000":{"title":"Supporto ANTHBOT Genie 1000 Home Assistant | ANTHBOT Map","description":"Supporto Genie 1000 in Home Assistant con mappa live, zone, programmi, cronologia, controlli e diagnostica.","eyebrow":"Tagliaerba supportato","heading":"ANTHBOT Genie 1000 + Home Assistant","lead":"La famiglia Genie è supportata da ANTHBOT Map ed è stata testata direttamente su hardware reale, incluso il caricamento dei programmi del Genie 1000.","cta":"Installazione / documentazione","sections":[["Validazione diretta su hardware","Il progetto pubblico ANTHBOT Map documenta test su dispositivi Genie reali. Anche il caricamento dei programmi nativi del Genie 1000 è stato verificato su hardware reale."],["Mappe, zone e cronologia di taglio","ANTHBOT Map mantiene separata la diagnostica mappa/percorso di Genie e mostra, dove disponibile, confine, zone, geometria No-Go, posizione live, percorso e dati storici."],["Programmazione nativa","L'integrazione replica il programma nativo dell'app ANTHBOT in Home Assistant e usa il percorso specifico del modello invece di convertire il comportamento della serie M."],["Controlli Home Assistant","Le operazioni supportate includono stato e controlli di taglio comuni, con routing specifico per modello e strumenti Battery Saver/diagnostica quando disponibili."]]},"/models/m9-pro":{"title":"Integrazione ANTHBOT M9 Pro Home Assistant | ANTHBOT Map","description":"ANTHBOT M9 Pro in Home Assistant: controllo, stato, mappa live, percorso, zone, cronologia, programmi e diagnostica.","eyebrow":"Testato direttamente su hardware","heading":"ANTHBOT M9 Pro + Home Assistant","lead":"ANTHBOT Map include un'implementazione dedicata alla serie M; controllo, stato, mappa, percorso, zone e cronologia di M9 Pro sono stati testati direttamente su hardware reale.","cta":"Documentazione M9 Pro","sections":[["Architettura della mappa live","I test su un M9 Pro reale hanno confermato aggiornamenti percorso WebSocket, gestione riavvio/riconnessione Home Assistant, ripristino snapshot e minore carico Recorder."],["Zone e dati di taglio","Il percorso serie M supporta mappa, percorso, zone e cronologia mantenendo la decodifica specifica separata da Genie e N8."],["Riscrittura nativa dei programmi","La creazione di un programma M9 Pro dalla scheda ANTHBOT Map è stata verificata su hardware reale e la regola appare nell'app ANTHBOT."],["Automazione Home Assistant","Stato del robot, prossimo taglio, eventi ciclo/programma e controlli supportati possono essere usati in dashboard e automazioni."]]},"/models/mgc1000":{"title":"Supporto ANTHBOT MGC1000 / Pion Home Assistant | ANTHBOT Map","description":"Supporto MGC1000 e Pion in ANTHBOT Map: rilevamento separato, normalizzazione stato, programmi nativi e routing di avvio.","eyebrow":"Famiglia Pion / MGC","heading":"ANTHBOT MGC1000 + Home Assistant","lead":"ANTHBOT Map tratta MGC500, MGC750 e MGC1000 come famiglia Pion/MGC dedicata invece di considerarli Genie.","cta":"Segui sviluppo Pion / MGC","sections":[["Gestione dedicata del modello","L'integrazione include rilevamento Pion/MGC separato e uno strato di normalizzazione flat-shadow per gli stati Home Assistant."],["Dati di stato confermati","L'implementazione attuale espone altezza di taglio, avanzamento e area, pioggia, Wi-Fi/IP, payload del percorso e firmware quando forniti dal robot/cloud."],["Programmi nativi e avvio","Pion/MGC usa un proprio formato di programma e percorso di avvio. L'integrazione conserva il comportamento un appuntamento al giorno/intero prato senza applicare payload Genie."],["Politica prudente delle capacità","Scritture impostazioni Pion/MGC e decodifica curpath non verificate restano disabilitate finché protocollo e hardware non sono confermati."]]},"/voice-packs":{"title":"Pacchetti voce ANTHBOT per Genie | ANTHBOT Map","description":"Pacchetti voce community e voci personalizzate per robot ANTHBOT Genie compatibili nell'ecosistema ANTHBOT Map.","eyebrow":"Pacchetti voce community","heading":"Pacchetti voce ANTHBOT e voci personalizzate","lead":"L'ecosistema ANTHBOT Map offre pacchetti voce community opzionali per robot ANTHBOT Genie compatibili e richieste di voce personalizzata.","cta":"Apri il negozio voci","sections":[["Pacchetti community pronti","I pacchetti disponibili sono elencati nell'ANTHBOT Community Voice Store. La compatibilità è mostrata per pacchetto e può variare in base a modello o firmware."],["Richieste di voce personalizzata","È disponibile un flusso separato per richieste non coperte dal catalogo pronto."],["Integrazione ANTHBOT Map","I diritti vocali acquistati possono essere collegati ad ANTHBOT Map così le installazioni compatibili riconoscono il pacchetto senza esporre pubblicamente gli URL di download a pagamento."],["Progetto indipendente","I pacchetti community e ANTHBOT Map sono funzioni di un progetto indipendente. ANTHBOT è un marchio del rispettivo titolare; questo sito non implica approvazione ufficiale."]]}}}}''')
_PUBLIC_EXPLORE_I18N = json.loads(r'''{"en":{"title":"Explore ANTHBOT Map topics","lead":"Detailed pages for Home Assistant integration, tested mower families and Community voice packs.","ha":"ANTHBOT Home Assistant","voice":"ANTHBOT Voice Packs"},"hu":{"title":"ANTHBOT Map témakörök","lead":"Részletes oldalak a Home Assistant integrációról, a tesztelt robotcsaládokról és a közösségi hangcsomagokról.","ha":"ANTHBOT Home Assistant","voice":"ANTHBOT hangcsomagok"},"de":{"title":"ANTHBOT-Map-Themen entdecken","lead":"Detaillierte Seiten zur Home-Assistant-Integration, getesteten Mäherfamilien und Community Voice Packs.","ha":"ANTHBOT Home Assistant","voice":"ANTHBOT Voice Packs"},"fr":{"title":"Explorer les thèmes ANTHBOT Map","lead":"Pages détaillées sur l'intégration Home Assistant, les familles de tondeuses testées et les packs vocaux communautaires.","ha":"ANTHBOT Home Assistant","voice":"Packs vocaux ANTHBOT"},"es":{"title":"Explorar temas de ANTHBOT Map","lead":"Páginas detalladas sobre la integración Home Assistant, familias de cortacéspedes probadas y paquetes de voz comunitarios.","ha":"ANTHBOT Home Assistant","voice":"Paquetes de voz ANTHBOT"},"it":{"title":"Esplora gli argomenti ANTHBOT Map","lead":"Pagine dettagliate sull'integrazione Home Assistant, le famiglie di tagliaerba testate e i pacchetti voce community.","ha":"ANTHBOT Home Assistant","voice":"Pacchetti voce ANTHBOT"}}''')
_PUBLIC_LANGUAGE_OPTIONS = (
    ("hu", "Magyar"),
    ("en", "English"),
    ("de", "Deutsch"),
    ("fr", "Français"),
    ("es", "Español"),
    ("it", "Italiano"),
)

_SEO_LANDING_PAGES: dict[str, dict[str, Any]] = {
    "/home-assistant": {
        "title": "ANTHBOT Home Assistant Integration | ANTHBOT Map",
        "description": (
            "Connect supported ANTHBOT robotic lawn mowers to Home Assistant with "
            "ANTHBOT Map: live map, zones, native schedules, mower controls, history "
            "and diagnostics."
        ),
        "eyebrow": "Home Assistant integration",
        "heading": "ANTHBOT in Home Assistant with ANTHBOT Map",
        "lead": (
            "ANTHBOT Map is an independent, open-source Home Assistant integration "
            "and Lovelace map card for supported ANTHBOT robotic lawn mowers."
        ),
        "sections": [
            (
                "What the integration adds",
                "ANTHBOT Map connects Home Assistant to the ANTHBOT cloud, creates a "
                "native lawn_mower entity, mirrors supported ANTHBOT app schedules, "
                "and provides model-aware controls instead of forcing every mower "
                "through one generic command path.",
            ),
            (
                "Live map and lawn data",
                "Where supported by the mower family, the card can render the lawn "
                "boundary, mowing zones, No-Go areas, mower position, live path and "
                "mowing coverage. A dedicated WebSocket live-map transport keeps "
                "high-frequency geometry out of Home Assistant Recorder.",
            ),
            (
                "Schedules and automations",
                "Native app schedules can be mirrored into Home Assistant and, on "
                "supported models, edited with write-back. Per-mower next-mow data, "
                "native mower events and timed mow/park overrides can be used in "
                "Home Assistant automations.",
            ),
            (
                "Model-aware design",
                "Genie, M-series, N8 and Pion/MGC devices use separated model routing. "
                "Capabilities remain conservative when a command or protocol detail "
                "has not been confirmed.",
            ),
        ],
        "cta": ("View ANTHBOT Map on GitHub", "https://github.com/Mqbretrofit/ha-anthbot-map-v2"),
    },
    "/models/genie-1000": {
        "title": "ANTHBOT Genie 1000 Home Assistant Support | ANTHBOT Map",
        "description": (
            "ANTHBOT Genie 1000 support in Home Assistant with ANTHBOT Map, including "
            "live map/path data, zones, schedules, history, mower controls and diagnostics."
        ),
        "eyebrow": "Supported mower",
        "heading": "ANTHBOT Genie 1000 + Home Assistant",
        "lead": (
            "The Genie family is supported by ANTHBOT Map and has been directly "
            "hardware-tested by the project, including Genie 1000 schedule loading."
        ),
        "sections": [
            (
                "Direct hardware validation",
                "The public ANTHBOT Map project documents direct real-device testing "
                "for the Genie family. Genie 1000 native app schedule loading has also "
                "been verified on real hardware.",
            ),
            (
                "Maps, zones and mowing history",
                "ANTHBOT Map keeps Genie-specific map/path diagnostics isolated from "
                "other mower families and exposes supported lawn boundary, zones, "
                "No-Go geometry, live mower position, path and historical mowing data.",
            ),
            (
                "Native scheduling",
                "The integration mirrors the mower's native ANTHBOT app schedule into "
                "Home Assistant and supports the model-specific schedule path rather "
                "than translating it through M-series behavior.",
            ),
            (
                "Home Assistant controls",
                "Supported operations include mower status and common mowing controls, "
                "with model-specific routing plus Battery Saver and diagnostic tooling "
                "where the underlying device capabilities are available.",
            ),
        ],
        "cta": ("Install / documentation", "https://github.com/Mqbretrofit/ha-anthbot-map-v2"),
    },
    "/models/m9-pro": {
        "title": "ANTHBOT M9 Pro Home Assistant Integration | ANTHBOT Map",
        "description": (
            "ANTHBOT M9 Pro support for Home Assistant with control, status, live map, "
            "path, zones, mowing history, schedules and diagnostics."
        ),
        "eyebrow": "Directly hardware-tested",
        "heading": "ANTHBOT M9 Pro + Home Assistant",
        "lead": (
            "ANTHBOT Map includes a dedicated M-series implementation, and M9 Pro "
            "control, status, map, path, zone and history handling have been directly "
            "hardware-tested by the project."
        ),
        "sections": [
            (
                "Live map architecture",
                "Real-device M9 Pro validation confirmed live WebSocket path updates, "
                "Home Assistant restart and reconnect handling, snapshot restore and "
                "reduced Recorder churn.",
            ),
            (
                "Zones and mowing data",
                "The M-series path supports map, path, zone and history handling while "
                "keeping model-specific decoding separate from Genie and N8.",
            ),
            (
                "Native schedule write-back",
                "Creating an M9 Pro schedule from the ANTHBOT Map card has been "
                "verified on real hardware, with the created rule appearing in the "
                "ANTHBOT app.",
            ),
            (
                "Home Assistant automation",
                "Mower state, next-mow information, lifecycle/schedule events and "
                "supported controls can be used in dashboards and automations.",
            ),
        ],
        "cta": ("View M9 Pro integration documentation", "https://github.com/Mqbretrofit/ha-anthbot-map-v2"),
    },
    "/models/mgc1000": {
        "title": "ANTHBOT MGC1000 / Pion Home Assistant Support | ANTHBOT Map",
        "description": (
            "ANTHBOT MGC1000 and Pion-family support in ANTHBOT Map for Home Assistant: "
            "isolated model detection, status normalization, native schedules and start routing."
        ),
        "eyebrow": "Pion / MGC family",
        "heading": "ANTHBOT MGC1000 + Home Assistant",
        "lead": (
            "ANTHBOT Map has a dedicated Pion/MGC model family for identifiers such "
            "as MGC500, MGC750 and MGC1000, instead of treating these mowers as Genie."
        ),
        "sections": [
            (
                "Dedicated model handling",
                "The integration includes isolated Pion/MGC detection and a flat-shadow "
                "normalization layer for Home Assistant status data.",
            ),
            (
                "Confirmed status data",
                "The current implementation exposes confirmed cutting height, mowing "
                "progress and area, rain state, Wi-Fi/IP, path payload and firmware data "
                "when supplied by the mower/cloud.",
            ),
            (
                "Native schedules and start routing",
                "Pion/MGC uses its own native schedule shape and start path. The "
                "integration preserves its one-appointment-per-day/full-lawn schedule "
                "behavior instead of applying Genie-only payloads.",
            ),
            (
                "Conservative capability policy",
                "Unverified Pion/MGC setting writes and curpath decoding remain "
                "intentionally disabled until protocol and hardware behavior are confirmed.",
            ),
        ],
        "cta": ("Follow Pion / MGC development", "https://github.com/Mqbretrofit/ha-anthbot-map-v2"),
    },
    "/voice-packs": {
        "title": "ANTHBOT Voice Packs for Genie Mowers | ANTHBOT Map",
        "description": (
            "ANTHBOT community voice packs and custom mower voices for compatible "
            "ANTHBOT Genie robots, integrated with the ANTHBOT Map ecosystem."
        ),
        "eyebrow": "Community voice packs",
        "heading": "ANTHBOT voice packs and custom mower voices",
        "lead": (
            "The ANTHBOT Map ecosystem includes optional Community voice packs for "
            "compatible ANTHBOT Genie robots, with ready-made packs and custom voice requests."
        ),
        "sections": [
            (
                "Ready-made Community packs",
                "Available voice packs are listed in the ANTHBOT Community Voice Store. "
                "Compatibility is shown with the pack and can vary by mower model or firmware.",
            ),
            (
                "Custom voice requests",
                "A separate custom-voice workflow is available for requests that are "
                "not covered by the ready-made catalogue.",
            ),
            (
                "ANTHBOT Map integration",
                "Purchased voice entitlements can be linked to ANTHBOT Map so compatible "
                "installed systems can recognize the purchased pack without exposing paid "
                "download URLs publicly.",
            ),
            (
                "Independent project",
                "Community voice packs and ANTHBOT Map are independent project features. "
                "ANTHBOT is a trademark of its respective owner; this site does not imply "
                "official ANTHBOT endorsement.",
            ),
        ],
        "cta": ("Open the Voice Pack Store", "/store"),
    },
}

# Stripe Managed Payments requires an eligible product tax code. Community
# voice packs are one-time downloadable digital audio with permanent access.
_VOICE_PACK_TAX_CODE = "txcd_10401100"

_LOGGER = logging.getLogger(__name__)


class CheckoutPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pack_id: str = Field(min_length=1, max_length=160)
    pair_code: str | None = Field(default=None, min_length=20, max_length=160)


class StoreClientCheckoutPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_token: str = Field(min_length=32, max_length=512)
    pack_id: str = Field(min_length=1, max_length=160)

    @field_validator("client_token")
    @classmethod
    def _validate_client_token(cls, value: str) -> str:
        normalized = value.strip()
        if not _CLIENT_TOKEN_RE.fullmatch(normalized):
            raise ValueError("invalid store client token")
        return normalized

    @field_validator("pack_id")
    @classmethod
    def _validate_pack_id(cls, value: str) -> str:
        return value.strip()


class StoreClientPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_token: str = Field(min_length=32, max_length=512)

    @field_validator("client_token")
    @classmethod
    def _validate_client_token(cls, value: str) -> str:
        normalized = value.strip()
        if not _CLIENT_TOKEN_RE.fullmatch(normalized):
            raise ValueError("invalid store client token")
        return normalized


class EntitlementPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    license_key: str = Field(min_length=20, max_length=1024)


class StorePricingPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access: Literal["free", "paid"]
    price_amount: int = Field(default=0, ge=0, le=100_000_000)
    currency: str = Field(default="eur", min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _CURRENCY_RE.fullmatch(normalized):
            raise ValueError("currency must be a three-letter ISO code")
        return normalized

    @model_validator(mode="after")
    def _validate_paid_price(self) -> "StorePricingPayload":
        if self.access == "paid" and self.price_amount <= 0:
            raise ValueError("paid voice packs require price_amount > 0")
        return self


class StoreVisibilityPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hidden: bool


class CustomVoiceRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_language: str = Field(min_length=1, max_length=64)
    voice_style: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=128)
    contact: str = Field(min_length=3, max_length=254)
    notes: str = Field(default="", max_length=2000)
    site_language: str = Field(default="en", min_length=2, max_length=16)

    @field_validator(
        "requested_language",
        "voice_style",
        "model",
        "contact",
        "notes",
        "site_language",
    )
    @classmethod
    def _strip_custom_request_text(cls, value: str) -> str:
        return value.strip()


class SiteVisitPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=64)
    language: str = Field(default="en", min_length=2, max_length=16)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        normalized = value.strip()
        if normalized not in _ANALYTICS_ALLOWED_PATHS:
            raise ValueError("unsupported analytics path")
        return normalized

    @field_validator("language")
    @classmethod
    def _validate_language(cls, value: str) -> str:
        normalized = value.strip()
        return normalized if normalized in _ANALYTICS_LANGUAGES else "en"


class PrivacyRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_type: Literal[
        "access",
        "rectification",
        "erasure",
        "restriction",
        "portability",
        "objection",
        "withdraw_consent",
        "complaint",
        "other",
    ]
    contact: str = Field(min_length=3, max_length=254)
    details: str = Field(default="", max_length=3000)
    site_language: str = Field(default="en", min_length=2, max_length=16)

    @field_validator("contact", "details", "site_language")
    @classmethod
    def _strip_privacy_request_text(cls, value: str) -> str:
        return value.strip()


def _iso_from_epoch(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return core._iso()


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _store_enabled() -> bool:
    return _env_flag("ANTHBOT_STORE_ENABLED", False)


def _stripe_secret_key() -> str:
    return os.environ.get("ANTHBOT_STRIPE_SECRET_KEY", "").strip()


def _stripe_webhook_secret() -> str:
    return os.environ.get("ANTHBOT_STRIPE_WEBHOOK_SECRET", "").strip()


def _license_secret() -> str:
    return os.environ.get("ANTHBOT_STORE_LICENSE_SECRET", "").strip()


def _stripe_automatic_tax() -> bool:
    return _env_flag("ANTHBOT_STRIPE_AUTOMATIC_TAX", False)


def _site_analytics_enabled() -> bool:
    return _env_flag("ANTHBOT_SITE_ANALYTICS_ENABLED", True)


def _privacy_controller_name() -> str:
    return os.environ.get(
        "ANTHBOT_PRIVACY_CONTROLLER_NAME",
        "MQB Retrofit Hungary",
    ).strip() or "MQB Retrofit Hungary"


def _privacy_controller_address() -> str:
    return os.environ.get("ANTHBOT_PRIVACY_CONTROLLER_ADDRESS", "").strip()


def _privacy_contact_email() -> str:
    return os.environ.get("ANTHBOT_PRIVACY_CONTACT_EMAIL", "").strip()


def _privacy_contact_phone() -> str:
    return os.environ.get(
        "ANTHBOT_PRIVACY_CONTACT_PHONE",
        "+36 30 620 9015",
    ).strip() or "+36 30 620 9015"


def _checkout_ready() -> bool:
    stripe_key = _stripe_secret_key()
    base_ready = bool(
        _store_enabled()
        and stripe_key
        and _stripe_webhook_secret()
        and _license_secret()
    )
    if not base_ready:
        return False
    # Sandbox remains usable while legal details are being prepared. Live
    # commercial checkout requires the controller's postal address so the
    # public Article 13 notice cannot accidentally go live incomplete.
    if stripe_key.startswith("sk_live_") and not _privacy_controller_address():
        return False
    return True


def _require_checkout_ready() -> None:
    if not _store_enabled():
        raise HTTPException(status_code=503, detail="voice store checkout is disabled")
    if not _stripe_secret_key():
        raise HTTPException(status_code=503, detail="Stripe secret key is not configured")
    if not _stripe_webhook_secret():
        raise HTTPException(status_code=503, detail="Stripe webhook secret is not configured")
    if not _license_secret():
        raise HTTPException(status_code=503, detail="voice store license secret is not configured")
    if _stripe_secret_key().startswith("sk_live_") and not _privacy_controller_address():
        raise HTTPException(
            status_code=503,
            detail="privacy controller postal address must be configured before live checkout",
        )


def _init_store_tables() -> None:
    with core._db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS store_orders (
                stripe_session_id TEXT PRIMARY KEY,
                pack_id TEXT NOT NULL,
                community_id TEXT,
                status TEXT NOT NULL,
                payment_status TEXT NOT NULL,
                amount_total INTEGER,
                currency TEXT,
                customer_email TEXT,
                stripe_customer_id TEXT,
                stripe_payment_intent_id TEXT,
                client_id TEXT,
                entitlement_scope TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                paid_at TEXT
            );

            CREATE TABLE IF NOT EXISTS store_client_pairings (
                pair_code TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_store_orders_pack_id
                ON store_orders(pack_id);
            CREATE INDEX IF NOT EXISTS idx_store_orders_paid_at
                ON store_orders(paid_at);
            CREATE INDEX IF NOT EXISTS idx_store_pairings_client_id
                ON store_client_pairings(client_id);

            CREATE TABLE IF NOT EXISTS store_custom_voice_requests (
                request_id TEXT PRIMARY KEY,
                requested_language TEXT NOT NULL,
                voice_style TEXT NOT NULL,
                model TEXT NOT NULL,
                contact TEXT NOT NULL,
                notes TEXT NOT NULL,
                site_language TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_store_custom_voice_requests_created
                ON store_custom_voice_requests(created_at);

            CREATE TABLE IF NOT EXISTS privacy_requests (
                request_id TEXT PRIMARY KEY,
                request_type TEXT NOT NULL,
                contact TEXT NOT NULL,
                details TEXT NOT NULL,
                site_language TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_privacy_requests_created
                ON privacy_requests(created_at);
            CREATE INDEX IF NOT EXISTS idx_privacy_requests_status
                ON privacy_requests(status);

            CREATE TABLE IF NOT EXISTS site_analytics_views (
                day TEXT NOT NULL,
                path TEXT NOT NULL,
                language TEXT NOT NULL,
                views INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(day, path, language)
            );
            CREATE INDEX IF NOT EXISTS idx_site_analytics_views_day
                ON site_analytics_views(day);

            CREATE TABLE IF NOT EXISTS site_analytics_unique_visitors (
                day TEXT NOT NULL,
                visitor_hash TEXT NOT NULL,
                PRIMARY KEY(day, visitor_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_site_analytics_unique_day
                ON site_analytics_unique_visitors(day);

            CREATE TABLE IF NOT EXISTS site_analytics_unique_page_visitors (
                day TEXT NOT NULL,
                path TEXT NOT NULL,
                visitor_hash TEXT NOT NULL,
                PRIMARY KEY(day, path, visitor_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_site_analytics_unique_page_day
                ON site_analytics_unique_page_visitors(day);

            CREATE TABLE IF NOT EXISTS site_analytics_unique_language_visitors (
                day TEXT NOT NULL,
                language TEXT NOT NULL,
                visitor_hash TEXT NOT NULL,
                PRIMARY KEY(day, language, visitor_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_site_analytics_unique_language_day
                ON site_analytics_unique_language_visitors(day);
            """
        )

        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(store_orders)").fetchall()
        }
        if "client_id" not in columns:
            conn.execute("ALTER TABLE store_orders ADD COLUMN client_id TEXT")
        if "community_id" not in columns:
            conn.execute("ALTER TABLE store_orders ADD COLUMN community_id TEXT")
        if "stripe_checked_at" not in columns:
            conn.execute("ALTER TABLE store_orders ADD COLUMN stripe_checked_at INTEGER")
        if "entitlement_scope" not in columns:
            conn.execute("ALTER TABLE store_orders ADD COLUMN entitlement_scope TEXT")
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_store_orders_client_id
            ON store_orders(client_id);
            CREATE INDEX IF NOT EXISTS idx_store_orders_community_id
            ON store_orders(community_id);
            """
        )

        # Backfill older orders while their purchased pack is still present.
        records = {
            str(item.get("id", "")): str(item.get("community_id", "")).strip()
            for item in _uploaded_voice_pack_registry_records()
            if str(item.get("community_id", "")).strip()
        }
        for old_pack_id, community_id in records.items():
            conn.execute(
                """
                UPDATE store_orders
                SET community_id = ?
                WHERE pack_id = ?
                  AND (community_id IS NULL OR community_id = '')
                """,
                (community_id, old_pack_id),
            )


def _analytics_secret_path() -> Path:
    return core._db_path().with_name(".site_analytics_secret")


def _analytics_master_secret() -> bytes:
    """Load or create a private secret kept outside the analytics database."""
    path = _analytics_secret_path()
    try:
        raw = path.read_text(encoding="ascii").strip()
        secret = bytes.fromhex(raw)
        if len(secret) >= 32:
            return secret
    except (OSError, ValueError):
        pass

    secret = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(secret.hex(), encoding="ascii")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return secret


def _normalized_ip(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        return ipaddress.ip_address(candidate).compressed
    except ValueError:
        return None


def _analytics_source_ip(request: Request) -> str | None:
    """Resolve the source address for counting only; never persist the raw IP."""
    for header in ("cf-connecting-ip", "x-real-ip"):
        normalized = _normalized_ip(request.headers.get(header))
        if normalized:
            return normalized

    forwarded = request.headers.get("x-forwarded-for", "")
    for item in forwarded.split(","):
        normalized = _normalized_ip(item)
        if normalized:
            return normalized

    if request.client is not None:
        return _normalized_ip(request.client.host)
    return None


def _analytics_visitor_hash(request: Request, day: str) -> str:
    source_ip = _analytics_source_ip(request) or "source-unavailable"
    master = _analytics_master_secret()
    day_key = hmac.new(
        master,
        f"anthbot-site-analytics:v1:{day}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return hmac.new(
        day_key,
        source_ip.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]


def _analytics_cleanup(conn: Any, today: datetime) -> None:
    unique_cutoff = (
        today.date() - timedelta(days=_ANALYTICS_UNIQUE_RETENTION_DAYS - 1)
    ).isoformat()
    aggregate_cutoff = (
        today.date() - timedelta(days=_ANALYTICS_AGGREGATE_RETENTION_DAYS - 1)
    ).isoformat()
    conn.execute(
        "DELETE FROM site_analytics_unique_visitors WHERE day < ?",
        (unique_cutoff,),
    )
    conn.execute(
        "DELETE FROM site_analytics_unique_page_visitors WHERE day < ?",
        (unique_cutoff,),
    )
    conn.execute(
        "DELETE FROM site_analytics_unique_language_visitors WHERE day < ?",
        (unique_cutoff,),
    )
    conn.execute(
        "DELETE FROM site_analytics_views WHERE day < ?",
        (aggregate_cutoff,),
    )


def _record_site_visit(
    request: Request,
    *,
    path: str,
    language: str,
) -> None:
    if not _site_analytics_enabled():
        return

    _init_store_tables()
    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    visitor_hash = _analytics_visitor_hash(request, day)
    with core._db() as conn:
        conn.execute(
            """
            INSERT INTO site_analytics_views(day, path, language, views)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(day, path, language)
            DO UPDATE SET views = views + 1
            """,
            (day, path, language),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO site_analytics_unique_visitors(day, visitor_hash)
            VALUES (?, ?)
            """,
            (day, visitor_hash),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO site_analytics_unique_page_visitors(
                day, path, visitor_hash
            ) VALUES (?, ?, ?)
            """,
            (day, path, visitor_hash),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO site_analytics_unique_language_visitors(
                day, language, visitor_hash
            ) VALUES (?, ?, ?)
            """,
            (day, language, visitor_hash),
        )
        _analytics_cleanup(conn, now)


def _uploaded_voice_pack_registry_records() -> list[dict[str, Any]]:
    return [
        item
        for item in core._uploaded_voice_pack_registry().get("packs", [])
        if isinstance(item, dict)
    ]


def _client_id_from_token(client_token: str) -> str:
    """Derive a stable opaque client ID without storing the bearer token."""
    normalized = client_token.strip()
    if not _CLIENT_TOKEN_RE.fullmatch(normalized):
        raise HTTPException(status_code=422, detail="invalid store client token")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"abvc_{digest[:40]}"


def _browser_store_token(request: Request) -> str | None:
    value = request.cookies.get(_STORE_BROWSER_COOKIE, "").strip()
    return value if _CLIENT_TOKEN_RE.fullmatch(value) else None


def _browser_store_client_id(request: Request) -> str | None:
    token = _browser_store_token(request)
    return _client_id_from_token(token) if token else None


def _set_browser_store_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        _STORE_BROWSER_COOKIE,
        token,
        max_age=_STORE_BROWSER_COOKIE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def _create_store_pairing(client_token: str) -> tuple[str, int]:
    """Create a temporary browser pairing code for one ANTHBOT Map install."""
    _init_store_tables()
    client_id = _client_id_from_token(client_token)
    pair_code = f"abp_{secrets.token_urlsafe(32)}"
    expires_at = int(time.time()) + _STORE_PAIR_TTL_SECONDS
    now = core._iso()
    with core._db() as conn:
        conn.execute(
            "DELETE FROM store_client_pairings WHERE expires_at < ?",
            (int(time.time()),),
        )
        conn.execute(
            """
            INSERT INTO store_client_pairings (
                pair_code, client_id, created_at, expires_at
            ) VALUES (?, ?, ?, ?)
            """,
            (pair_code, client_id, now, expires_at),
        )
    return pair_code, expires_at


def _client_id_from_pairing(pair_code: str | None) -> str | None:
    """Resolve a non-secret browser pairing code to one Map client."""
    if pair_code is None:
        return None
    normalized = pair_code.strip()
    if not _PAIR_RE.fullmatch(normalized):
        raise HTTPException(status_code=422, detail="invalid store pairing code")

    _init_store_tables()
    with core._db() as conn:
        row = conn.execute(
            """
            SELECT client_id, expires_at
            FROM store_client_pairings
            WHERE pair_code = ?
            """,
            (normalized,),
        ).fetchone()
    if row is None or int(row["expires_at"]) < int(time.time()):
        raise HTTPException(status_code=401, detail="voice store pairing expired")
    return str(row["client_id"])


def _reconcile_paid_order_with_stripe(
    order: dict[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Best-effort fallback for refunds missed by the Stripe webhook.

    Live entitlements remain webhook-driven for fast updates, but a paid order
    is periodically rechecked against Stripe so a missed webhook cannot leave a
    refunded voice permanently unlocked.
    """
    if str(order.get("payment_status", "")).casefold() != "paid":
        return order

    secret_key = _stripe_secret_key()
    payment_intent_id = str(order.get("stripe_payment_intent_id") or "").strip()
    if not secret_key.startswith("sk_live_") or not payment_intent_id:
        return order

    now_epoch = int(time.time())
    try:
        checked_at = int(order.get("stripe_checked_at") or 0)
    except (TypeError, ValueError):
        checked_at = 0
    if (
        not force
        and checked_at > 0
        and now_epoch - checked_at < _STRIPE_REFUND_RECONCILE_SECONDS
    ):
        return order

    _configure_stripe()
    try:
        payment_intent = stripe.PaymentIntent.retrieve(
            payment_intent_id,
            expand=["latest_charge"],
        )
        intent_payload = _stripe_session_dict(payment_intent)
        latest_charge = intent_payload.get("latest_charge")
        if isinstance(latest_charge, str) and latest_charge:
            latest_charge = _stripe_session_dict(stripe.Charge.retrieve(latest_charge))
    except stripe.StripeError as err:
        _LOGGER.debug(
            "Stripe refund reconciliation failed for %s: %s",
            payment_intent_id,
            _stripe_error_detail(err),
        )
        return order
    except Exception as err:
        _LOGGER.debug(
            "Unexpected Stripe refund reconciliation failure for %s: %s",
            payment_intent_id,
            err,
        )
        return order

    refunded = False
    if isinstance(latest_charge, dict):
        try:
            amount_refunded = int(latest_charge.get("amount_refunded") or 0)
        except (TypeError, ValueError):
            amount_refunded = 0
        refunded = bool(latest_charge.get("refunded")) or amount_refunded > 0

    _init_store_tables()
    with core._db() as conn:
        if refunded:
            conn.execute(
                """
                UPDATE store_orders
                SET status = 'refunded',
                    payment_status = 'refunded',
                    updated_at = ?,
                    stripe_checked_at = ?
                WHERE stripe_session_id = ?
                """,
                (
                    core._iso(),
                    now_epoch,
                    str(order.get("stripe_session_id") or ""),
                ),
            )
        else:
            conn.execute(
                """
                UPDATE store_orders
                SET stripe_checked_at = ?
                WHERE stripe_session_id = ?
                """,
                (
                    now_epoch,
                    str(order.get("stripe_session_id") or ""),
                ),
            )
        row = conn.execute(
            "SELECT * FROM store_orders WHERE stripe_session_id = ?",
            (str(order.get("stripe_session_id") or ""),),
        ).fetchone()

    return dict(row) if row is not None else order


def _paid_order_for_client_pack(
    client_id: str,
    record: dict[str, Any],
    *,
    entitlement_scope: str | None = None,
) -> dict[str, Any] | None:
    """Return an active purchase for one client and entitlement surface."""
    _init_store_tables()
    pack_id = str(record.get("id", "")).strip()
    community_id = str(record.get("community_id", "")).strip()
    with core._db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM store_orders
            WHERE client_id = ?
              AND payment_status = 'paid'
              AND (
                    ? IS NULL
                    OR entitlement_scope = ?
                    OR entitlement_scope IS NULL
                  )
              AND (
                    (? != '' AND community_id = ?)
                    OR pack_id = ?
                  )
            ORDER BY COALESCE(paid_at, updated_at) DESC
            LIMIT 1
            """,
            (
                client_id,
                entitlement_scope,
                entitlement_scope,
                community_id,
                community_id,
                pack_id,
            ),
        ).fetchone()
    if row is None:
        return None
    order = _reconcile_paid_order_with_stripe(dict(row))
    if str(order.get("payment_status", "")).casefold() != "paid":
        return None
    return order


def _uploaded_records() -> list[dict[str, Any]]:
    return _uploaded_voice_pack_registry_records()


def _is_paid(record: dict[str, Any]) -> bool:
    return str(record.get("access", "free")).strip().casefold() == "paid"


def _is_store_hidden(record: dict[str, Any]) -> bool:
    return bool(record.get("store_hidden", False))


def _price_amount(record: dict[str, Any]) -> int:
    if _is_paid(record):
        return _STANDARD_VOICE_PACK_PRICE_AMOUNT
    return 0


def _currency(record: dict[str, Any]) -> str:
    if _is_paid(record):
        return _STANDARD_VOICE_PACK_CURRENCY
    value = str(record.get("currency", "eur")).strip().lower()
    return value if _CURRENCY_RE.fullmatch(value) else "eur"


def _find_uploaded_pack(pack_id: str) -> dict[str, Any]:
    match = next(
        (item for item in _uploaded_records() if str(item.get("id", "")) == pack_id),
        None,
    )
    if match is None:
        raise HTTPException(status_code=404, detail="voice pack not found")
    return match


def _find_uploaded_pack_by_community_id(
    community_id: str,
) -> dict[str, Any]:
    normalized = community_id.strip()
    match = next(
        (
            item
            for item in _uploaded_records()
            if str(item.get("community_id", "")).strip() == normalized
        ),
        None,
    )
    if match is None:
        raise HTTPException(status_code=404, detail="voice pack not found")
    return match


def _resolve_order_pack(order: dict[str, Any]) -> dict[str, Any]:
    """Resolve an old purchase to the current version of the same voice."""
    community_id = str(order.get("community_id", "")).strip()
    if community_id:
        try:
            return _find_uploaded_pack_by_community_id(community_id)
        except HTTPException:
            pass
    return _find_uploaded_pack(str(order.get("pack_id", "")).strip())


def _order_covers_pack(
    order: dict[str, Any],
    pack: dict[str, Any],
) -> bool:
    order_community_id = str(order.get("community_id", "")).strip()
    pack_community_id = str(pack.get("community_id", "")).strip()
    if order_community_id and pack_community_id:
        return secrets.compare_digest(order_community_id, pack_community_id)
    return secrets.compare_digest(
        str(order.get("pack_id", "")).strip(),
        str(pack.get("id", "")).strip(),
    )


def _public_paid_pack(record: dict[str, Any], request: Request) -> dict[str, Any]:
    public = dict(record)
    public.pop("filename", None)
    public.pop("uploaded_at", None)
    public.pop("music_url", None)
    public["access"] = "paid"
    public["price_amount"] = _price_amount(record)
    public["currency"] = _currency(record)
    public["checkout_available"] = _checkout_ready()
    public["store_url"] = f"{core._public_base_url(request)}/store"
    return public


def _public_free_pack(record: dict[str, Any]) -> dict[str, Any]:
    public = dict(record)
    public["access"] = "free"
    public["price_amount"] = 0
    public["currency"] = None
    public["checkout_available"] = False
    return public


def _store_catalog(request: Request) -> dict[str, Any]:
    free_registry = core._voice_pack_registry(request)
    free_packs = [
        _public_free_pack(item)
        for item in free_registry.get("packs", [])
        if isinstance(item, dict) and not _is_store_hidden(item)
    ]
    paid_packs = [
        _public_paid_pack(item, request)
        for item in _uploaded_records()
        if _is_paid(item) and not _is_store_hidden(item)
    ]
    packs = free_packs + paid_packs
    packs.sort(
        key=lambda item: (
            str(item.get("language", "")).casefold(),
            str(item.get("variant_name", "")).casefold(),
            str(item.get("id", "")).casefold(),
        )
    )
    return {
        "schema": STORE_SCHEMA,
        "checkout_available": _checkout_ready(),
        "web_installer_available": _env_flag("ANTHBOT_WEB_VOICE_INSTALLER_ENABLED", False),
        "packs": packs,
    }


def _stripe_session_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)

    # stripe-python returns StripeObject instances. Current releases expose
    # .to_dict(); older releases used .to_dict_recursive().
    for method_name in ("to_dict", "to_dict_recursive"):
        converter = getattr(value, method_name, None)
        if not callable(converter):
            continue
        try:
            payload = converter()
        except TypeError:
            continue
        if isinstance(payload, dict):
            return payload

    raise HTTPException(
        status_code=502,
        detail=(
            "Stripe returned an unsupported Checkout Session object "
            f"({type(value).__name__})"
        ),
    )


def _stripe_error_detail(err: BaseException) -> str:
    user_message = getattr(err, "user_message", None)
    if isinstance(user_message, str) and user_message.strip():
        return user_message.strip()[:500]
    message = str(err).strip()
    if message:
        return message[:500]
    return "Stripe request failed"


def _configure_stripe() -> None:
    key = _stripe_secret_key()
    if not key:
        raise HTTPException(status_code=503, detail="Stripe secret key is not configured")
    stripe.api_key = key
    stripe.max_network_retries = 1


def _create_checkout_session(
    record: dict[str, Any],
    request: Request,
    *,
    client_id: str | None = None,
    pair_code: str | None = None,
    entitlement_scope: str | None = None,
    success_url_override: str | None = None,
    cancel_url_override: str | None = None,
) -> dict[str, Any]:
    amount = _price_amount(record)
    currency = _currency(record)
    if amount <= 0:
        raise HTTPException(status_code=409, detail="voice pack price is not configured")

    base = core._public_base_url(request)
    pack_id = str(record.get("id", "")).strip()
    community_id = str(record.get("community_id", "")).strip()
    language = str(record.get("language", "")).strip() or "ANTHBOT"
    variant = str(record.get("variant_name", "")).strip()
    product_name = (
        f"{language} · {variant}"
        if variant
        else f"{language} · Community voice"
    )

    cancel_url = cancel_url_override or f"{base}/store?cancelled=1"
    if pair_code and cancel_url_override is None:
        cancel_url = f"{cancel_url}&pair={quote(pair_code)}"
    success_url = (
        success_url_override
        or f"{base}/store/success?session_id={{CHECKOUT_SESSION_ID}}"
    )

    metadata = {
        "pack_id": pack_id,
        "community_id": community_id,
    }
    if client_id:
        metadata["store_client_id"] = client_id
    if entitlement_scope in {"map", "web"}:
        metadata["entitlement_scope"] = entitlement_scope

    payment_metadata = {
        "pack_id": pack_id,
        "community_id": community_id,
    }
    if client_id:
        payment_metadata["store_client_id"] = client_id
    if entitlement_scope in {"map", "web"}:
        payment_metadata["entitlement_scope"] = entitlement_scope

    params: dict[str, Any] = {
        "mode": "payment",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": pack_id,
        "customer_creation": "always",
        "locale": "auto",
        "managed_payments": {"enabled": False},
        "line_items": [
            {
                "price_data": {
                    "currency": currency,
                    "unit_amount": amount,
                    "product_data": {
                        "name": product_name,
                        "description": "ANTHBOT Community voice pack · one-time purchase",
                        "tax_code": _VOICE_PACK_TAX_CODE,
                    },
                },
                "quantity": 1,
            }
        ],
        "metadata": metadata,
        "payment_intent_data": {
            "metadata": payment_metadata,
        },
    }
    if _stripe_automatic_tax():
        params["automatic_tax"] = {"enabled": True}

    _configure_stripe()
    try:
        session = stripe.checkout.Session.create(**params)
    except stripe.StripeError as err:
        detail = _stripe_error_detail(err)
        _LOGGER.warning(
            "Stripe Checkout Session creation failed: %s (request_id=%s)",
            detail,
            getattr(err, "request_id", None),
        )
        raise HTTPException(status_code=502, detail=f"Stripe: {detail}") from err
    except Exception as err:
        _LOGGER.exception("Unexpected Stripe Checkout Session creation failure")
        raise HTTPException(
            status_code=502,
            detail=f"Stripe connection failed: {type(err).__name__}",
        ) from err
    return _stripe_session_dict(session)


def _retrieve_checkout_session(session_id: str) -> dict[str, Any]:
    if not _SESSION_RE.fullmatch(session_id):
        raise HTTPException(status_code=422, detail="invalid checkout session id")

    _configure_stripe()
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except stripe.StripeError as err:
        detail = _stripe_error_detail(err)
        _LOGGER.warning(
            "Stripe Checkout Session retrieval failed: %s (request_id=%s)",
            detail,
            getattr(err, "request_id", None),
        )
        raise HTTPException(status_code=502, detail=f"Stripe: {detail}") from err
    except Exception as err:
        _LOGGER.exception("Unexpected Stripe Checkout Session retrieval failure")
        raise HTTPException(
            status_code=502,
            detail=f"Stripe connection failed: {type(err).__name__}",
        ) from err
    return _stripe_session_dict(session)


def _session_pack_id(session: dict[str, Any]) -> str:
    metadata = session.get("metadata")
    if isinstance(metadata, dict):
        pack_id = str(metadata.get("pack_id", "")).strip()
        if pack_id:
            return pack_id
    return str(session.get("client_reference_id", "")).strip()


def _session_community_id(session: dict[str, Any]) -> str | None:
    metadata = session.get("metadata")
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("community_id")
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized[:160] if normalized else None


def _session_client_id(session: dict[str, Any]) -> str | None:
    metadata = session.get("metadata")
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("store_client_id")
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized.startswith("abvc_") or len(normalized) > 64:
        return None
    return normalized


def _session_entitlement_scope(session: dict[str, Any]) -> str | None:
    metadata = session.get("metadata")
    if not isinstance(metadata, dict):
        return None
    value = str(metadata.get("entitlement_scope") or "").strip().casefold()
    return value if value in {"map", "web"} else None


def _session_customer_email(session: dict[str, Any]) -> str | None:
    details = session.get("customer_details")
    if isinstance(details, dict):
        email = details.get("email")
        if isinstance(email, str) and email.strip():
            return email.strip()[:320]
    email = session.get("customer_email")
    if isinstance(email, str) and email.strip():
        return email.strip()[:320]
    return None


def _upsert_order_from_session(session: dict[str, Any]) -> dict[str, Any]:
    _init_store_tables()
    session_id = str(session.get("id", "")).strip()
    if not _SESSION_RE.fullmatch(session_id):
        raise HTTPException(status_code=422, detail="invalid Stripe checkout session")

    pack_id = _session_pack_id(session)
    if not pack_id:
        raise HTTPException(status_code=422, detail="checkout session is missing pack_id")

    community_id = _session_community_id(session)
    if not community_id:
        try:
            community_id = str(
                _find_uploaded_pack(pack_id).get("community_id", "")
            ).strip() or None
        except HTTPException:
            community_id = None

    payment_status = str(session.get("payment_status", "unpaid")).strip().lower() or "unpaid"
    checkout_status = str(session.get("status", "open")).strip().lower() or "open"
    status_value = "paid" if payment_status == "paid" else checkout_status
    amount_total = session.get("amount_total")
    try:
        amount_total = int(amount_total) if amount_total is not None else None
    except (TypeError, ValueError):
        amount_total = None
    currency = str(session.get("currency", "")).strip().lower() or None
    customer_email = _session_customer_email(session)
    customer_id = session.get("customer")
    customer_id = str(customer_id)[:255] if customer_id else None
    payment_intent = session.get("payment_intent")
    payment_intent = str(payment_intent)[:255] if payment_intent else None
    client_id = _session_client_id(session)
    entitlement_scope = _session_entitlement_scope(session)
    created_at = _iso_from_epoch(session.get("created"))
    now = core._iso()

    with core._db() as conn:
        existing = conn.execute(
            "SELECT paid_at FROM store_orders WHERE stripe_session_id = ?",
            (session_id,),
        ).fetchone()
        paid_at = (
            existing["paid_at"]
            if existing is not None and existing["paid_at"]
            else (now if payment_status == "paid" else None)
        )
        conn.execute(
            """
            INSERT INTO store_orders (
                stripe_session_id, pack_id, community_id, status, payment_status,
                amount_total, currency, customer_email, stripe_customer_id,
                stripe_payment_intent_id, client_id, entitlement_scope,
                created_at, updated_at, paid_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stripe_session_id) DO UPDATE SET
                pack_id=excluded.pack_id,
                community_id=COALESCE(excluded.community_id, store_orders.community_id),
                status=excluded.status,
                payment_status=excluded.payment_status,
                amount_total=excluded.amount_total,
                currency=excluded.currency,
                customer_email=excluded.customer_email,
                stripe_customer_id=excluded.stripe_customer_id,
                stripe_payment_intent_id=excluded.stripe_payment_intent_id,
                client_id=COALESCE(excluded.client_id, store_orders.client_id),
                entitlement_scope=COALESCE(
                    excluded.entitlement_scope,
                    store_orders.entitlement_scope
                ),
                updated_at=excluded.updated_at,
                paid_at=COALESCE(store_orders.paid_at, excluded.paid_at)
            """,
            (
                session_id,
                pack_id,
                community_id,
                status_value,
                payment_status,
                amount_total,
                currency,
                customer_email,
                customer_id,
                payment_intent,
                client_id,
                entitlement_scope,
                created_at,
                now,
                paid_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM store_orders WHERE stripe_session_id = ?",
            (session_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=500, detail="could not persist store order")
    return dict(row)


def _order_by_session(session_id: str) -> dict[str, Any] | None:
    _init_store_tables()
    with core._db() as conn:
        row = conn.execute(
            "SELECT * FROM store_orders WHERE stripe_session_id = ?",
            (session_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def _license_for_order(order: dict[str, Any]) -> str:
    secret = _license_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="voice store license secret is not configured")
    session_id = str(order.get("stripe_session_id", "")).strip()
    pack_id = str(order.get("pack_id", "")).strip()
    encoded = base64.urlsafe_b64encode(session_id.encode("utf-8")).decode("ascii").rstrip("=")
    signature = hmac.new(
        secret.encode("utf-8"),
        f"{session_id}|{pack_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"abv1.{encoded}.{signature}"


def _order_from_license(license_key: str) -> dict[str, Any]:
    match = _LICENSE_RE.fullmatch(license_key.strip())
    if not match:
        raise HTTPException(status_code=401, detail="invalid voice store license")
    encoded, supplied_signature = match.groups()
    padding = "=" * ((4 - len(encoded) % 4) % 4)
    try:
        session_id = base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=401, detail="invalid voice store license")

    order = _order_by_session(session_id)
    if order is not None:
        order = _reconcile_paid_order_with_stripe(order)
    if order is None or str(order.get("payment_status", "")).casefold() != "paid":
        raise HTTPException(status_code=401, detail="voice store license is not active")

    expected = _license_for_order(order)
    expected_signature = expected.rsplit(".", 1)[1]
    if not secrets.compare_digest(supplied_signature, expected_signature):
        raise HTTPException(status_code=401, detail="invalid voice store license")
    return order


def _order_public(order: dict[str, Any], request: Request) -> dict[str, Any]:
    pack = _resolve_order_pack(order)
    paid = str(order.get("payment_status", "")).casefold() == "paid"
    scope = str(order.get("entitlement_scope") or "").strip().casefold()
    legacy_linked = not scope and bool(order.get("client_id"))
    body: dict[str, Any] = {
        "stripe_session_id": order.get("stripe_session_id"),
        "pack_id": order.get("pack_id"),
        "status": order.get("status"),
        "payment_status": order.get("payment_status"),
        "amount_total": order.get("amount_total"),
        "currency": order.get("currency"),
        "customer_email": order.get("customer_email"),
        "paid_at": order.get("paid_at"),
        "entitlement_scope": scope or None,
        "map_linked": scope == "map" or legacy_linked,
        "web_installer_linked": scope == "web",
        "pack": _public_paid_pack(pack, request),
    }
    if scope == "web":
        body["installer_url"] = (
            f"{core._public_base_url(request)}/voice-installer"
            f"?pack={quote(str(pack.get('id') or order.get('pack_id') or ''))}"
        )
    elif paid:
        body["license_key"] = _license_for_order(order)
    return body


def _verify_stripe_signature(body: bytes, signature_header: str | None) -> None:
    secret = _stripe_webhook_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="Stripe webhook secret is not configured")
    if not signature_header:
        raise HTTPException(status_code=400, detail="missing Stripe-Signature")

    timestamp: int | None = None
    signatures: list[str] = []
    for part in signature_header.split(","):
        key, sep, value = part.partition("=")
        if not sep:
            continue
        if key.strip() == "t":
            try:
                timestamp = int(value.strip())
            except ValueError:
                timestamp = None
        elif key.strip() == "v1":
            signatures.append(value.strip())

    if timestamp is None or not signatures:
        raise HTTPException(status_code=400, detail="invalid Stripe-Signature")
    if abs(int(time.time()) - timestamp) > _STRIPE_WEBHOOK_TOLERANCE_SECONDS:
        raise HTTPException(status_code=400, detail="stale Stripe webhook signature")

    signed_payload = str(timestamp).encode("ascii") + b"." + body
    expected = hmac.new(
        secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    if not any(secrets.compare_digest(expected, candidate) for candidate in signatures):
        raise HTTPException(status_code=400, detail="invalid Stripe webhook signature")


def _canonical_public_redirect(
    request: Request,
    path: str,
) -> RedirectResponse | None:
    forwarded_host = (
        request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    )
    host = forwarded_host or request.headers.get("host", "")
    host = host.split(":", 1)[0].strip().casefold()
    if host not in _LEGACY_PUBLIC_HOSTS:
        return None

    target = f"{_PUBLIC_SITE_BASE_URL}{path}"
    query = request.url.query
    if query:
        target = f"{target}?{query}"
    return RedirectResponse(url=target, status_code=301)


def _apply_seo_metadata(name: str, html: str) -> str:
    page = _SEO_PAGES.get(name)
    if not page or "</head>" not in html:
        return html

    title = str(page["title"])
    description = str(page["description"])
    canonical = f"{_PUBLIC_SITE_BASE_URL}{page['path']}"
    indexed = bool(page["index"])

    # Keep the rendered English title/description aligned with the server-side
    # metadata after the page language helper runs in the browser.
    if name == "public_site.html":
        html = html.replace(
            "MQB Retrofit Hungary | ANTHBOT Map & Digital Tools",
            title,
        )
        html = html.replace(
            "MQB Retrofit Hungary develops the independent open-source ANTHBOT Map "
            "Home Assistant integration, map card, diagnostics, scheduling tools and "
            "optional digital products for supported ANTHBOT robotic lawn mowers.",
            description,
        )
    elif name == "store.html":
        html = html.replace("ANTHBOT Community Voice Store", title)

    title_tag = f"<title>{escape(title)}</title>"
    if re.search(r"<title>.*?</title>", html, flags=re.IGNORECASE | re.DOTALL):
        html = re.sub(
            r"<title>.*?</title>",
            lambda _: title_tag,
            html,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
    else:
        html = html.replace("</head>", f"{title_tag}\n</head>", 1)

    description_tag = (
        f'<meta name="description" content="{escape(description, quote=True)}">'
    )
    description_pattern = re.compile(
        r'<meta\s+name="description"\s+content="[^"]*"\s*/?>',
        flags=re.IGNORECASE,
    )
    if description_pattern.search(html):
        html = description_pattern.sub(description_tag, html, count=1)
        extra_description = ""
    else:
        extra_description = description_tag + "\n"

    robots = (
        "index,follow,max-image-preview:large,max-snippet:-1,max-video-preview:-1"
        if indexed
        else "noindex,nofollow"
    )
    social = [
        extra_description.rstrip("\n"),
        f'<link rel="canonical" href="{escape(canonical, quote=True)}">',
        f'<meta name="robots" content="{robots}">',
        '<meta property="og:type" content="website">',
        '<meta property="og:site_name" content="ANTHBOT Map">',
        f'<meta property="og:title" content="{escape(title, quote=True)}">',
        f'<meta property="og:description" content="{escape(description, quote=True)}">',
        f'<meta property="og:url" content="{escape(canonical, quote=True)}">',
        '<meta name="twitter:card" content="summary">',
        f'<meta name="twitter:title" content="{escape(title, quote=True)}">',
        f'<meta name="twitter:description" content="{escape(description, quote=True)}">',
    ]
    social = [item for item in social if item]

    if name == "public_site.html":
        structured = json.dumps(
            {
                "@context": "https://schema.org",
                "@type": "SoftwareApplication",
                "name": "ANTHBOT Map",
                "applicationCategory": "HomeAutomationApplication",
                "operatingSystem": "Home Assistant",
                "url": f"{_PUBLIC_SITE_BASE_URL}/",
                "downloadUrl": "https://github.com/Mqbretrofit/ha-anthbot-map-v2",
                "sameAs": ["https://github.com/Mqbretrofit/ha-anthbot-map-v2"],
                "license": "https://opensource.org/licenses/MIT",
                "isAccessibleForFree": True,
                "description": description,
                "publisher": {
                    "@type": "Organization",
                    "name": "MQB Retrofit Hungary",
                    "url": f"{_PUBLIC_SITE_BASE_URL}/",
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        social.append(
            f'<script type="application/ld+json">{structured}</script>'
        )

    html = html.replace("</head>", "\n".join(social) + "\n</head>", 1)
    html = html.replace("</head>", _PUBLIC_TYPOGRAPHY_STYLE + "</head>", 1)
    if name == "public_site.html" and "</main>" in html:
        explore_i18n = json.dumps(
            _PUBLIC_EXPLORE_I18N,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        explore = f"""
<section class="section" id="anthbot-seo-topics"><div class="wrap">
  <h2 data-topic-i18n="title">Explore ANTHBOT Map topics</h2>
  <p class="lead" data-topic-i18n="lead">Detailed pages for Home Assistant integration, tested mower families and Community voice packs.</p>
  <div class="policy-links">
    <a class="btn" href="/home-assistant" data-topic-i18n="ha">ANTHBOT Home Assistant</a>
    <a class="btn" href="/models/genie-1000">Genie 1000</a>
    <a class="btn" href="/models/m9-pro">M9 Pro</a>
    <a class="btn" href="/models/mgc1000">MGC1000 / Pion</a>
    <a class="btn" href="/voice-packs" data-topic-i18n="voice">ANTHBOT Voice Packs</a>
  </div>
</div></section>
<script id="anthbot-seo-topics-i18n">
(function(){{
  const I18N={explore_i18n};
  const supported=Object.keys(I18N);
  function initial(){{
    let saved="";try{{saved=localStorage.getItem("mqb-site-language")||"";}}catch(_){{}}
    if(supported.includes(saved))return saved;
    const raw=navigator.language||"en";
    if(supported.includes(raw))return raw;
    const short=raw.slice(0,2).toLowerCase();
    return supported.includes(short)?short:"en";
  }}
  function apply(lang){{
    const current=supported.includes(lang)?lang:"en";
    const table=I18N[current]||I18N.en;
    document.querySelectorAll("[data-topic-i18n]").forEach(el=>{{
      const key=el.getAttribute("data-topic-i18n");
      if(table[key])el.textContent=table[key];
    }});
  }}
  const select=document.getElementById("site-language");
  if(select)select.addEventListener("change",()=>apply(select.value));
  apply(initial());
}})();
</script>
"""
        html = html.replace("</main>", explore + "</main>", 1)
    return html


def _seo_landing_html(path: str) -> str:
    page = _SEO_LANDING_PAGES.get(path)
    if page is None:
        raise HTTPException(status_code=404, detail="page not found")

    landing_i18n = json.dumps(
        _SEO_LANDING_I18N,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    language_options = "".join(
        f'<option value="{escape(code, quote=True)}">{escape(label)}</option>'
        for code, label in _PUBLIC_LANGUAGE_OPTIONS
    )

    title = str(page["title"])
    description = str(page["description"])
    heading = str(page["heading"])
    lead = str(page["lead"])
    canonical = f"{_PUBLIC_SITE_BASE_URL}{path}"
    section_items = list(page["sections"])
    sections = "".join(
        (
            '<div class="feature glass"><div class="feature-icon">✦</div><h3 data-i18n="section'
            + str(index)
            + 'Title">'
            + escape(str(section_title))
            + '</h3><p data-i18n="section'
            + str(index)
            + 'Text">'
            + escape(str(section_text))
            + '</p></div>'
        )
        for index, (section_title, section_text) in enumerate(section_items)
    )
    preview_rows = "".join(
        (
            '<div class="tile"><strong data-i18n="section'
            + str(index)
            + 'Title">'
            + escape(str(section_title))
            + '</strong><span data-i18n="section'
            + str(index)
            + 'Text">'
            + escape(str(section_text))
            + '</span></div>'
        )
        for index, (section_title, section_text) in enumerate(section_items[:3])
    )
    cta_label, cta_href = page["cta"]
    structured = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "WebPage",
            "name": title,
            "url": canonical,
            "description": description,
            "isPartOf": {
                "@type": "WebSite",
                "name": "ANTHBOT Map",
                "url": f"{_PUBLIC_SITE_BASE_URL}/",
            },
            "about": {
                "@type": "SoftwareApplication",
                "name": "ANTHBOT Map",
                "applicationCategory": "HomeAutomationApplication",
                "operatingSystem": "Home Assistant",
                "url": f"{_PUBLIC_SITE_BASE_URL}/",
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title>
<meta name="description" content="{escape(description, quote=True)}">
<link rel="canonical" href="{escape(canonical, quote=True)}">
<meta name="robots" content="index,follow,max-image-preview:large,max-snippet:-1,max-video-preview:-1">
<meta property="og:type" content="website">
<meta property="og:site_name" content="ANTHBOT Map">
<meta property="og:title" content="{escape(title, quote=True)}">
<meta property="og:description" content="{escape(description, quote=True)}">
<meta property="og:url" content="{escape(canonical, quote=True)}">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="{escape(title, quote=True)}">
<meta name="twitter:description" content="{escape(description, quote=True)}">
<script type="application/ld+json">{structured}</script>
{_PUBLIC_TYPOGRAPHY_STYLE}
<style>
:root{{
  color-scheme:dark;
  --bg:#081017;--card:#111820;--card2:#141d27;--card3:#0c1218;
  --text:#fff;--muted:rgba(255,255,255,.7);--faint:rgba(255,255,255,.48);
  --line:rgba(255,255,255,.11);--line2:rgba(255,255,255,.18);
  --green:#5ee083;--blue:#03a9f4;--red:#ff5252;--violet:#6f6bff;
  --max:1200px;
  font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif
}}
*{{box-sizing:border-box}}
html{{scroll-behavior:smooth}}
body{{margin:0;background:
  radial-gradient(circle at 14% 0,rgba(40,94,145,.34),transparent 29rem),
  radial-gradient(circle at 90% 24%,rgba(94,224,131,.10),transparent 26rem),
  var(--bg);color:var(--text);line-height:1.6;overflow-x:hidden}}
a{{color:inherit}}.wrap{{max-width:var(--max);margin:auto;padding:0 22px}}
.bg-grid{{position:fixed;inset:0;pointer-events:none;opacity:.12;background-image:
 linear-gradient(rgba(94,224,131,.14) 1px,transparent 1px),
 linear-gradient(90deg,rgba(94,224,131,.14) 1px,transparent 1px);background-size:46px 46px;
 mask-image:linear-gradient(to bottom,#000,transparent 80%)}}
.nav{{position:sticky;top:0;z-index:50;background:rgba(8,16,23,.76);backdrop-filter:blur(18px);border-bottom:1px solid var(--line)}}
.navin{{height:72px;display:flex;align-items:center;justify-content:space-between;gap:22px}}
.brand{{display:flex;gap:11px;align-items:center;text-decoration:none;font-weight:850}}
.logo{{width:36px;height:36px;border-radius:12px;display:grid;place-items:center;background:linear-gradient(145deg,#31bf62,#249c4d);box-shadow:0 0 30px rgba(94,224,131,.18)}}
.links{{display:flex;gap:16px;flex-wrap:wrap;align-items:center}}
.links a{{text-decoration:none;color:var(--muted);font-size:14px}}.links a:hover{{color:#fff}}
.lang-wrap{{display:flex;align-items:center;gap:8px;flex:0 0 auto}}.lang-label{{color:var(--faint);font-size:12px}}.lang-select{{height:38px;min-width:116px;border-radius:11px;border:1px solid var(--line2);background:var(--card2);color:#fff;padding:0 9px;font:inherit;font-size:12px}}
.hero{{padding:74px 0 46px}}
.hero-grid{{display:grid;grid-template-columns:.95fr 1.05fr;gap:34px;align-items:center}}
.eyebrow{{display:inline-flex;gap:8px;align-items:center;border:1px solid var(--line2);background:rgba(20,29,39,.72);border-radius:999px;padding:7px 12px;font-size:12px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:#dfe8ee}}
.dotlive{{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 14px rgba(94,224,131,.85);animation:blink 1.6s ease-in-out infinite}}
.hero h1{{font-size:clamp(2.7rem,6vw,4.9rem);line-height:1.02;margin:18px 0 18px}}
.grad{{background:linear-gradient(135deg,#dfffea,var(--green));-webkit-background-clip:text;background-clip:text;color:transparent}}
.hero p{{font-size:1.14rem;color:var(--muted);max-width:690px}}
.tags{{display:flex;gap:9px;flex-wrap:wrap;margin-top:21px}}.tag{{font-size:13px;padding:7px 11px;border-radius:999px;border:1px solid var(--line2);background:rgba(20,29,39,.72);color:#d6e0e7}}
.cta{{display:flex;gap:12px;flex-wrap:wrap;margin-top:26px}}
.btn{{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:0 17px;border-radius:12px;border:1px solid var(--line2);background:var(--card2);text-decoration:none;font-weight:800;transition:.18s}}
.btn:hover{{transform:translateY(-2px);border-color:rgba(255,255,255,.34)}}.btn.primary{{background:linear-gradient(180deg,#34c759,#248a46);border:0;color:#fff;box-shadow:0 16px 34px rgba(36,138,70,.25)}}
.repo-meta{{display:flex;gap:18px;flex-wrap:wrap;margin-top:22px;color:var(--faint);font-size:13px}}.repo-meta strong{{color:#fff}}
.glass{{background:linear-gradient(180deg,rgba(17,24,32,.88),rgba(12,18,24,.82));border:1px solid var(--line);box-shadow:0 22px 70px rgba(0,0,0,.26);backdrop-filter:blur(12px)}}
.topic-stage{{position:relative;min-height:420px;border-radius:26px;padding:22px;overflow:hidden;display:flex;align-items:center}}
.topic-stage:before{{content:"";position:absolute;inset:0;background:radial-gradient(circle at 50% 30%,rgba(94,224,131,.12),transparent 42%),linear-gradient(180deg,rgba(20,29,39,.7),rgba(8,16,23,.82))}}
.topic-panel{{position:relative;z-index:2;width:100%;border:1px solid rgba(255,255,255,.13);border-radius:22px;background:#111820;box-shadow:0 26px 80px rgba(0,0,0,.42);overflow:hidden}}
.topic-top{{padding:18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:16px;align-items:center}}
.topic-title strong{{display:block;font-size:20px}}.topic-title span{{display:block;color:var(--muted);font-size:12px;margin-top:2px}}
.cloud{{display:flex;align-items:center;gap:8px;color:#aeb7c2;font-size:12px;font-weight:800}}.cloud i{{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 12px rgba(94,224,131,.8)}}
.topic-body{{padding:14px;display:grid;gap:9px}}
.tile{{min-height:78px;border-radius:15px;border:1px solid var(--line);background:#141d27;padding:12px}}
.tile strong{{display:block;font-size:13px}}.tile span{{display:block;font-size:11px;color:var(--muted);margin-top:3px}}
.section{{padding:68px 0}}.section h2{{font-size:2.2rem;line-height:1.15;margin:0 0 12px}}.lead{{font-size:1.02rem;color:var(--muted);max-width:830px}}
.feature-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:16px;margin-top:25px}}
.feature{{padding:22px;border-radius:20px}}.feature-icon{{width:48px;height:48px;border-radius:15px;display:grid;place-items:center;background:#141d27;border:1px solid var(--line2);font-size:22px;margin-bottom:13px}}
.feature h3{{margin:5px 0 8px}}.feature p{{margin:0;color:var(--muted);font-size:14px}}
.open-source{{padding:23px;border-radius:20px;display:grid;grid-template-columns:1fr auto;gap:20px;align-items:center}}
.open-source p{{margin:6px 0 0;color:var(--muted)}}
.footer{{margin-top:36px;border-top:1px solid var(--line);padding:28px 0 44px;color:#94a2ad;font-size:13px}}
.foot{{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px}}.foot a{{color:#c3ced5}}
@keyframes blink{{0%,100%{{opacity:.6}}50%{{opacity:1}}}}
@media(max-width:1050px){{.hero-grid{{grid-template-columns:1fr}}.topic-stage{{min-height:360px}}.feature-grid{{grid-template-columns:1fr}}}}
@media(max-width:760px){{.links{{display:none}}.lang-label{{display:none}}.hero{{padding-top:54px}}.hero h1{{font-size:clamp(2.35rem,11vw,4rem)}}.hero p{{font-size:1.02rem}}.topic-stage{{min-height:auto;padding:12px 0 0}}.open-source{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<div class="bg-grid"></div>
<nav class="nav"><div class="wrap navin">
  <a class="brand" href="/"><span class="logo">M</span><span>MQB Retrofit Hungary</span></a>
  <div class="links">
    <a href="/#anthbot-map">ANTHBOT Map</a>
    <a href="/#features" data-i18n="features">Features</a>
    <a href="/#models" data-i18n="models">Models</a>
    <a href="/store" data-i18n="voiceStore">Voice Store</a>
    <a href="/#support" data-i18n="support">Support</a>
    <a href="/terms" data-i18n="terms">Terms</a>
    <a href="/privacy" data-i18n="privacy">Privacy</a>
  </div>
  <div class="lang-wrap"><span class="lang-label" data-i18n="language">Language</span><select class="lang-select" id="site-language" aria-label="Language">{language_options}</select></div>
</div></nav>

<header class="hero"><div class="wrap hero-grid">
  <div>
    <span class="eyebrow"><i class="dotlive"></i><span data-i18n="pageEyebrow">{escape(str(page["eyebrow"]))}</span></span>
    <h1 data-i18n="pageHeading">{escape(heading)}</h1>
    <p data-i18n="pageLead">{escape(lead)}</p>
    <div class="tags">
      <span class="tag">Home Assistant</span>
      <span class="tag">ANTHBOT Map</span>
      <span class="tag" data-i18n="tagModelAware">Model-aware</span>
      <span class="tag" data-i18n="tagIndependent">Independent community project</span>
    </div>
    <div class="cta">
      <a class="btn primary" href="{escape(str(cta_href), quote=True)}" data-i18n="pageCta">{escape(str(cta_label))}</a>
      <a class="btn" href="/" data-i18n="back">Back to ANTHBOT Map</a>
    </div>
    <div class="repo-meta"><span><span data-i18n="project">Project</span> <strong>ANTHBOT Map</strong></span><span><span data-i18n="platform">Platform</span> <strong>Home Assistant</strong></span><span><span data-i18n="license">License</span> <strong>MIT</strong></span></div>
  </div>

  <div class="topic-stage glass">
    <div class="topic-panel">
      <div class="topic-top"><div class="topic-title"><strong data-i18n="pageHeading">{escape(heading)}</strong><span data-i18n="cardSubtitle">Anthbot Map Card · Home Assistant</span></div><div class="cloud"><i></i> <span data-i18n="cloudLive">CLOUD LIVE</span></div></div>
      <div class="topic-body">{preview_rows}</div>
    </div>
  </div>
</div></header>

<main>
<section class="section"><div class="wrap">
  <span class="eyebrow">ANTHBOT Map</span>
  <h2 data-i18n="builtTitle">Built around the same project as the main site.</h2>
  <p class="lead" data-i18n="pageDescription">{escape(description)}</p>
  <div class="feature-grid">{sections}</div>
</div></section>

<section class="section"><div class="wrap">
  <div class="open-source glass">
    <div><strong data-i18n="noticeTitle">Independent project / trademark notice</strong><p data-i18n="noticeBody">ANTHBOT is a trademark of its respective owner. ANTHBOT Map and MQB Retrofit Hungary are independent and are not official ANTHBOT products unless explicitly stated otherwise.</p></div>
    <a class="btn" href="https://github.com/Mqbretrofit/ha-anthbot-map-v2" target="_blank" rel="noopener" data-i18n="github">ANTHBOT Map GitHub</a>
  </div>
</div></section>
</main>

<footer class="footer"><div class="wrap">© 2026 MQB Retrofit Hungary. <span data-i18n="rights">All rights reserved.</span><div class="foot"><a href="/" data-i18n="home">Home</a><a href="https://github.com/Mqbretrofit/ha-anthbot-map-v2" target="_blank" rel="noopener" data-i18n="github">ANTHBOT Map GitHub</a><a href="/store" data-i18n="voicePackStore">Voice Pack Store</a><a href="/refunds" data-i18n="refunds">Refunds</a><a href="/terms" data-i18n="terms">Terms</a><a href="/privacy" data-i18n="privacy">Privacy</a></div></div></footer>
<script id="seo-landing-i18n">
(function(){{
  const I18N={landing_i18n};
  const PATH={json.dumps(path)};
  const supported=Object.keys(I18N);
  function initial(){{
    let saved="";try{{saved=localStorage.getItem("mqb-site-language")||"";}}catch(_){{}}
    if(supported.includes(saved))return saved;
    const raw=navigator.language||"en";
    if(supported.includes(raw))return raw;
    const short=raw.slice(0,2).toLowerCase();
    return supported.includes(short)?short:"en";
  }}
  function value(table,key){{
    if(Object.prototype.hasOwnProperty.call(table.common||{{}},key))return table.common[key];
    const page=(table.pages||{{}})[PATH]||{{}};
    if(key==="pageTitle")return page.title;
    if(key==="pageDescription")return page.description;
    if(key==="pageEyebrow")return page.eyebrow;
    if(key==="pageHeading")return page.heading;
    if(key==="pageLead")return page.lead;
    if(key==="pageCta")return page.cta;
    const match=/^section([0-9]+)(Title|Text)$/.exec(key);
    if(match){{
      const item=(page.sections||[])[Number(match[1])];
      if(item)return item[match[2]==="Title"?0:1];
    }}
    return null;
  }}
  function apply(lang){{
    const current=supported.includes(lang)?lang:"en";
    const table=I18N[current]||I18N.en;
    document.documentElement.lang=current;
    const select=document.getElementById("site-language");
    if(select)select.value=current;
    document.querySelectorAll("[data-i18n]").forEach(el=>{{
      const translated=value(table,el.getAttribute("data-i18n"));
      if(translated)el.textContent=translated;
    }});
    const page=(table.pages||{{}})[PATH]||{{}};
    if(page.title)document.title=page.title;
    const meta=document.querySelector('meta[name="description"]');
    if(meta&&page.description)meta.setAttribute("content",page.description);
    try{{localStorage.setItem("mqb-site-language",current);}}catch(_){{}}
  }}
  const select=document.getElementById("site-language");
  if(select)select.addEventListener("change",()=>apply(select.value));
  apply(initial());
}})();
</script>
<script src="/site-analytics.js?v=1"></script>
</body>
</html>"""

def _html_file(name: str) -> str:
    try:
        html = Path(__file__).with_name(name).read_text(encoding="utf-8")
    except OSError as err:
        raise HTTPException(status_code=503, detail="voice store asset unavailable") from err
    if name in _ANALYTICS_PUBLIC_HTML and "</body>" in html:
        html = html.replace(
            "</body>",
            '<script src="/site-analytics.js?v=1"></script></body>',
            1,
        )
    return _apply_seo_metadata(name, html)


@router.get("/site-analytics.js")
def site_analytics_script() -> Response:
    script = r"""
(() => {
  if (
    navigator.globalPrivacyControl === true ||
    navigator.doNotTrack === "1" ||
    window.doNotTrack === "1"
  ) return;
  const allowed = new Set(["/","/store","/store/success","/privacy","/terms","/refunds","/home-assistant","/models/genie-1000","/models/m9-pro","/models/mgc1000","/voice-packs"]);
  const path = location.pathname.replace(/\/+$/, "") || "/";
  if (!allowed.has(path)) return;
  const language = String(document.documentElement.lang || navigator.language || "en")
    .slice(0, 16);
  const payload = JSON.stringify({path, language});
  setTimeout(() => {
    fetch("/api/anthbot/site/visit", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: payload,
      credentials: "omit",
      cache: "no-store",
      keepalive: true
    }).catch(() => {});
  }, 0);
})();
"""
    return Response(
        content=script,
        media_type="application/javascript",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.post("/api/anthbot/site/visit", status_code=204)
def record_site_visit(
    payload: SiteVisitPayload,
    request: Request,
) -> Response:
    _record_site_visit(
        request,
        path=payload.path,
        language=payload.language,
    )
    return Response(status_code=204)


@router.get(
    "/api/anthbot/admin/site-analytics",
    dependencies=[Depends(core.require_admin)],
)
def admin_site_analytics(days: int = 30) -> dict[str, Any]:
    _init_store_tables()
    days = max(1, min(int(days), _ANALYTICS_UNIQUE_RETENTION_DAYS))
    today = datetime.now(timezone.utc).date()
    start_day = (today - timedelta(days=days - 1)).isoformat()
    today_key = today.isoformat()
    seven_day = (today - timedelta(days=6)).isoformat()
    thirty_day = (today - timedelta(days=29)).isoformat()

    with core._db() as conn:
        today_views = int(
            conn.execute(
                "SELECT COALESCE(SUM(views), 0) FROM site_analytics_views WHERE day = ?",
                (today_key,),
            ).fetchone()[0]
        )
        today_unique = int(
            conn.execute(
                "SELECT COUNT(*) FROM site_analytics_unique_visitors WHERE day = ?",
                (today_key,),
            ).fetchone()[0]
        )
        views_7d = int(
            conn.execute(
                "SELECT COALESCE(SUM(views), 0) FROM site_analytics_views WHERE day >= ?",
                (seven_day,),
            ).fetchone()[0]
        )
        views_30d = int(
            conn.execute(
                "SELECT COALESCE(SUM(views), 0) FROM site_analytics_views WHERE day >= ?",
                (thirty_day,),
            ).fetchone()[0]
        )
        daily_rows = conn.execute(
            """
            SELECT v.day,
                   SUM(v.views) AS views,
                   (
                     SELECT COUNT(*)
                     FROM site_analytics_unique_visitors u
                     WHERE u.day = v.day
                   ) AS unique_visitors
            FROM site_analytics_views v
            WHERE v.day >= ?
            GROUP BY v.day
            ORDER BY v.day DESC
            """,
            (start_day,),
        ).fetchall()
        page_rows = conn.execute(
            """
            SELECT v.path,
                   SUM(v.views) AS views,
                   (
                     SELECT COUNT(*)
                     FROM site_analytics_unique_page_visitors u
                     WHERE u.path = v.path AND u.day >= ?
                   ) AS unique_visitor_days
            FROM site_analytics_views v
            WHERE v.day >= ?
            GROUP BY v.path
            ORDER BY views DESC, v.path
            """,
            (start_day, start_day),
        ).fetchall()
        language_rows = conn.execute(
            """
            SELECT v.language,
                   SUM(v.views) AS views,
                   (
                     SELECT COUNT(*)
                     FROM site_analytics_unique_language_visitors u
                     WHERE u.language = v.language AND u.day >= ?
                   ) AS unique_visitor_days
            FROM site_analytics_views v
            WHERE v.day >= ?
            GROUP BY v.language
            ORDER BY views DESC, v.language
            """,
            (start_day, start_day),
        ).fetchall()

    return {
        "enabled": _site_analytics_enabled(),
        "window_days": days,
        "today": {
            "views": today_views,
            "unique_visitors": today_unique,
        },
        "views_7d": views_7d,
        "views_30d": views_30d,
        "daily": [dict(row) for row in daily_rows],
        "pages": [dict(row) for row in page_rows],
        "languages": [dict(row) for row in language_rows],
        "privacy": {
            "raw_ip_persisted": False,
            "user_agent_persisted": False,
            "analytics_cookie": False,
            "unique_identifier_rotation": "daily_utc",
            "unique_hash_retention_days": _ANALYTICS_UNIQUE_RETENTION_DAYS,
            "aggregate_retention_days": _ANALYTICS_AGGREGATE_RETENTION_DAYS,
        },
    }


@router.get("/api/anthbot/store/voice-packs")
def store_voice_packs(request: Request) -> dict[str, Any]:
    catalog = _store_catalog(request)
    client_id = _browser_store_client_id(request)
    if client_id is None:
        return catalog

    packs: list[dict[str, Any]] = []
    for item in catalog.get("packs", []):
        if not isinstance(item, dict):
            continue
        public = dict(item)
        if str(public.get("access") or "free").casefold() == "paid":
            try:
                record = _find_uploaded_pack(str(public.get("id") or ""))
                order = _paid_order_for_client_pack(
                    client_id,
                    record,
                    entitlement_scope="web",
                )
            except HTTPException:
                order = None
            public["owned"] = order is not None
            if order is not None:
                public["ownership"] = "web"
        packs.append(public)
    catalog["packs"] = packs
    return catalog


@router.post("/api/anthbot/store/client/checkout")
async def create_store_client_checkout(
    payload: StoreClientCheckoutPayload,
    request: Request,
) -> dict[str, Any]:
    """Create a checkout directly for trusted first-party clients such as the
    standalone Voice Installer, without asking the user to copy a pairing code.
    """
    _require_checkout_ready()
    record = _find_uploaded_pack(payload.pack_id)
    if _is_store_hidden(record):
        raise HTTPException(status_code=404, detail="voice pack is hidden from store")
    if not _is_paid(record):
        raise HTTPException(status_code=409, detail="voice pack is not a paid product")

    client_id = _client_id_from_token(payload.client_token)
    existing_order = _paid_order_for_client_pack(
        client_id,
        record,
        entitlement_scope="web",
    )
    if existing_order is not None:
        return {
            "already_owned": True,
            "pack_id": payload.pack_id,
            "checkout_url": None,
            "session_id": existing_order["stripe_session_id"],
        }

    session = await asyncio.to_thread(
        _create_checkout_session,
        record,
        request,
        client_id=client_id,
        pair_code=None,
        entitlement_scope="web",
    )
    order = _upsert_order_from_session(session)
    checkout_url = session.get("url")
    if not isinstance(checkout_url, str) or not checkout_url.startswith("https://"):
        raise HTTPException(status_code=502, detail="Stripe did not return a checkout URL")
    return {
        "already_owned": False,
        "pack_id": payload.pack_id,
        "checkout_url": checkout_url,
        "session_id": order["stripe_session_id"],
    }


@router.post("/api/anthbot/store/client/pair")
def create_store_client_pair(
    payload: StoreClientPayload,
    request: Request,
) -> dict[str, Any]:
    if not _store_enabled():
        raise HTTPException(status_code=503, detail="voice store is disabled")
    pair_code, expires_at = _create_store_pairing(payload.client_token)
    base = core._public_base_url(request)
    return {
        "paired": True,
        "store_url": f"{base}/store?pair={quote(pair_code)}",
        "expires_at": _iso_from_epoch(expires_at),
    }


@router.post("/api/anthbot/store/client/entitlements")
def store_client_entitlements(
    payload: StoreClientPayload,
    request: Request,
) -> dict[str, Any]:
    client_id = _client_id_from_token(payload.client_token)
    _init_store_tables()
    with core._db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM store_orders
            WHERE client_id = ?
              AND payment_status = 'paid'
              AND (
                    entitlement_scope = 'map'
                    OR entitlement_scope IS NULL
                  )
            ORDER BY COALESCE(paid_at, updated_at) DESC
            """,
            (client_id,),
        ).fetchall()

    packs: list[dict[str, Any]] = []
    seen_voice_ids: set[str] = set()
    base = core._public_base_url(request)
    for row in rows:
        order = _reconcile_paid_order_with_stripe(dict(row))
        if str(order.get("payment_status", "")).casefold() != "paid":
            continue
        try:
            pack = _resolve_order_pack(order)
        except HTTPException:
            continue
        if not _is_paid(pack):
            continue

        current_pack_id = str(pack.get("id", "")).strip()
        stable_voice_id = (
            str(pack.get("community_id", "")).strip()
            or str(order.get("community_id", "")).strip()
            or current_pack_id
        )
        if not current_pack_id or stable_voice_id in seen_voice_ids:
            continue

        license_key = _license_for_order(order)
        public = _public_paid_pack(pack, request)
        public["music_url"] = (
            f"{base}/api/anthbot/store/voice-packs/{quote(current_pack_id)}/download"
            f"?license={quote(license_key)}"
        )
        public["entitlement"] = "purchased"
        packs.append(public)
        seen_voice_ids.add(stable_voice_id)

    return {
        "licensed": bool(packs),
        "license_version": 1,
        "packs": packs,
    }


@router.post("/api/anthbot/store/custom-voice-requests", status_code=201)
def create_custom_voice_request(
    payload: CustomVoiceRequestPayload,
) -> dict[str, Any]:
    _init_store_tables()
    request_id = f"cvr_{secrets.token_urlsafe(12)}"
    now = core._iso()
    with core._db() as conn:
        conn.execute(
            """
            INSERT INTO store_custom_voice_requests (
                request_id, requested_language, voice_style, model, contact,
                notes, site_language, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (
                request_id,
                payload.requested_language,
                payload.voice_style,
                payload.model,
                payload.contact,
                payload.notes,
                payload.site_language,
                now,
                now,
            ),
        )
    return {
        "submitted": True,
        "request_id": request_id,
        "starting_price_amount": _CUSTOM_VOICE_STARTING_PRICE_AMOUNT,
        "currency": _STANDARD_VOICE_PACK_CURRENCY,
    }


@router.post("/api/anthbot/privacy-requests", status_code=201)
def create_privacy_request(
    payload: PrivacyRequestPayload,
) -> dict[str, Any]:
    """Receive an electronic GDPR/data-rights request without requiring email."""
    _init_store_tables()
    request_id = f"prv_{secrets.token_urlsafe(12)}"
    now = core._iso()
    with core._db() as conn:
        conn.execute(
            """
            INSERT INTO privacy_requests (
                request_id, request_type, contact, details, site_language,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (
                request_id,
                payload.request_type,
                payload.contact,
                payload.details,
                payload.site_language,
                now,
                now,
            ),
        )
    return {
        "submitted": True,
        "request_id": request_id,
    }


@router.post("/api/anthbot/store/checkout")
async def create_store_checkout(
    payload: CheckoutPayload,
    request: Request,
) -> dict[str, Any]:
    _require_checkout_ready()
    record = _find_uploaded_pack(payload.pack_id)
    if _is_store_hidden(record):
        raise HTTPException(status_code=404, detail="voice pack is hidden from store")
    if not _is_paid(record):
        raise HTTPException(status_code=409, detail="voice pack is not a paid product")

    pair_client_id = _client_id_from_pairing(payload.pair_code)
    entitlement_scope = "map" if pair_client_id is not None else "web"
    client_id = pair_client_id or _browser_store_client_id(request)
    if client_id is None:
        raise HTTPException(
            status_code=409,
            detail="Voice Store browser identity is missing. Reload the store page.",
        )

    existing_order = _paid_order_for_client_pack(
        client_id,
        record,
        entitlement_scope=entitlement_scope,
    )
    if existing_order is not None:
        return {
            "already_owned": True,
            "pack_id": payload.pack_id,
            "checkout_url": None,
            "session_id": existing_order["stripe_session_id"],
            "entitlement_scope": entitlement_scope,
        }

    session = await asyncio.to_thread(
        _create_checkout_session,
        record,
        request,
        client_id=client_id,
        pair_code=payload.pair_code,
        entitlement_scope=entitlement_scope,
    )
    order = _upsert_order_from_session(session)
    checkout_url = session.get("url")
    if not isinstance(checkout_url, str) or not checkout_url.startswith("https://"):
        raise HTTPException(status_code=502, detail="Stripe did not return a checkout URL")
    return {
        "already_owned": False,
        "checkout_url": checkout_url,
        "session_id": order["stripe_session_id"],
        "entitlement_scope": entitlement_scope,
    }


@router.post("/api/anthbot/store/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
) -> dict[str, Any]:
    body = await request.body()
    _verify_stripe_signature(body, stripe_signature)
    try:
        event = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as err:
        raise HTTPException(status_code=400, detail="invalid Stripe webhook body") from err

    event_type = str(event.get("type", ""))
    data = event.get("data")
    session = data.get("object") if isinstance(data, dict) else None
    if (
        event_type
        in {
            "checkout.session.completed",
            "checkout.session.async_payment_succeeded",
            "checkout.session.async_payment_failed",
            "checkout.session.expired",
        }
        and isinstance(session, dict)
        and session.get("object") == "checkout.session"
    ):
        _upsert_order_from_session(session)
    elif event_type == "charge.refunded" and isinstance(session, dict):
        payment_intent = session.get("payment_intent")
        if payment_intent:
            _init_store_tables()
            with core._db() as conn:
                conn.execute(
                    """
                    UPDATE store_orders
                    SET status = 'refunded',
                        payment_status = 'refunded',
                        updated_at = ?
                    WHERE stripe_payment_intent_id = ?
                    """,
                    (core._iso(), str(payment_intent)),
                )

    return {"received": True}


@router.get("/api/anthbot/store/orders/{session_id}")
async def store_order(session_id: str, request: Request) -> dict[str, Any]:
    if not _SESSION_RE.fullmatch(session_id):
        raise HTTPException(status_code=422, detail="invalid checkout session id")

    order = _order_by_session(session_id)
    payment_state = (
        str(order.get("payment_status", "")).casefold()
        if order is not None
        else ""
    )
    if order is None or payment_state not in {"paid", "refunded"}:
        _require_checkout_ready()
        session = await asyncio.to_thread(_retrieve_checkout_session, session_id)
        order = _upsert_order_from_session(session)

    return _order_public(order, request)


@router.post("/api/anthbot/store/entitlements")
def store_entitlements(
    payload: EntitlementPayload,
    request: Request,
) -> dict[str, Any]:
    order = _order_from_license(payload.license_key)
    pack = _resolve_order_pack(order)
    if not _is_paid(pack):
        raise HTTPException(status_code=409, detail="licensed voice pack is no longer paid")

    pack_id = str(pack.get("id", ""))
    base = core._public_base_url(request)
    public = _public_paid_pack(pack, request)
    public["music_url"] = (
        f"{base}/api/anthbot/store/voice-packs/{quote(pack_id)}/download"
        f"?license={quote(payload.license_key)}"
    )
    return {
        "licensed": True,
        "license_version": 1,
        "packs": [public],
    }


@router.get("/api/anthbot/store/voice-packs/{pack_id}/download")
def download_paid_voice_pack(
    pack_id: str,
    license: str,
) -> FileResponse:
    order = _order_from_license(license)
    pack = _find_uploaded_pack(pack_id)
    if not _order_covers_pack(order, pack):
        raise HTTPException(status_code=403, detail="license does not cover this voice pack")

    if not _is_paid(pack):
        raise HTTPException(status_code=404, detail="paid voice pack not found")
    filename = str(pack.get("filename", ""))
    if Path(filename).name != filename or not core._VOICE_PACK_SAFE_PART.fullmatch(filename):
        raise HTTPException(status_code=404, detail="paid voice pack not found")
    path = core._voice_pack_dir() / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="paid voice pack not found")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get(
    "/api/anthbot/admin/store/voice-packs/{pack_id}/download",
    dependencies=[Depends(core.require_admin)],
)
def admin_download_voice_pack(pack_id: str) -> FileResponse:
    """Allow the authenticated project owner to download any uploaded voice pack.

    This bypasses customer purchase/licence checks only for the existing admin
    session/token. It does not create an order, entitlement, sale or Stripe event.
    """
    pack = _find_uploaded_pack(pack_id)
    filename = str(pack.get("filename", "")).strip()
    if (
        not filename
        or Path(filename).name != filename
        or not core._VOICE_PACK_SAFE_PART.fullmatch(filename)
    ):
        raise HTTPException(status_code=404, detail="voice pack file not found")

    path = core._voice_pack_dir() / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="voice pack file not found")

    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
        headers={
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
        },
    )


@router.get(
    "/api/anthbot/admin/store/voice-packs",
    dependencies=[Depends(core.require_admin)],
)
def admin_store_voice_packs(request: Request) -> dict[str, Any]:
    _init_store_tables()
    with core._db() as conn:
        sales_rows = conn.execute(
            """
            SELECT COALESCE(NULLIF(community_id, ''), pack_id) AS voice_id,
                   COUNT(*) AS sales,
                   COALESCE(SUM(amount_total), 0) AS revenue
            FROM store_orders
            WHERE payment_status = 'paid'
            GROUP BY COALESCE(NULLIF(community_id, ''), pack_id)
            """
        ).fetchall()
    sales = {
        row["voice_id"]: {"sales": row["sales"], "revenue": row["revenue"]}
        for row in sales_rows
    }

    items: list[dict[str, Any]] = []
    for record in _uploaded_records():
        public = core._public_voice_pack(record, request)
        public["access"] = "paid" if _is_paid(record) else "free"
        public["price_amount"] = _price_amount(record)
        public["currency"] = _currency(record)
        public["store_hidden"] = _is_store_hidden(record)
        voice_id = (
            str(record.get("community_id", "")).strip()
            or str(record.get("id", ""))
        )
        public["sales"] = int(sales.get(voice_id, {}).get("sales", 0))
        public["revenue"] = int(sales.get(voice_id, {}).get("revenue", 0))
        items.append(public)
    items.sort(key=lambda item: str(item.get("id", "")).casefold())
    return {
        "schema": STORE_SCHEMA,
        "store_enabled": _store_enabled(),
        "checkout_ready": _checkout_ready(),
        "items": items,
    }


@router.patch(
    "/api/anthbot/admin/store/voice-packs/{pack_id}",
    dependencies=[Depends(core.require_admin)],
)
def update_store_voice_pack(
    pack_id: str,
    payload: StorePricingPayload,
    request: Request,
) -> dict[str, Any]:
    registry = core._uploaded_voice_pack_registry()
    packs = [item for item in registry.get("packs", []) if isinstance(item, dict)]
    target = next((item for item in packs if str(item.get("id", "")) == pack_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="uploaded voice pack not found")

    target["access"] = payload.access
    target["price_amount"] = _STANDARD_VOICE_PACK_PRICE_AMOUNT if payload.access == "paid" else 0
    target["currency"] = _STANDARD_VOICE_PACK_CURRENCY if payload.access == "paid" else payload.currency
    core._write_uploaded_voice_registry(
        {"schema": core.VOICE_PACKS_SCHEMA, "packs": packs}
    )

    public = core._public_voice_pack(target, request)
    public["access"] = payload.access
    public["price_amount"] = target["price_amount"]
    public["currency"] = target["currency"]
    return {"updated": True, "pack": public}


@router.patch(
    "/api/anthbot/admin/store/voice-packs/{pack_id}/visibility",
    dependencies=[Depends(core.require_admin)],
)
def update_store_voice_pack_visibility(
    pack_id: str,
    payload: StoreVisibilityPayload,
    request: Request,
) -> dict[str, Any]:
    registry = core._uploaded_voice_pack_registry()
    packs = [item for item in registry.get("packs", []) if isinstance(item, dict)]
    target = next((item for item in packs if str(item.get("id", "")) == pack_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="uploaded voice pack not found")

    if payload.hidden:
        target["store_hidden"] = True
    else:
        target.pop("store_hidden", None)

    core._write_uploaded_voice_registry(
        {"schema": core.VOICE_PACKS_SCHEMA, "packs": packs}
    )

    public = core._public_voice_pack(target, request)
    public["access"] = "paid" if _is_paid(target) else "free"
    public["price_amount"] = _price_amount(target)
    public["currency"] = _currency(target)
    public["store_hidden"] = _is_store_hidden(target)
    return {"updated": True, "pack": public}


@router.get(
    "/api/anthbot/admin/store/custom-voice-requests",
    dependencies=[Depends(core.require_admin)],
)
def admin_custom_voice_requests(limit: int = 200) -> dict[str, Any]:
    _init_store_tables()
    limit = max(1, min(int(limit), 500))
    with core._db() as conn:
        rows = conn.execute(
            """
            SELECT request_id, requested_language, voice_style, model, contact,
                   notes, site_language, status, created_at, updated_at
            FROM store_custom_voice_requests
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {"count": len(rows), "items": [dict(row) for row in rows]}


@router.get(
    "/api/anthbot/admin/store/orders",
    dependencies=[Depends(core.require_admin)],
)
def admin_store_orders(limit: int = 200) -> dict[str, Any]:
    _init_store_tables()
    limit = max(1, min(int(limit), 500))
    with core._db() as conn:
        rows = conn.execute(
            """
            SELECT stripe_session_id, pack_id, community_id, status, payment_status,
                   amount_total, currency, customer_email, client_id,
                   created_at, updated_at, paid_at
            FROM store_orders
            ORDER BY COALESCE(paid_at, updated_at) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {"count": len(rows), "items": [dict(row) for row in rows]}


@router.get(
    "/api/anthbot/admin/privacy-requests",
    dependencies=[Depends(core.require_admin)],
)
def admin_privacy_requests(limit: int = 200) -> dict[str, Any]:
    _init_store_tables()
    limit = max(1, min(int(limit), 500))
    with core._db() as conn:
        rows = conn.execute(
            """
            SELECT request_id, request_type, contact, details, site_language,
                   status, created_at, updated_at
            FROM privacy_requests
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return {"count": len(rows), "items": [dict(row) for row in rows]}


@router.get(
    "/api/anthbot/admin/store/stats",
    dependencies=[Depends(core.require_admin)],
)
def admin_store_stats() -> dict[str, Any]:
    _init_store_tables()
    with core._db() as conn:
        total_orders = conn.execute(
            "SELECT COUNT(*) FROM store_orders"
        ).fetchone()[0]
        paid_orders = conn.execute(
            "SELECT COUNT(*) FROM store_orders WHERE payment_status = 'paid'"
        ).fetchone()[0]
        revenue_rows = conn.execute(
            """
            SELECT COALESCE(currency, 'unknown') AS currency,
                   COALESCE(SUM(amount_total), 0) AS amount
            FROM store_orders
            WHERE payment_status = 'paid'
            GROUP BY COALESCE(currency, 'unknown')
            ORDER BY currency
            """
        ).fetchall()
    return {
        "orders": total_orders,
        "paid_orders": paid_orders,
        "revenue": [dict(row) for row in revenue_rows],
        "store_enabled": _store_enabled(),
        "checkout_ready": _checkout_ready(),
        "automatic_tax": _stripe_automatic_tax(),
    }


@router.get("/robots.txt")
def robots_txt() -> Response:
    return Response(
        content=(
            "User-agent: *\n"
            "Allow: /\n"
            "Disallow: /dashboard\n"
            "Disallow: /api/\n"
            "Disallow: /store/success\n"
            f"Sitemap: {_PUBLIC_SITE_BASE_URL}/sitemap.xml\n"
        ),
        media_type="text/plain; charset=utf-8",
    )


@router.get("/sitemap.xml")
def sitemap_xml() -> Response:
    urls = (
        "/",
        "/home-assistant",
        "/models/genie-1000",
        "/models/m9-pro",
        "/models/mgc1000",
        "/voice-packs",
        "/store",
        "/privacy",
        "/terms",
        "/refunds",
    )
    entries = "".join(
        f"<url><loc>{_PUBLIC_SITE_BASE_URL}{path}</loc></url>"
        for path in urls
    )
    return Response(
        content=(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{entries}</urlset>"
        ),
        media_type="application/xml",
    )


@router.get("/home-assistant", response_class=HTMLResponse)
def home_assistant_landing_page(request: Request) -> Response:
    path = "/home-assistant"
    redirect = _canonical_public_redirect(request, path)
    if redirect is not None:
        return redirect
    return HTMLResponse(_seo_landing_html(path))


@router.get("/models/{model_slug}", response_class=HTMLResponse)
def model_landing_page(model_slug: str, request: Request) -> Response:
    path = f"/models/{model_slug}"
    if path not in _SEO_LANDING_PAGES:
        raise HTTPException(status_code=404, detail="model page not found")
    redirect = _canonical_public_redirect(request, path)
    if redirect is not None:
        return redirect
    return HTMLResponse(_seo_landing_html(path))


@router.get("/voice-packs", response_class=HTMLResponse)
def voice_packs_landing_page(request: Request) -> Response:
    path = "/voice-packs"
    redirect = _canonical_public_redirect(request, path)
    if redirect is not None:
        return redirect
    return HTMLResponse(_seo_landing_html(path))


@router.get("/", response_class=HTMLResponse)
def public_business_page(request: Request) -> Response:
    redirect = _canonical_public_redirect(request, "/")
    if redirect is not None:
        return redirect
    return HTMLResponse(_html_file("public_site.html"))


@router.get("/terms", response_class=HTMLResponse)
def public_terms_page(request: Request) -> Response:
    redirect = _canonical_public_redirect(request, "/terms")
    if redirect is not None:
        return redirect
    return HTMLResponse(_html_file("public_terms.html"))


@router.get("/refunds", response_class=HTMLResponse)
def public_refunds_page(request: Request) -> Response:
    redirect = _canonical_public_redirect(request, "/refunds")
    if redirect is not None:
        return redirect
    return HTMLResponse(_html_file("public_refunds.html"))


@router.get("/privacy", response_class=HTMLResponse)
def public_privacy_page(request: Request) -> Response:
    redirect = _canonical_public_redirect(request, "/privacy")
    if redirect is not None:
        return redirect
    html = _html_file("public_privacy.html")
    address = _privacy_controller_address()
    email = _privacy_contact_email()
    replacements = {
        "__CONTROLLER_NAME__": escape(_privacy_controller_name()),
        "__CONTROLLER_ADDRESS__": escape(address or "—"),
        "__PRIVACY_EMAIL__": escape(email),
        "__PRIVACY_PHONE__": escape(_privacy_contact_phone()),
        "__PRIVACY_EMAIL_CARD_CLASS__": "info" if email else "info hidden",
        "__PRIVACY_CONFIG_WARNING_CLASS__": (
            "controller-warning" if not address else "hidden"
        ),
    }
    for needle, value in replacements.items():
        html = html.replace(needle, value)
    return HTMLResponse(html)


@router.get("/store", response_class=HTMLResponse)
def store_page(request: Request) -> Response:
    redirect = _canonical_public_redirect(request, "/store")
    if redirect is not None:
        return redirect
    response = HTMLResponse(_html_file("store.html"))
    if _browser_store_token(request) is None:
        _set_browser_store_cookie(response, secrets.token_urlsafe(36))
    return response


@router.get("/store/success", response_class=HTMLResponse)
def store_success_page(request: Request) -> Response:
    redirect = _canonical_public_redirect(request, "/store/success")
    if redirect is not None:
        return redirect
    return HTMLResponse(
        _html_file("store_success.html"),
        headers={"X-Robots-Tag": "noindex, nofollow"},
    )


@router.get("/dashboard/store", response_class=HTMLResponse)
def store_admin_page(request: Request):
    if not core._request_is_admin(request):
        return RedirectResponse(url="/dashboard", status_code=303)
    return HTMLResponse(_html_file("store_admin.html"))
