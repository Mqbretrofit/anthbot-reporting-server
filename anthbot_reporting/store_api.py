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
import smtplib
import ssl
import time
import tarfile
from typing import Any, Literal
from urllib.parse import quote

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import app as core
import store_accounts


router = APIRouter()

STORE_SCHEMA = "anthbot-community-voice-store-v1"
_CURRENCY_RE = re.compile(r"^[a-zA-Z]{3}$")
_SESSION_RE = re.compile(r"^cs_[A-Za-z0-9_]+$")
_LICENSE_RE = re.compile(r"^abv1\.([A-Za-z0-9_-]+)\.([0-9a-f]{64})$")
_OWNER_ACCESS_RE = re.compile(r"^abo1\.([A-Za-z0-9_-]+)\.([0-9a-f]{64})$")
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
_VOICE_PREVIEW_FILES = ("A004.mp3", "A005.mp3")
_VOICE_PREVIEW_MAX_BYTES = 2 * 1024 * 1024
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

def _google_site_verification_meta() -> str:
    token = os.environ.get("ANTHBOT_GOOGLE_SITE_VERIFICATION", "").strip()
    if not token:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._=-]{8,512}", token):
        return ""
    return (
        '<meta name="google-site-verification" content="'
        + escape(token, quote=True)
        + '">'
    )
_LEGACY_PUBLIC_HOSTS = {"reports.mqbretrofithungary.online"}
_BRAND_LOGO_URL = "/brand/anthbot-map-logo.webp?v=2"
_SOCIAL_IMAGE_URL = f"{_PUBLIC_SITE_BASE_URL}{_BRAND_LOGO_URL}"
_BRAND_LOGO_IMG = (
    f'<img class="anthbot-brand-logo" src="{_BRAND_LOGO_URL}" '
    'alt="ANTHBOT Map" width="480" height="160">'
)
_FAVICON_HEAD = (
    '<link rel="icon" href="/favicon.png?v=3" type="image/png">\n'
    '<link rel="shortcut icon" href="/favicon.ico?v=2">\n'
    '<link rel="apple-touch-icon" href="/favicon.png?v=3">\n'
    '<link rel="manifest" href="/site.webmanifest?v=1">\n'
    '<meta name="theme-color" content="#081017">\n'
    '<meta name="application-name" content="ANTHBOT Map">\n'
    '<meta name="apple-mobile-web-app-title" content="ANTHBOT Map">\n'
    '<meta name="mobile-web-app-capable" content="yes">'
)
_BRAND_STYLE = """
<style id="anthbot-brand-style">
.anthbot-brand{display:inline-flex;align-items:center;text-decoration:none;flex:0 0 auto}
.anthbot-brand-logo{display:block;width:190px;max-width:42vw;height:auto;object-fit:contain}
.navlinks .anthbot-brand{border:0!important;background:transparent!important;padding:0 4px!important;min-height:38px}
.top .anthbot-brand-logo{width:176px;max-width:52vw}
@media(max-width:760px){
  .anthbot-brand-logo{width:165px;max-width:52vw}
  .navlinks .anthbot-brand{grid-column:1/-1;justify-content:flex-start}
}
</style>
"""
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

_PUBLIC_A11Y_STYLE = """
<style id="anthbot-public-a11y">
:focus-visible{outline:3px solid #7fe1bf!important;outline-offset:3px!important}
.skip-link{position:fixed;left:12px;top:12px;z-index:10000;transform:translateY(-180%);padding:10px 14px;border-radius:10px;background:#fff;color:#081017;font-weight:800;text-decoration:none;box-shadow:0 10px 28px rgba(0,0,0,.35)}
.skip-link:focus{transform:none}
img{max-width:100%;height:auto}
button,input,select,textarea{font:inherit}
.btn,button,.lang-select,.filter-control,.account-input{min-height:44px}
main,.wrap,.shell,.card,.feature,.tile,.purchase-item{min-width:0}
p,li,.desc,.custom-text,.account-sub,.purchase-name,.purchase-meta,.feature p{overflow-wrap:anywhere}
@media(max-width:760px){
  .wrap,.shell{padding-left:14px!important;padding-right:14px!important}
  .btn,button,.lang-select,.filter-control,.account-input{min-height:44px}
  .account-head,.purchase-item-head,.organizer-head{min-width:0}
  .purchase-license{min-width:0}
  .purchase-license input{min-width:0}
}
@media(prefers-reduced-motion:reduce){
  html{scroll-behavior:auto!important}
  *,*::before,*::after{animation-duration:.001ms!important;animation-iteration-count:1!important;transition-duration:.001ms!important;scroll-behavior:auto!important}
}
</style>
"""

def _apply_public_accessibility(html: str) -> str:
    if "</head>" in html and 'id="anthbot-public-a11y"' not in html:
        html = html.replace("</head>", _PUBLIC_A11Y_STYLE + "</head>", 1)
    if 'class="skip-link"' not in html:
        html = re.sub(
            r"(<body\b[^>]*>)",
            r'\1<a class="skip-link" href="#main-content">Skip to content</a>',
            html,
            count=1,
            flags=re.IGNORECASE,
        )
    if 'id="main-content"' not in html:
        html = re.sub(
            r"<main(?P<rest>\s|>)",
            r'<main id="main-content"\g<rest>',
            html,
            count=1,
            flags=re.IGNORECASE,
        )
    return html


_SEO_LANDING_I18N = json.loads(r'''{"en":{"common":{"features":"Features","models":"Models","voiceStore":"Voice Store","support":"Support","terms":"Terms","privacy":"Privacy","language":"Language","tagModelAware":"Model-aware","tagIndependent":"Independent community project","back":"Back to ANTHBOT Map","project":"Project","platform":"Platform","license":"License","cardSubtitle":"Anthbot Map Card · Home Assistant","cloudLive":"CLOUD LIVE","builtTitle":"Built around the same project as the main site.","noticeTitle":"Independent project / trademark notice","noticeBody":"ANTHBOT is a trademark of its respective owner. ANTHBOT Map is an independent community project and is not an official ANTHBOT product unless explicitly stated otherwise.","github":"ANTHBOT Map GitHub","home":"Home","voicePackStore":"Voice Pack Store","refunds":"Refunds","rights":"All rights reserved."},"pages":{"/home-assistant":{"title":"ANTHBOT Home Assistant Integration | ANTHBOT Map","description":"Connect supported ANTHBOT robotic lawn mowers to Home Assistant with ANTHBOT Map: live map, zones, native schedules, mower controls, history and diagnostics.","eyebrow":"Home Assistant integration","heading":"ANTHBOT in Home Assistant with ANTHBOT Map","lead":"ANTHBOT Map is an independent, open-source Home Assistant integration and Lovelace map card for supported ANTHBOT robotic lawn mowers.","cta":"View ANTHBOT Map on GitHub","sections":[["What the integration adds","ANTHBOT Map connects Home Assistant to the ANTHBOT cloud, creates a native lawn_mower entity, mirrors supported ANTHBOT app schedules, and provides model-aware controls instead of forcing every mower through one generic command path."],["Live map and lawn data","Where supported by the mower family, the card can render the lawn boundary, mowing zones, No-Go areas, mower position, live path and mowing coverage. A dedicated WebSocket live-map transport keeps high-frequency geometry out of Home Assistant Recorder."],["Schedules and automations","Native app schedules can be mirrored into Home Assistant and, on supported models, edited with write-back. Per-mower next-mow data, native mower events and timed mow/park overrides can be used in Home Assistant automations."],["Model-aware design","Genie, M-series, N8 and Pion/MGC devices use separated model routing. Capabilities remain conservative when a command or protocol detail has not been confirmed."]]},"/models/genie-1000":{"title":"ANTHBOT Genie 1000 Home Assistant Support | ANTHBOT Map","description":"ANTHBOT Genie 1000 support in Home Assistant with ANTHBOT Map, including live map/path data, zones, schedules, history, mower controls and diagnostics.","eyebrow":"Supported mower","heading":"ANTHBOT Genie 1000 + Home Assistant","lead":"The Genie family is supported by ANTHBOT Map and has been directly hardware-tested by the project, including Genie 1000 schedule loading.","cta":"Install / documentation","sections":[["Direct hardware validation","The public ANTHBOT Map project documents direct real-device testing for the Genie family. Genie 1000 native app schedule loading has also been verified on real hardware."],["Maps, zones and mowing history","ANTHBOT Map keeps Genie-specific map/path diagnostics isolated from other mower families and exposes supported lawn boundary, zones, No-Go geometry, live mower position, path and historical mowing data."],["Native scheduling","The integration mirrors the mower's native ANTHBOT app schedule into Home Assistant and supports the model-specific schedule path rather than translating it through M-series behavior."],["Home Assistant controls","Supported operations include mower status and common mowing controls, with model-specific routing plus Battery Saver and diagnostic tooling where the underlying device capabilities are available."]]},"/models/m9-pro":{"title":"ANTHBOT M9 Pro Home Assistant Integration | ANTHBOT Map","description":"ANTHBOT M9 Pro support for Home Assistant with control, status, live map, path, zones, mowing history, schedules and diagnostics.","eyebrow":"Directly hardware-tested","heading":"ANTHBOT M9 Pro + Home Assistant","lead":"ANTHBOT Map includes a dedicated M-series implementation, and M9 Pro control, status, map, path, zone and history handling have been directly hardware-tested by the project.","cta":"View M9 Pro integration documentation","sections":[["Live map architecture","Real-device M9 Pro validation confirmed live WebSocket path updates, Home Assistant restart and reconnect handling, snapshot restore and reduced Recorder churn."],["Zones and mowing data","The M-series path supports map, path, zone and history handling while keeping model-specific decoding separate from Genie and N8."],["Native schedule write-back","Creating an M9 Pro schedule from the ANTHBOT Map card has been verified on real hardware, with the created rule appearing in the ANTHBOT app."],["Home Assistant automation","Mower state, next-mow information, lifecycle/schedule events and supported controls can be used in dashboards and automations."]]},"/models/mgc1000":{"title":"ANTHBOT MGC1000 / Pion Home Assistant Support | ANTHBOT Map","description":"ANTHBOT MGC1000 and Pion-family support in ANTHBOT Map for Home Assistant: isolated model detection, status normalization, native schedules and start routing.","eyebrow":"Pion / MGC family","heading":"ANTHBOT MGC1000 + Home Assistant","lead":"ANTHBOT Map has a dedicated Pion/MGC model family for identifiers such as MGC500, MGC750 and MGC1000, instead of treating these mowers as Genie.","cta":"Follow Pion / MGC development","sections":[["Dedicated model handling","The integration includes isolated Pion/MGC detection and a flat-shadow normalization layer for Home Assistant status data."],["Confirmed status data","The current implementation exposes confirmed cutting height, mowing progress and area, rain state, Wi-Fi/IP, path payload and firmware data when supplied by the mower/cloud."],["Native schedules and start routing","Pion/MGC uses its own native schedule shape and start path. The integration preserves its one-appointment-per-day/full-lawn schedule behavior instead of applying Genie-only payloads."],["Conservative capability policy","Unverified Pion/MGC setting writes and curpath decoding remain intentionally disabled until protocol and hardware behavior are confirmed."]]},"/voice-packs":{"title":"ANTHBOT Voice Packs for Genie Mowers | ANTHBOT Map","description":"ANTHBOT community voice packs and custom mower voices for compatible ANTHBOT Genie robots, integrated with the ANTHBOT Map ecosystem.","eyebrow":"Community voice packs","heading":"ANTHBOT voice packs and custom mower voices","lead":"The ANTHBOT Map ecosystem includes optional Community voice packs for compatible ANTHBOT Genie robots, with ready-made packs and custom voice requests.","cta":"Open the Voice Pack Store","sections":[["Ready-made Community packs","Available voice packs are listed in the ANTHBOT Community Voice Store. Compatibility is shown with the pack and can vary by mower model or firmware."],["Custom voice requests","A separate custom-voice workflow is available for requests that are not covered by the ready-made catalogue."],["ANTHBOT Map integration","Purchased voice entitlements can be linked to ANTHBOT Map so compatible installed systems can recognize the purchased pack without exposing paid download URLs publicly."],["Independent project","Community voice packs and ANTHBOT Map are independent project features. ANTHBOT is a trademark of its respective owner; this site does not imply official ANTHBOT endorsement."]]}}},"hu":{"common":{"features":"Funkciók","models":"Modellek","voiceStore":"Hangbolt","support":"Támogatás","terms":"Feltételek","privacy":"Adatvédelem","language":"Nyelv","tagModelAware":"Modellfüggő","tagIndependent":"Független közösségi projekt","back":"Vissza az ANTHBOT Maphez","project":"Projekt","platform":"Platform","license":"Licenc","cardSubtitle":"Anthbot Map kártya · Home Assistant","cloudLive":"ÉLŐ FELHŐ","builtTitle":"Ugyanarra a projektre épül, mint a főoldal.","noticeTitle":"Független projekt / védjegy","noticeBody":"Az ANTHBOT a mindenkori jogosult védjegye. Az ANTHBOT Map független közösségi projekt; nem hivatalos ANTHBOT-termék, kivéve ha ezt külön jelezzük.","github":"ANTHBOT Map GitHub","home":"Főoldal","voicePackStore":"Hangcsomagbolt","refunds":"Visszatérítések","rights":"Minden jog fenntartva."},"pages":{"/home-assistant":{"title":"ANTHBOT Home Assistant integráció | ANTHBOT Map","description":"Támogatott ANTHBOT robotfűnyírók csatlakoztatása a Home Assistanthoz ANTHBOT Mappel: élő térkép, zónák, natív ütemezések, vezérlés, előzmények és diagnosztika.","eyebrow":"Home Assistant integráció","heading":"ANTHBOT a Home Assistantban az ANTHBOT Mappel","lead":"Az ANTHBOT Map egy független, nyílt forráskódú Home Assistant integráció és Lovelace térképkártya a támogatott ANTHBOT robotfűnyírókhoz.","cta":"ANTHBOT Map megnyitása GitHubon","sections":[["Mit ad az integráció?","Az ANTHBOT Map összekapcsolja a Home Assistantot az ANTHBOT felhővel, natív lawn_mower entitást hoz létre, tükrözi a támogatott ANTHBOT appos ütemezéseket, és modellenként kezeli a vezérlést."],["Élő térkép és gyepadatok","A támogatott modelleknél a kártya megjeleníti a gyephatárt, nyírási zónákat, tiltott területeket, a robot helyzetét, élő útvonalát és a nyírás lefedettségét. A külön WebSocket-útvonal a nagyfrekvenciás geometriát távol tartja a Home Assistant Recordertől."],["Ütemezések és automatizálások","A natív appos ütemezések tükrözhetők a Home Assistantba, támogatott modelleken pedig vissza is írhatók. A következő nyírás adatai, a robotesemények és az időzített nyírás/parkolás felülírások automatizálásokban is használhatók."],["Modellfüggő kialakítás","A Genie, M-széria, N8 és Pion/MGC eszközök külön modellútvonalat használnak. Az integráció nem engedélyez olyan funkciót, amelynek parancsa vagy protokollja még nincs megerősítve."]]},"/models/genie-1000":{"title":"ANTHBOT Genie 1000 Home Assistant támogatás | ANTHBOT Map","description":"ANTHBOT Genie 1000 támogatás Home Assistantban ANTHBOT Mappel: élő térkép és útvonal, zónák, ütemezések, előzmények, vezérlés és diagnosztika.","eyebrow":"Támogatott fűnyíró","heading":"ANTHBOT Genie 1000 + Home Assistant","lead":"A Genie családot támogatja az ANTHBOT Map, és a projekt közvetlenül valódi hardveren is tesztelte, beleértve a Genie 1000 ütemezéseinek betöltését.","cta":"Telepítés / dokumentáció","sections":[["Közvetlen hardveres ellenőrzés","Az ANTHBOT Map projekt valódi eszközön végzett Genie-teszteket dokumentál. A Genie 1000 natív appos ütemezésének betöltése is igazolt valódi hardveren."],["Térkép, zónák és nyírási előzmények","Az ANTHBOT Map a Genie-specifikus térkép- és útvonaldiagnosztikát elkülönítve kezeli, és támogatás esetén megjeleníti a gyephatárt, zónákat, tiltott területeket, a robot élő helyzetét, útvonalát és korábbi nyírási adatokat."],["Natív ütemezés","Az integráció a robot natív ANTHBOT appos ütemezését tükrözi a Home Assistantba, és a modell saját ütemezési útvonalát használja az M-szériás viselkedés átalakítása helyett."],["Home Assistant vezérlés","A támogatott műveletek közé tartozik a robot állapota és az alapvető nyírásvezérlés, modellfüggő útvonalon, valamint az elérhető Battery Saver és diagnosztikai eszközök."]]},"/models/m9-pro":{"title":"ANTHBOT M9 Pro Home Assistant integráció | ANTHBOT Map","description":"ANTHBOT M9 Pro támogatás Home Assistantban: vezérlés, állapot, élő térkép, útvonal, zónák, nyírási előzmények, ütemezések és diagnosztika.","eyebrow":"Közvetlenül hardveren tesztelve","heading":"ANTHBOT M9 Pro + Home Assistant","lead":"Az ANTHBOT Map külön M-szériás megvalósítást tartalmaz; az M9 Pro vezérlését, állapotát, térképét, útvonalát, zónáit és előzményeit a projekt közvetlenül valódi hardveren is tesztelte.","cta":"M9 Pro integráció dokumentációja","sections":[["Élőtérkép-architektúra","A valódi M9 Pro eszközön végzett teszt igazolta az élő WebSocket útvonalfrissítéseket, a Home Assistant újraindítás és újracsatlakozás kezelését, a snapshot-visszaállítást és a Recorder-terhelés csökkentését."],["Zónák és nyírási adatok","Az M-szériás útvonal kezeli a térképet, útvonalat, zónákat és előzményeket, miközben a modellfüggő dekódolás külön marad a Genie és N8 családtól."],["Natív ütemezés-visszaírás","Az M9 Pro ütemezés létrehozása az ANTHBOT Map kártyáról valódi hardveren igazolt; a létrehozott szabály megjelenik az ANTHBOT appban."],["Home Assistant automatizálás","A robot állapota, következő nyírása, életciklus- és ütemezési eseményei, valamint a támogatott vezérlések használhatók műszerfalakon és automatizálásokban."]]},"/models/mgc1000":{"title":"ANTHBOT MGC1000 / Pion Home Assistant támogatás | ANTHBOT Map","description":"ANTHBOT MGC1000 és Pion család támogatása ANTHBOT Mapben: elkülönített modellfelismerés, állapotnormalizálás, natív ütemezések és indítási útvonal.","eyebrow":"Pion / MGC család","heading":"ANTHBOT MGC1000 + Home Assistant","lead":"Az ANTHBOT Map külön Pion/MGC modellcsaládot kezel az MGC500, MGC750 és MGC1000 azonosítókhoz, így ezeket a robotokat nem Genie-ként kezeli.","cta":"Pion / MGC fejlesztés követése","sections":[["Külön modellkezelés","Az integráció elkülönített Pion/MGC felismerést és flat-shadow normalizálási réteget tartalmaz a Home Assistant állapotadatokhoz."],["Megerősített állapotadatok","A jelenlegi megvalósítás a robot/felhő által szolgáltatott adatokból elérhetővé teszi a megerősített vágási magasságot, nyírási haladást és területet, esőállapotot, Wi-Fi/IP adatokat, útvonal-payloadot és firmware-adatokat."],["Natív ütemezések és indítás","A Pion/MGC saját natív ütemezési formát és indítási útvonalat használ. Az integráció megtartja a napi egy időpont/teljes gyep működést, és nem alkalmaz Genie-specifikus payloadokat."],["Óvatos képességkezelés","A még nem igazolt Pion/MGC beállításírás és curpath-dekódolás szándékosan letiltva marad, amíg a protokoll és a hardver viselkedése nincs megerősítve."]]},"/voice-packs":{"title":"ANTHBOT hangcsomagok Genie robotokhoz | ANTHBOT Map","description":"ANTHBOT közösségi hangcsomagok és egyedi robothangok kompatibilis ANTHBOT Genie robotokhoz, az ANTHBOT Map rendszerébe integrálva.","eyebrow":"Közösségi hangcsomagok","heading":"ANTHBOT hangcsomagok és egyedi robothangok","lead":"Az ANTHBOT Map rendszer opcionális közösségi hangcsomagokat kínál kompatibilis ANTHBOT Genie robotokhoz, kész csomagokkal és egyedi hangigényléssel.","cta":"Hangcsomagbolt megnyitása","sections":[["Kész közösségi csomagok","Az elérhető hangcsomagok az ANTHBOT Community Hangboltban jelennek meg. A kompatibilitás csomagonként látható, és modellenként vagy firmware-verziónként eltérhet."],["Egyedi hangigénylés","Külön egyedihang-folyamat érhető el azokra az igényekre, amelyeket a kész katalógus nem fed le."],["ANTHBOT Map integráció","A megvásárolt hangjogosultság összekapcsolható az ANTHBOT Mappel, így a kompatibilis telepített rendszer felismerheti a megvásárolt csomagot anélkül, hogy a fizetős letöltési URL nyilvánossá válna."],["Független projekt","A közösségi hangcsomagok és az ANTHBOT Map független projektfunkciók. Az ANTHBOT a mindenkori jogosult védjegye; az oldal nem állít hivatalos ANTHBOT jóváhagyást."]]}}},"de":{"common":{"features":"Funktionen","models":"Modelle","voiceStore":"Voice Store","support":"Support","terms":"Bedingungen","privacy":"Datenschutz","language":"Sprache","tagModelAware":"Modellabhängig","tagIndependent":"Unabhängiges Community-Projekt","back":"Zurück zu ANTHBOT Map","project":"Projekt","platform":"Plattform","license":"Lizenz","cardSubtitle":"Anthbot Map Card · Home Assistant","cloudLive":"CLOUD LIVE","builtTitle":"Basiert auf demselben Projekt wie die Hauptseite.","noticeTitle":"Unabhängiges Projekt / Markenhinweis","noticeBody":"ANTHBOT ist eine Marke des jeweiligen Rechteinhabers. ANTHBOT Map ist ein unabhängiges Community-Projekt und kein offizielles ANTHBOT-Produkt, sofern nicht ausdrücklich anders angegeben.","github":"ANTHBOT Map GitHub","home":"Startseite","voicePackStore":"Voice-Pack-Store","refunds":"Erstattungen","rights":"Alle Rechte vorbehalten."},"pages":{"/home-assistant":{"title":"ANTHBOT Home Assistant Integration | ANTHBOT Map","description":"Unterstützte ANTHBOT Mähroboter mit ANTHBOT Map in Home Assistant integrieren: Live-Karte, Zonen, native Zeitpläne, Steuerung, Verlauf und Diagnose.","eyebrow":"Home Assistant Integration","heading":"ANTHBOT in Home Assistant mit ANTHBOT Map","lead":"ANTHBOT Map ist eine unabhängige Open-Source-Integration für Home Assistant mit Lovelace-Kartenansicht für unterstützte ANTHBOT Mähroboter.","cta":"ANTHBOT Map auf GitHub öffnen","sections":[["Was die Integration bietet","ANTHBOT Map verbindet Home Assistant mit der ANTHBOT Cloud, erstellt eine native lawn_mower-Entität, spiegelt unterstützte ANTHBOT-App-Zeitpläne und nutzt modellabhängige Steuerpfade."],["Live-Karte und Rasen-Daten","Bei unterstützten Modellen zeigt die Karte Rasenbegrenzung, Mähzonen, No-Go-Bereiche, Roboterposition, Live-Pfad und Mähabdeckung. Ein eigener WebSocket-Transport hält hochfrequente Geometriedaten aus dem Home Assistant Recorder heraus."],["Zeitpläne und Automationen","Native App-Zeitpläne können in Home Assistant gespiegelt und bei unterstützten Modellen zurückgeschrieben werden. Nächster Mähtermin, Roboterereignisse und zeitgesteuerte Mäh-/Park-Overrides lassen sich in Automationen verwenden."],["Modellabhängiges Design","Genie-, M-Serie-, N8- und Pion/MGC-Geräte verwenden getrennte Modellpfade. Nicht bestätigte Befehle oder Protokolldetails bleiben deaktiviert."]]},"/models/genie-1000":{"title":"ANTHBOT Genie 1000 Home Assistant Support | ANTHBOT Map","description":"ANTHBOT Genie 1000 Unterstützung in Home Assistant mit Live-Karte, Zonen, Zeitplänen, Verlauf, Steuerung und Diagnose.","eyebrow":"Unterstützter Mäher","heading":"ANTHBOT Genie 1000 + Home Assistant","lead":"Die Genie-Familie wird von ANTHBOT Map unterstützt und direkt auf echter Hardware getestet, einschließlich des Ladens nativer Genie-1000-Zeitpläne.","cta":"Installation / Dokumentation","sections":[["Direkte Hardware-Prüfung","Das öffentliche ANTHBOT-Map-Projekt dokumentiert Tests an echten Genie-Geräten. Auch das Laden nativer Genie-1000-App-Zeitpläne wurde auf realer Hardware bestätigt."],["Karten, Zonen und Mähverlauf","ANTHBOT Map trennt Genie-spezifische Karten-/Pfad-Diagnosen von anderen Modellfamilien und zeigt, soweit unterstützt, Begrenzung, Zonen, No-Go-Geometrie, Live-Position, Pfad und historische Mähdaten."],["Native Zeitplanung","Die Integration spiegelt den nativen ANTHBOT-App-Zeitplan des Mähers in Home Assistant und nutzt den modellspezifischen Zeitplanpfad statt M-Serie-Verhalten umzusetzen."],["Home Assistant Steuerung","Unterstützte Funktionen umfassen Status und grundlegende Mähsteuerung mit modellabhängigem Routing sowie Battery-Saver- und Diagnosewerkzeuge, sofern vom Gerät unterstützt."]]},"/models/m9-pro":{"title":"ANTHBOT M9 Pro Home Assistant Integration | ANTHBOT Map","description":"ANTHBOT M9 Pro in Home Assistant: Steuerung, Status, Live-Karte, Pfad, Zonen, Mähverlauf, Zeitpläne und Diagnose.","eyebrow":"Direkt auf Hardware getestet","heading":"ANTHBOT M9 Pro + Home Assistant","lead":"ANTHBOT Map enthält eine eigene M-Serie-Implementierung; Steuerung, Status, Karte, Pfad, Zonen und Verlauf des M9 Pro wurden direkt auf realer Hardware getestet.","cta":"M9-Pro-Dokumentation öffnen","sections":[["Live-Karten-Architektur","Tests am echten M9 Pro bestätigten Live-WebSocket-Pfadupdates, Neustart- und Reconnect-Verhalten von Home Assistant, Snapshot-Wiederherstellung und geringere Recorder-Belastung."],["Zonen und Mähdaten","Der M-Serie-Pfad unterstützt Karte, Pfad, Zonen und Verlauf und hält die modellspezifische Dekodierung von Genie und N8 getrennt."],["Native Zeitplan-Rückschreibung","Das Erstellen eines M9-Pro-Zeitplans über die ANTHBOT-Map-Karte wurde auf realer Hardware bestätigt; die Regel erscheint in der ANTHBOT App."],["Home Assistant Automation","Mäherstatus, nächster Mähtermin, Lebenszyklus-/Zeitplanereignisse und unterstützte Steuerungen können in Dashboards und Automationen genutzt werden."]]},"/models/mgc1000":{"title":"ANTHBOT MGC1000 / Pion Home Assistant Support | ANTHBOT Map","description":"ANTHBOT MGC1000 und Pion-Familie in ANTHBOT Map: getrennte Modellerkennung, Statusnormalisierung, native Zeitpläne und Start-Routing.","eyebrow":"Pion / MGC Familie","heading":"ANTHBOT MGC1000 + Home Assistant","lead":"ANTHBOT Map behandelt MGC500, MGC750 und MGC1000 als eigene Pion/MGC-Modellfamilie statt sie als Genie zu behandeln.","cta":"Pion-/MGC-Entwicklung verfolgen","sections":[["Eigene Modellbehandlung","Die Integration enthält eine getrennte Pion/MGC-Erkennung und eine Flat-Shadow-Normalisierung für Home-Assistant-Statusdaten."],["Bestätigte Statusdaten","Die aktuelle Implementierung stellt bestätigte Schnitthöhe, Mähfortschritt und -fläche, Regenstatus, Wi-Fi/IP, Pfad-Payload und Firmware-Daten bereit, wenn sie vom Mäher bzw. der Cloud geliefert werden."],["Native Zeitpläne und Start-Routing","Pion/MGC verwendet ein eigenes natives Zeitplanformat und einen eigenen Startpfad. Die Integration erhält das Verhalten mit einem Termin pro Tag und vollständiger Rasenfläche, statt Genie-Payloads anzuwenden."],["Konservative Funktionspolitik","Nicht verifizierte Pion/MGC-Schreibzugriffe und curpath-Dekodierung bleiben deaktiviert, bis Protokoll und Hardwareverhalten bestätigt sind."]]},"/voice-packs":{"title":"ANTHBOT Voice Packs für Genie Mäher | ANTHBOT Map","description":"Community-Voice-Packs und individuelle Stimmen für kompatible ANTHBOT Genie Roboter im ANTHBOT-Map-Ökosystem.","eyebrow":"Community Voice Packs","heading":"ANTHBOT Voice Packs und individuelle Mäherstimmen","lead":"Das ANTHBOT-Map-Ökosystem bietet optionale Community-Voice-Packs für kompatible ANTHBOT Genie Roboter sowie individuelle Sprachwünsche.","cta":"Voice-Pack-Store öffnen","sections":[["Fertige Community-Pakete","Verfügbare Voice Packs stehen im ANTHBOT Community Voice Store. Die Kompatibilität wird pro Paket angegeben und kann je nach Modell oder Firmware variieren."],["Individuelle Sprachwünsche","Für Wünsche außerhalb des fertigen Katalogs steht ein separater Custom-Voice-Ablauf bereit."],["ANTHBOT Map Integration","Erworbene Voice-Berechtigungen können mit ANTHBOT Map verknüpft werden, sodass kompatible Installationen das Paket erkennen, ohne kostenpflichtige Download-URLs öffentlich freizugeben."],["Unabhängiges Projekt","Community Voice Packs und ANTHBOT Map sind unabhängige Projektfunktionen. ANTHBOT ist eine Marke des jeweiligen Rechteinhabers; diese Seite behauptet keine offizielle ANTHBOT-Freigabe."]]}}},"fr":{"common":{"features":"Fonctions","models":"Modèles","voiceStore":"Boutique vocale","support":"Assistance","terms":"Conditions","privacy":"Confidentialité","language":"Langue","tagModelAware":"Selon le modèle","tagIndependent":"Projet communautaire indépendant","back":"Retour à ANTHBOT Map","project":"Projet","platform":"Plateforme","license":"Licence","cardSubtitle":"Carte Anthbot Map · Home Assistant","cloudLive":"CLOUD LIVE","builtTitle":"Basé sur le même projet que le site principal.","noticeTitle":"Projet indépendant / marque","noticeBody":"ANTHBOT est une marque de son propriétaire respectif. ANTHBOT Map est un projet communautaire indépendant et n’est pas un produit officiel ANTHBOT, sauf indication explicite.","github":"ANTHBOT Map GitHub","home":"Accueil","voicePackStore":"Boutique de voix","refunds":"Remboursements","rights":"Tous droits réservés."},"pages":{"/home-assistant":{"title":"Intégration ANTHBOT Home Assistant | ANTHBOT Map","description":"Connectez les robots tondeuses ANTHBOT pris en charge à Home Assistant avec ANTHBOT Map : carte en direct, zones, programmations natives, commandes, historique et diagnostics.","eyebrow":"Intégration Home Assistant","heading":"ANTHBOT dans Home Assistant avec ANTHBOT Map","lead":"ANTHBOT Map est une intégration Home Assistant indépendante et open source avec une carte Lovelace pour les robots tondeuses ANTHBOT pris en charge.","cta":"Voir ANTHBOT Map sur GitHub","sections":[["Ce qu'ajoute l'intégration","ANTHBOT Map relie Home Assistant au cloud ANTHBOT, crée une entité lawn_mower native, reflète les programmations prises en charge de l'application ANTHBOT et utilise des commandes adaptées au modèle."],["Carte en direct et données de pelouse","Selon le modèle, la carte affiche la limite de pelouse, les zones de tonte, les zones interdites, la position du robot, le trajet en direct et la couverture. Un transport WebSocket dédié évite d'enregistrer la géométrie haute fréquence dans Recorder."],["Programmations et automatisations","Les programmations natives de l'application peuvent être reflétées dans Home Assistant et réécrites sur les modèles pris en charge. Le prochain passage, les événements du robot et les dérogations temporisées peuvent servir dans les automatisations."],["Conception adaptée au modèle","Les appareils Genie, série M, N8 et Pion/MGC utilisent des chemins séparés. Les fonctions non confirmées restent désactivées."]]},"/models/genie-1000":{"title":"Support ANTHBOT Genie 1000 Home Assistant | ANTHBOT Map","description":"Support du Genie 1000 dans Home Assistant avec carte en direct, zones, programmations, historique, commandes et diagnostics.","eyebrow":"Tondeuse prise en charge","heading":"ANTHBOT Genie 1000 + Home Assistant","lead":"La famille Genie est prise en charge par ANTHBOT Map et a été testée directement sur du matériel réel, y compris le chargement des programmations du Genie 1000.","cta":"Installation / documentation","sections":[["Validation matérielle directe","Le projet public ANTHBOT Map documente des tests réels sur la famille Genie. Le chargement des programmations natives du Genie 1000 a également été validé sur du matériel réel."],["Cartes, zones et historique de tonte","ANTHBOT Map sépare les diagnostics carte/trajet propres à Genie et expose, lorsque disponible, limite de pelouse, zones, géométrie No-Go, position en direct, trajet et données historiques."],["Programmation native","L'intégration reflète la programmation native de l'application ANTHBOT dans Home Assistant et utilise le chemin propre au modèle au lieu de convertir le comportement de la série M."],["Commandes Home Assistant","Les opérations prises en charge incluent l'état du robot et les commandes de tonte courantes, avec routage par modèle et outils Battery Saver/diagnostics lorsque disponibles."]]},"/models/m9-pro":{"title":"Intégration ANTHBOT M9 Pro Home Assistant | ANTHBOT Map","description":"ANTHBOT M9 Pro dans Home Assistant : commandes, état, carte en direct, trajet, zones, historique, programmations et diagnostics.","eyebrow":"Testé directement sur matériel","heading":"ANTHBOT M9 Pro + Home Assistant","lead":"ANTHBOT Map possède une implémentation dédiée à la série M ; commandes, état, carte, trajet, zones et historique du M9 Pro ont été testés directement sur du matériel réel.","cta":"Documentation de l'intégration M9 Pro","sections":[["Architecture de carte en direct","Les tests sur un M9 Pro réel ont validé les mises à jour de trajet WebSocket, la gestion des redémarrages/reconnexions Home Assistant, la restauration du snapshot et la réduction de la charge Recorder."],["Zones et données de tonte","Le chemin série M gère carte, trajet, zones et historique tout en séparant le décodage spécifique du modèle de Genie et N8."],["Réécriture native des programmations","La création d'une programmation M9 Pro depuis la carte ANTHBOT Map a été validée sur du matériel réel ; la règle apparaît dans l'application ANTHBOT."],["Automatisation Home Assistant","L'état du robot, le prochain passage, les événements de cycle/programmation et les commandes prises en charge peuvent être utilisés dans les tableaux de bord et automatisations."]]},"/models/mgc1000":{"title":"Support ANTHBOT MGC1000 / Pion Home Assistant | ANTHBOT Map","description":"Support des MGC1000 et Pion dans ANTHBOT Map : détection séparée, normalisation d'état, programmations natives et routage de démarrage.","eyebrow":"Famille Pion / MGC","heading":"ANTHBOT MGC1000 + Home Assistant","lead":"ANTHBOT Map traite MGC500, MGC750 et MGC1000 comme une famille Pion/MGC dédiée au lieu de les traiter comme Genie.","cta":"Suivre le développement Pion / MGC","sections":[["Gestion dédiée du modèle","L'intégration comprend une détection Pion/MGC séparée et une couche de normalisation flat-shadow pour les états Home Assistant."],["Données d'état confirmées","L'implémentation actuelle expose hauteur de coupe, progression et surface tondues, état de pluie, Wi-Fi/IP, payload de trajet et firmware lorsque ces données sont fournies."],["Programmations natives et démarrage","Pion/MGC utilise son propre format de programmation et son propre chemin de démarrage. L'intégration conserve le fonctionnement un rendez-vous par jour/pelouse complète sans appliquer de payloads Genie."],["Politique de capacités prudente","Les écritures de réglages Pion/MGC et le décodage curpath non vérifiés restent désactivés jusqu'à confirmation du protocole et du matériel."]]},"/voice-packs":{"title":"Packs vocaux ANTHBOT pour Genie | ANTHBOT Map","description":"Packs vocaux communautaires et voix personnalisées pour robots ANTHBOT Genie compatibles dans l'écosystème ANTHBOT Map.","eyebrow":"Packs vocaux communautaires","heading":"Packs vocaux ANTHBOT et voix personnalisées","lead":"L'écosystème ANTHBOT Map propose des packs vocaux communautaires optionnels pour les robots ANTHBOT Genie compatibles, ainsi que des demandes de voix personnalisées.","cta":"Ouvrir la boutique de voix","sections":[["Packs communautaires prêts à l'emploi","Les packs disponibles sont listés dans l'ANTHBOT Community Voice Store. La compatibilité est affichée par pack et peut varier selon le modèle ou le firmware."],["Demandes de voix personnalisées","Un flux séparé permet de demander une voix qui n'est pas couverte par le catalogue prêt à l'emploi."],["Intégration ANTHBOT Map","Les droits vocaux achetés peuvent être liés à ANTHBOT Map afin que les installations compatibles reconnaissent le pack sans exposer publiquement les URL de téléchargement payantes."],["Projet indépendant","Les packs communautaires et ANTHBOT Map sont des fonctions d'un projet indépendant. ANTHBOT est une marque de son propriétaire respectif ; ce site ne prétend pas à une approbation officielle."]]}}},"es":{"common":{"features":"Funciones","models":"Modelos","voiceStore":"Tienda de voz","support":"Soporte","terms":"Términos","privacy":"Privacidad","language":"Idioma","tagModelAware":"Según el modelo","tagIndependent":"Proyecto comunitario independiente","back":"Volver a ANTHBOT Map","project":"Proyecto","platform":"Plataforma","license":"Licencia","cardSubtitle":"Tarjeta Anthbot Map · Home Assistant","cloudLive":"NUBE EN VIVO","builtTitle":"Basado en el mismo proyecto que el sitio principal.","noticeTitle":"Proyecto independiente / marca","noticeBody":"ANTHBOT es una marca de su respectivo titular. ANTHBOT Map es un proyecto comunitario independiente y no es un producto oficial de ANTHBOT salvo indicación expresa.","github":"ANTHBOT Map GitHub","home":"Inicio","voicePackStore":"Tienda de voces","refunds":"Reembolsos","rights":"Todos los derechos reservados."},"pages":{"/home-assistant":{"title":"Integración ANTHBOT Home Assistant | ANTHBOT Map","description":"Conecta cortacéspedes ANTHBOT compatibles a Home Assistant con ANTHBOT Map: mapa en vivo, zonas, horarios nativos, controles, historial y diagnósticos.","eyebrow":"Integración Home Assistant","heading":"ANTHBOT en Home Assistant con ANTHBOT Map","lead":"ANTHBOT Map es una integración independiente y de código abierto para Home Assistant con tarjeta Lovelace para cortacéspedes ANTHBOT compatibles.","cta":"Ver ANTHBOT Map en GitHub","sections":[["Qué añade la integración","ANTHBOT Map conecta Home Assistant con la nube de ANTHBOT, crea una entidad lawn_mower nativa, refleja horarios compatibles de la app ANTHBOT y usa controles específicos por modelo."],["Mapa en vivo y datos del césped","Cuando el modelo lo permite, la tarjeta muestra límite del césped, zonas, áreas No-Go, posición del robot, ruta en vivo y cobertura. Un transporte WebSocket dedicado evita guardar geometría de alta frecuencia en Recorder."],["Horarios y automatizaciones","Los horarios nativos de la app pueden reflejarse en Home Assistant y reescribirse en modelos compatibles. Próxima siega, eventos del robot y anulaciones temporizadas pueden usarse en automatizaciones."],["Diseño específico por modelo","Genie, serie M, N8 y Pion/MGC usan rutas separadas. Las funciones no confirmadas permanecen desactivadas."]]},"/models/genie-1000":{"title":"Soporte ANTHBOT Genie 1000 Home Assistant | ANTHBOT Map","description":"Soporte de Genie 1000 en Home Assistant con mapa en vivo, zonas, horarios, historial, controles y diagnósticos.","eyebrow":"Cortacésped compatible","heading":"ANTHBOT Genie 1000 + Home Assistant","lead":"La familia Genie es compatible con ANTHBOT Map y ha sido probada directamente en hardware real, incluida la carga de horarios del Genie 1000.","cta":"Instalación / documentación","sections":[["Validación directa en hardware","El proyecto público ANTHBOT Map documenta pruebas en dispositivos Genie reales. La carga de horarios nativos del Genie 1000 también se verificó en hardware real."],["Mapas, zonas e historial de siega","ANTHBOT Map mantiene separados los diagnósticos de mapa/ruta de Genie y expone, cuando está disponible, límite, zonas, geometría No-Go, posición en vivo, ruta e historial."],["Programación nativa","La integración refleja el horario nativo de la app ANTHBOT en Home Assistant y usa la ruta específica del modelo en lugar de convertir el comportamiento de la serie M."],["Controles de Home Assistant","Las operaciones compatibles incluyen estado y controles comunes de siega, con enrutamiento por modelo y herramientas Battery Saver/diagnóstico cuando están disponibles."]]},"/models/m9-pro":{"title":"Integración ANTHBOT M9 Pro Home Assistant | ANTHBOT Map","description":"ANTHBOT M9 Pro en Home Assistant: control, estado, mapa en vivo, ruta, zonas, historial, horarios y diagnósticos.","eyebrow":"Probado directamente en hardware","heading":"ANTHBOT M9 Pro + Home Assistant","lead":"ANTHBOT Map incluye una implementación específica para la serie M; control, estado, mapa, ruta, zonas e historial del M9 Pro se han probado directamente en hardware real.","cta":"Documentación de M9 Pro","sections":[["Arquitectura de mapa en vivo","Las pruebas en un M9 Pro real confirmaron actualizaciones de ruta WebSocket, reinicio/reconexión de Home Assistant, restauración de snapshot y menor carga de Recorder."],["Zonas y datos de siega","La ruta de la serie M admite mapa, ruta, zonas e historial manteniendo la decodificación específica separada de Genie y N8."],["Reescritura nativa de horarios","La creación de un horario M9 Pro desde la tarjeta ANTHBOT Map se verificó en hardware real y la regla aparece en la app ANTHBOT."],["Automatización en Home Assistant","Estado del robot, próxima siega, eventos de ciclo/horario y controles compatibles pueden usarse en paneles y automatizaciones."]]},"/models/mgc1000":{"title":"Soporte ANTHBOT MGC1000 / Pion Home Assistant | ANTHBOT Map","description":"Soporte MGC1000 y Pion en ANTHBOT Map: detección separada, normalización de estado, horarios nativos y ruta de inicio.","eyebrow":"Familia Pion / MGC","heading":"ANTHBOT MGC1000 + Home Assistant","lead":"ANTHBOT Map trata MGC500, MGC750 y MGC1000 como una familia Pion/MGC dedicada en lugar de tratarlos como Genie.","cta":"Seguir desarrollo Pion / MGC","sections":[["Gestión dedicada del modelo","La integración incluye detección Pion/MGC separada y una capa de normalización flat-shadow para los estados de Home Assistant."],["Datos de estado confirmados","La implementación actual expone altura de corte, progreso y superficie, lluvia, Wi-Fi/IP, payload de ruta y firmware cuando el robot o la nube los proporcionan."],["Horarios nativos e inicio","Pion/MGC usa su propio formato de horario y ruta de inicio. La integración conserva el comportamiento de una cita al día/césped completo sin aplicar payloads Genie."],["Política conservadora de capacidades","Las escrituras de ajustes Pion/MGC y la decodificación curpath no verificadas permanecen desactivadas hasta confirmar protocolo y hardware."]]},"/voice-packs":{"title":"Paquetes de voz ANTHBOT para Genie | ANTHBOT Map","description":"Paquetes de voz comunitarios y voces personalizadas para robots ANTHBOT Genie compatibles dentro del ecosistema ANTHBOT Map.","eyebrow":"Paquetes de voz comunitarios","heading":"Paquetes de voz ANTHBOT y voces personalizadas","lead":"El ecosistema ANTHBOT Map ofrece paquetes de voz comunitarios opcionales para robots ANTHBOT Genie compatibles y solicitudes de voz personalizada.","cta":"Abrir tienda de voces","sections":[["Paquetes comunitarios preparados","Los paquetes disponibles aparecen en la ANTHBOT Community Voice Store. La compatibilidad se indica por paquete y puede variar según el modelo o firmware."],["Solicitudes de voz personalizada","Existe un flujo separado para solicitar voces que no estén cubiertas por el catálogo preparado."],["Integración con ANTHBOT Map","Los derechos de voz comprados pueden vincularse a ANTHBOT Map para que las instalaciones compatibles reconozcan el paquete sin exponer públicamente URL de descarga de pago."],["Proyecto independiente","Los paquetes comunitarios y ANTHBOT Map son funciones de un proyecto independiente. ANTHBOT es una marca de su respectivo titular; este sitio no implica aprobación oficial."]]}}},"it":{"common":{"features":"Funzioni","models":"Modelli","voiceStore":"Negozio voci","support":"Supporto","terms":"Termini","privacy":"Privacy","language":"Lingua","tagModelAware":"Specifico per modello","tagIndependent":"Progetto community indipendente","back":"Torna ad ANTHBOT Map","project":"Progetto","platform":"Piattaforma","license":"Licenza","cardSubtitle":"Scheda Anthbot Map · Home Assistant","cloudLive":"CLOUD LIVE","builtTitle":"Basato sullo stesso progetto del sito principale.","noticeTitle":"Progetto indipendente / marchio","noticeBody":"ANTHBOT è un marchio del rispettivo titolare. ANTHBOT Map è un progetto community indipendente e non è un prodotto ufficiale ANTHBOT salvo indicazione esplicita.","github":"ANTHBOT Map GitHub","home":"Home","voicePackStore":"Negozio pacchetti voce","refunds":"Rimborsi","rights":"Tutti i diritti riservati."},"pages":{"/home-assistant":{"title":"Integrazione ANTHBOT Home Assistant | ANTHBOT Map","description":"Collega i robot tagliaerba ANTHBOT supportati a Home Assistant con ANTHBOT Map: mappa live, zone, programmi nativi, controlli, cronologia e diagnostica.","eyebrow":"Integrazione Home Assistant","heading":"ANTHBOT in Home Assistant con ANTHBOT Map","lead":"ANTHBOT Map è un'integrazione Home Assistant indipendente e open source con scheda Lovelace per i robot tagliaerba ANTHBOT supportati.","cta":"Apri ANTHBOT Map su GitHub","sections":[["Cosa aggiunge l'integrazione","ANTHBOT Map collega Home Assistant al cloud ANTHBOT, crea un'entità lawn_mower nativa, replica i programmi supportati dell'app ANTHBOT e usa controlli specifici per modello."],["Mappa live e dati del prato","Dove supportato, la scheda mostra confine del prato, zone, aree No-Go, posizione del robot, percorso live e copertura. Un trasporto WebSocket dedicato evita di salvare geometrie ad alta frequenza nel Recorder."],["Programmi e automazioni","I programmi nativi dell'app possono essere replicati in Home Assistant e riscritti sui modelli supportati. Prossimo taglio, eventi del robot e override temporizzati possono essere usati nelle automazioni."],["Design specifico per modello","Genie, serie M, N8 e Pion/MGC usano percorsi separati. Le funzioni non confermate restano disabilitate."]]},"/models/genie-1000":{"title":"Supporto ANTHBOT Genie 1000 Home Assistant | ANTHBOT Map","description":"Supporto Genie 1000 in Home Assistant con mappa live, zone, programmi, cronologia, controlli e diagnostica.","eyebrow":"Tagliaerba supportato","heading":"ANTHBOT Genie 1000 + Home Assistant","lead":"La famiglia Genie è supportata da ANTHBOT Map ed è stata testata direttamente su hardware reale, incluso il caricamento dei programmi del Genie 1000.","cta":"Installazione / documentazione","sections":[["Validazione diretta su hardware","Il progetto pubblico ANTHBOT Map documenta test su dispositivi Genie reali. Anche il caricamento dei programmi nativi del Genie 1000 è stato verificato su hardware reale."],["Mappe, zone e cronologia di taglio","ANTHBOT Map mantiene separata la diagnostica mappa/percorso di Genie e mostra, dove disponibile, confine, zone, geometria No-Go, posizione live, percorso e dati storici."],["Programmazione nativa","L'integrazione replica il programma nativo dell'app ANTHBOT in Home Assistant e usa il percorso specifico del modello invece di convertire il comportamento della serie M."],["Controlli Home Assistant","Le operazioni supportate includono stato e controlli di taglio comuni, con routing specifico per modello e strumenti Battery Saver/diagnostica quando disponibili."]]},"/models/m9-pro":{"title":"Integrazione ANTHBOT M9 Pro Home Assistant | ANTHBOT Map","description":"ANTHBOT M9 Pro in Home Assistant: controllo, stato, mappa live, percorso, zone, cronologia, programmi e diagnostica.","eyebrow":"Testato direttamente su hardware","heading":"ANTHBOT M9 Pro + Home Assistant","lead":"ANTHBOT Map include un'implementazione dedicata alla serie M; controllo, stato, mappa, percorso, zone e cronologia di M9 Pro sono stati testati direttamente su hardware reale.","cta":"Documentazione M9 Pro","sections":[["Architettura della mappa live","I test su un M9 Pro reale hanno confermato aggiornamenti percorso WebSocket, gestione riavvio/riconnessione Home Assistant, ripristino snapshot e minore carico Recorder."],["Zone e dati di taglio","Il percorso serie M supporta mappa, percorso, zone e cronologia mantenendo la decodifica specifica separata da Genie e N8."],["Riscrittura nativa dei programmi","La creazione di un programma M9 Pro dalla scheda ANTHBOT Map è stata verificata su hardware reale e la regola appare nell'app ANTHBOT."],["Automazione Home Assistant","Stato del robot, prossimo taglio, eventi ciclo/programma e controlli supportati possono essere usati in dashboard e automazioni."]]},"/models/mgc1000":{"title":"Supporto ANTHBOT MGC1000 / Pion Home Assistant | ANTHBOT Map","description":"Supporto MGC1000 e Pion in ANTHBOT Map: rilevamento separato, normalizzazione stato, programmi nativi e routing di avvio.","eyebrow":"Famiglia Pion / MGC","heading":"ANTHBOT MGC1000 + Home Assistant","lead":"ANTHBOT Map tratta MGC500, MGC750 e MGC1000 come famiglia Pion/MGC dedicata invece di considerarli Genie.","cta":"Segui sviluppo Pion / MGC","sections":[["Gestione dedicata del modello","L'integrazione include rilevamento Pion/MGC separato e uno strato di normalizzazione flat-shadow per gli stati Home Assistant."],["Dati di stato confermati","L'implementazione attuale espone altezza di taglio, avanzamento e area, pioggia, Wi-Fi/IP, payload del percorso e firmware quando forniti dal robot/cloud."],["Programmi nativi e avvio","Pion/MGC usa un proprio formato di programma e percorso di avvio. L'integrazione conserva il comportamento un appuntamento al giorno/intero prato senza applicare payload Genie."],["Politica prudente delle capacità","Scritture impostazioni Pion/MGC e decodifica curpath non verificate restano disabilitate finché protocollo e hardware non sono confermati."]]},"/voice-packs":{"title":"Pacchetti voce ANTHBOT per Genie | ANTHBOT Map","description":"Pacchetti voce community e voci personalizzate per robot ANTHBOT Genie compatibili nell'ecosistema ANTHBOT Map.","eyebrow":"Pacchetti voce community","heading":"Pacchetti voce ANTHBOT e voci personalizzate","lead":"L'ecosistema ANTHBOT Map offre pacchetti voce community opzionali per robot ANTHBOT Genie compatibili e richieste di voce personalizzata.","cta":"Apri il negozio voci","sections":[["Pacchetti community pronti","I pacchetti disponibili sono elencati nell'ANTHBOT Community Voice Store. La compatibilità è mostrata per pacchetto e può variare in base a modello o firmware."],["Richieste di voce personalizzata","È disponibile un flusso separato per richieste non coperte dal catalogo pronto."],["Integrazione ANTHBOT Map","I diritti vocali acquistati possono essere collegati ad ANTHBOT Map così le installazioni compatibili riconoscono il pacchetto senza esporre pubblicamente gli URL di download a pagamento."],["Progetto indipendente","I pacchetti community e ANTHBOT Map sono funzioni di un progetto indipendente. ANTHBOT è un marchio del rispettivo titolare; questo sito non implica approvazione ufficiale."]]}}}}''')
_PUBLIC_EXPLORE_I18N = json.loads(r'''{"en":{"title":"Explore ANTHBOT Map topics","lead":"Detailed pages for Home Assistant integration, tested mower families and Community voice packs.","ha":"ANTHBOT Home Assistant","voice":"ANTHBOT Voice Packs"},"hu":{"title":"ANTHBOT Map témakörök","lead":"Részletes oldalak a Home Assistant integrációról, a tesztelt robotcsaládokról és a közösségi hangcsomagokról.","ha":"ANTHBOT Home Assistant","voice":"ANTHBOT hangcsomagok"},"de":{"title":"ANTHBOT-Map-Themen entdecken","lead":"Detaillierte Seiten zur Home-Assistant-Integration, getesteten Mäherfamilien und Community Voice Packs.","ha":"ANTHBOT Home Assistant","voice":"ANTHBOT Voice Packs"},"fr":{"title":"Explorer les thèmes ANTHBOT Map","lead":"Pages détaillées sur l'intégration Home Assistant, les familles de tondeuses testées et les packs vocaux communautaires.","ha":"ANTHBOT Home Assistant","voice":"Packs vocaux ANTHBOT"},"es":{"title":"Explorar temas de ANTHBOT Map","lead":"Páginas detalladas sobre la integración Home Assistant, familias de cortacéspedes probadas y paquetes de voz comunitarios.","ha":"ANTHBOT Home Assistant","voice":"Paquetes de voz ANTHBOT"},"it":{"title":"Esplora gli argomenti ANTHBOT Map","lead":"Pagine dettagliate sull'integrazione Home Assistant, le famiglie di tagliaerba testate e i pacchetti voce community.","ha":"ANTHBOT Home Assistant","voice":"Pacchetti voce ANTHBOT"},"pt":{"title":"Explorar temas do ANTHBOT Map","lead":"Páginas detalhadas sobre a integração Home Assistant, famílias de robôs testadas e pacotes de voz da comunidade.","ha":"ANTHBOT Home Assistant","voice":"Pacotes de voz ANTHBOT"},"nl":{"title":"Ontdek ANTHBOT Map-onderwerpen","lead":"Uitgebreide pagina's over Home Assistant-integratie, geteste maaierfamilies en community-stempakketten.","ha":"ANTHBOT Home Assistant","voice":"ANTHBOT-stempakketten"},"pl":{"title":"Poznaj tematy ANTHBOT Map","lead":"Szczegółowe strony o integracji Home Assistant, przetestowanych rodzinach kosiarek i społecznościowych pakietach głosowych.","ha":"ANTHBOT Home Assistant","voice":"Pakiety głosowe ANTHBOT"},"cs":{"title":"Prozkoumat témata ANTHBOT Map","lead":"Podrobné stránky o integraci Home Assistant, testovaných rodinách sekaček a komunitních hlasových balíčcích.","ha":"ANTHBOT Home Assistant","voice":"Hlasové balíčky ANTHBOT"},"sk":{"title":"Preskúmať témy ANTHBOT Map","lead":"Podrobné stránky o integrácii Home Assistant, testovaných rodinách kosačiek a komunitných hlasových balíkoch.","ha":"ANTHBOT Home Assistant","voice":"Hlasové balíky ANTHBOT"},"ro":{"title":"Explorează subiectele ANTHBOT Map","lead":"Pagini detaliate despre integrarea Home Assistant, familiile de roboți testate și pachetele vocale ale comunității.","ha":"ANTHBOT Home Assistant","voice":"Pachete vocale ANTHBOT"},"da":{"title":"Udforsk ANTHBOT Map-emner","lead":"Detaljerede sider om Home Assistant-integration, testede plæneklipperfamilier og community-stemmepakker.","ha":"ANTHBOT Home Assistant","voice":"ANTHBOT-stemmepakker"},"sv":{"title":"Utforska ANTHBOT Map-ämnen","lead":"Detaljerade sidor om Home Assistant-integration, testade klipparfamiljer och community-röstpaket.","ha":"ANTHBOT Home Assistant","voice":"ANTHBOT-röstpaket"},"no":{"title":"Utforsk ANTHBOT Map-emner","lead":"Detaljerte sider om Home Assistant-integrasjon, testede klipperfamilier og community-stemmepakker.","ha":"ANTHBOT Home Assistant","voice":"ANTHBOT-stemmepakker"},"fi":{"title":"Tutustu ANTHBOT Map -aiheisiin","lead":"Yksityiskohtaisia sivuja Home Assistant -integraatiosta, testatuista leikkuriperheistä ja yhteisön äänipaketeista.","ha":"ANTHBOT Home Assistant","voice":"ANTHBOT-äänipaketit"},"zh-CN":{"title":"探索 ANTHBOT Map 主题","lead":"了解 Home Assistant 集成、已测试的割草机系列和社区语音包。","ha":"ANTHBOT Home Assistant","voice":"ANTHBOT 语音包"},"zh-TW":{"title":"探索 ANTHBOT Map 主題","lead":"了解 Home Assistant 整合、已測試的割草機系列和社群語音包。","ha":"ANTHBOT Home Assistant","voice":"ANTHBOT 語音包"},"tr":{"title":"ANTHBOT Map konularını keşfet","lead":"Home Assistant entegrasyonu, test edilen biçme makinesi aileleri ve topluluk ses paketleri için ayrıntılı sayfalar.","ha":"ANTHBOT Home Assistant","voice":"ANTHBOT Ses Paketleri"},"th":{"title":"สำรวจหัวข้อ ANTHBOT Map","lead":"หน้ารายละเอียดเกี่ยวกับการเชื่อมต่อ Home Assistant รุ่นหุ่นยนต์ที่ทดสอบแล้ว และชุดเสียงชุมชน","ha":"ANTHBOT Home Assistant","voice":"ชุดเสียง ANTHBOT"},"vi":{"title":"Khám phá các chủ đề ANTHBOT Map","lead":"Các trang chi tiết về tích hợp Home Assistant, dòng máy đã kiểm thử và gói giọng nói cộng đồng.","ha":"ANTHBOT Home Assistant","voice":"Gói giọng ANTHBOT"},"ko":{"title":"ANTHBOT Map 주제 살펴보기","lead":"Home Assistant 통합, 테스트된 잔디깎이 제품군 및 커뮤니티 음성 팩에 대한 자세한 페이지입니다.","ha":"ANTHBOT Home Assistant","voice":"ANTHBOT 음성 팩"},"km":{"title":"ស្វែងយល់ប្រធានបទ ANTHBOT Map","lead":"ទំព័រលម្អិតអំពីការរួមបញ្ចូល Home Assistant គ្រួសារម៉ាស៊ីនកាត់ស្មៅដែលបានសាកល្បង និងកញ្ចប់សំឡេងសហគមន៍។","ha":"ANTHBOT Home Assistant","voice":"កញ្ចប់សំឡេង ANTHBOT"}}''')
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


class OwnerPairPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pair_code: str = Field(min_length=20, max_length=160)

    @field_validator("pair_code")
    @classmethod
    def _validate_pair_code(cls, value: str) -> str:
        normalized = value.strip()
        if not _PAIR_RE.fullmatch(normalized):
            raise ValueError("invalid store pairing code")
        return normalized


class StoreAccountLinkPayload(OwnerPairPayload):
    pass


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
    name = os.environ.get("ANTHBOT_PRIVACY_CONTROLLER_NAME", "").strip()
    if not name or name.casefold() == "mqb retrofit hungary":
        return "ANTHBOT Map"
    return name


def _privacy_controller_address() -> str:
    return os.environ.get("ANTHBOT_PRIVACY_CONTROLLER_ADDRESS", "").strip()


def _privacy_contact_email() -> str:
    email = os.environ.get("ANTHBOT_PRIVACY_CONTACT_EMAIL", "").strip()
    if not email or email.casefold() == "support@mqbretrofithungary.online":
        return "support@anthbotmap.com"
    return email


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
                user_id TEXT,
                entitlement_scope TEXT,
                installation_email_status TEXT,
                installation_email_attempted_at INTEGER,
                installation_email_sent_at TEXT,
                installation_email_error TEXT,
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

            CREATE TABLE IF NOT EXISTS store_owner_clients (
                client_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
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
        if "user_id" not in columns:
            conn.execute("ALTER TABLE store_orders ADD COLUMN user_id TEXT")
        if "installation_email_status" not in columns:
            conn.execute(
                "ALTER TABLE store_orders "
                "ADD COLUMN installation_email_status TEXT"
            )
        if "installation_email_attempted_at" not in columns:
            conn.execute(
                "ALTER TABLE store_orders "
                "ADD COLUMN installation_email_attempted_at INTEGER"
            )
        if "installation_email_sent_at" not in columns:
            conn.execute(
                "ALTER TABLE store_orders "
                "ADD COLUMN installation_email_sent_at TEXT"
            )
        if "installation_email_error" not in columns:
            conn.execute(
                "ALTER TABLE store_orders "
                "ADD COLUMN installation_email_error TEXT"
            )
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_store_orders_client_id
            ON store_orders(client_id);
            CREATE INDEX IF NOT EXISTS idx_store_orders_community_id
            ON store_orders(community_id);
            CREATE INDEX IF NOT EXISTS idx_store_orders_user_id
            ON store_orders(user_id);
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


def _is_owner_client(client_id: str) -> bool:
    """Return whether one anonymous Map client has maintainer-owner access."""
    _init_store_tables()
    with core._db() as conn:
        row = conn.execute(
            "SELECT 1 FROM store_owner_clients WHERE client_id = ?",
            (client_id,),
        ).fetchone()
    return row is not None


def _grant_owner_client(client_id: str) -> None:
    """Persist maintainer-owner access for one anonymous Map client."""
    _init_store_tables()
    now = core._iso()
    with core._db() as conn:
        conn.execute(
            """
            INSERT INTO store_owner_clients (client_id, created_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(client_id) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (client_id, now, now),
        )


def _revoke_owner_client(client_id: str) -> bool:
    """Remove maintainer-owner access from one anonymous Map client."""
    _init_store_tables()
    with core._db() as conn:
        cursor = conn.execute(
            "DELETE FROM store_owner_clients WHERE client_id = ?",
            (client_id,),
        )
    return bool(cursor.rowcount)


def _owner_access_for_pack(client_id: str, pack: dict[str, Any]) -> str:
    """Return a signed non-purchase token for one owner client + stable voice."""
    secret = _license_secret()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="voice store license secret is not configured",
        )
    voice_id = (
        str(pack.get("community_id", "")).strip()
        or str(pack.get("id", "")).strip()
    )
    payload = f"{client_id}|{voice_id}"
    encoded = (
        base64.urlsafe_b64encode(payload.encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    signature = hmac.new(
        secret.encode("utf-8"),
        f"owner|{payload}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"abo1.{encoded}.{signature}"


def _owner_access_client_for_pack(
    owner_token: str,
    pack: dict[str, Any],
) -> str:
    """Validate owner access for the requested current voice-pack version."""
    match = _OWNER_ACCESS_RE.fullmatch(owner_token.strip())
    if not match:
        raise HTTPException(status_code=401, detail="invalid owner voice access")

    encoded, supplied_signature = match.groups()
    padding = "=" * ((4 - len(encoded) % 4) % 4)
    try:
        payload = base64.urlsafe_b64decode(
            (encoded + padding).encode("ascii")
        ).decode("utf-8")
        client_id, voice_id = payload.split("|", 1)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=401, detail="invalid owner voice access")

    stable_voice_id = (
        str(pack.get("community_id", "")).strip()
        or str(pack.get("id", "")).strip()
    )
    if not client_id or voice_id != stable_voice_id or not _is_owner_client(client_id):
        raise HTTPException(status_code=401, detail="owner voice access is not active")

    expected = _owner_access_for_pack(client_id, pack).rsplit(".", 1)[1]
    if not secrets.compare_digest(supplied_signature, expected):
        raise HTTPException(status_code=401, detail="invalid owner voice access")
    return client_id


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


def _paid_order_for_user_pack(
    user_id: str,
    record: dict[str, Any],
) -> dict[str, Any] | None:
    """Return an active purchase owned by one verified Voice Store account."""
    _init_store_tables()
    pack_id = str(record.get("id", "")).strip()
    community_id = str(record.get("community_id", "")).strip()
    with core._db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM store_orders
            WHERE user_id = ?
              AND payment_status = 'paid'
              AND (
                    (? != '' AND community_id = ?)
                    OR pack_id = ?
                  )
            ORDER BY COALESCE(paid_at, updated_at) DESC
            LIMIT 1
            """,
            (
                user_id,
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

    uploaded_ids = {
        str(item.get("id", "")).strip()
        for item in _uploaded_records()
        if str(item.get("id", "")).strip()
    }
    base = core._public_base_url(request)
    for item in packs:
        pack_id = str(item.get("id", "")).strip()
        if pack_id not in uploaded_ids:
            continue
        item["preview_samples"] = [
            {
                "sample": index,
                "url": (
                    f"{base}/api/anthbot/store/voice-packs/{quote(pack_id)}"
                    f"/preview/{index}"
                ),
            }
            for index in range(1, len(_VOICE_PREVIEW_FILES) + 1)
        ]

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
    user_id: str | None = None,
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
    if user_id:
        metadata["store_user_id"] = user_id
    if entitlement_scope in {"map", "web"}:
        metadata["entitlement_scope"] = entitlement_scope

    payment_metadata = {
        "pack_id": pack_id,
        "community_id": community_id,
    }
    if client_id:
        payment_metadata["store_client_id"] = client_id
    if user_id:
        payment_metadata["store_user_id"] = user_id
    if entitlement_scope in {"map", "web"}:
        payment_metadata["entitlement_scope"] = entitlement_scope

    account = store_accounts.get_user(user_id) if user_id else None
    params: dict[str, Any] = {
        "mode": "payment",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": pack_id,
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
    if account is not None:
        stripe_customer_id = str(account.get("stripe_customer_id") or "").strip()
        account_email = str(account.get("email") or "").strip()
        if stripe_customer_id:
            params["customer"] = stripe_customer_id
        else:
            params["customer_creation"] = "always"
            if account_email:
                params["customer_email"] = account_email
    else:
        params["customer_creation"] = "always"

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


def _session_user_id(session: dict[str, Any]) -> str | None:
    metadata = session.get("metadata")
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("store_user_id")
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized.startswith("usr_") or len(normalized) > 96:
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
    user_id = _session_user_id(session)
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
                stripe_payment_intent_id, client_id, user_id, entitlement_scope,
                created_at, updated_at, paid_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                user_id=COALESCE(excluded.user_id, store_orders.user_id),
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
                user_id,
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
    result = dict(row)
    if user_id and customer_id:
        store_accounts.set_stripe_customer_id(user_id, customer_id)
    return result


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


def _purchase_pack_name(pack: dict[str, Any]) -> str:
    language = str(pack.get("language") or pack.get("english_name") or "ANTHBOT").strip()
    variant = str(pack.get("variant_name") or "").strip()
    return f"{language} – {variant}" if variant else language


def _deliver_purchase_installation_email(order: dict[str, Any]) -> None:
    """Deliver the installation guide for one paid Voice Store order."""
    pack = _resolve_order_pack(order)
    user_id = str(order.get("user_id") or "").strip()
    user = store_accounts.get_user(user_id) if user_id else None
    email = (
        str(user.get("email") or "").strip()
        if user is not None
        else ""
    ) or str(order.get("customer_email") or "").strip()
    if not email:
        raise RuntimeError("purchase has no customer email")

    language = (
        str(user.get("preferred_language") or "en").strip()
        if user is not None
        else "en"
    ) or "en"
    scope = str(order.get("entitlement_scope") or "").strip().casefold()
    map_linked = scope == "map" or (not scope and bool(order.get("client_id")))
    session_id = str(order.get("stripe_session_id") or "").strip()
    store_accounts.send_purchase_installation_email(
        email,
        language=language,
        pack_name=_purchase_pack_name(pack),
        license_key=_license_for_order(order),
        success_url=(
            f"{_PUBLIC_SITE_BASE_URL}/store/success"
            f"?session_id={quote(session_id)}"
        ),
        map_linked=map_linked,
    )


def _purchase_email_error_detail(err: Exception) -> str:
    if isinstance(
        err,
        (
            smtplib.SMTPException,
            ssl.SSLError,
            TimeoutError,
            OSError,
        ),
    ):
        return store_accounts._smtp_failure_detail(err)
    return type(err).__name__


def _send_purchase_installation_email_once(order: dict[str, Any]) -> bool:
    """Send one installation email per paid Stripe order.

    The database claim makes webhook + success-page polling idempotent. A failed
    send can retry after 60 seconds; an interrupted in-flight send can be
    reclaimed after two minutes.
    """
    if str(order.get("payment_status") or "").strip().casefold() != "paid":
        return False

    session_id = str(order.get("stripe_session_id") or "").strip()
    if not _SESSION_RE.fullmatch(session_id):
        return False

    now_epoch = int(time.time())
    _init_store_tables()
    with core._db() as conn:
        cursor = conn.execute(
            """
            UPDATE store_orders
            SET installation_email_status = 'sending',
                installation_email_attempted_at = ?,
                installation_email_error = NULL
            WHERE stripe_session_id = ?
              AND installation_email_sent_at IS NULL
              AND COALESCE(installation_email_attempted_at, 0) <= ?
              AND (
                    installation_email_status IS NULL
                    OR installation_email_status = ''
                    OR installation_email_status = 'failed'
                    OR (
                        installation_email_status = 'sending'
                        AND COALESCE(installation_email_attempted_at, 0) <= ?
                    )
                  )
            """,
            (
                now_epoch,
                session_id,
                now_epoch - 60,
                now_epoch - 120,
            ),
        )
        if not cursor.rowcount:
            return False

    try:
        _deliver_purchase_installation_email(order)
    except Exception as err:
        safe_error = _purchase_email_error_detail(err)
        with core._db() as conn:
            conn.execute(
                """
                UPDATE store_orders
                SET installation_email_status = 'failed',
                    installation_email_error = ?
                WHERE stripe_session_id = ?
                """,
                (str(safe_error)[:240], session_id),
            )
        _LOGGER.warning(
            "Purchase installation email failed for %s: %s",
            session_id,
            safe_error,
        )
        return False

    with core._db() as conn:
        conn.execute(
            """
            UPDATE store_orders
            SET installation_email_status = 'sent',
                installation_email_sent_at = ?,
                installation_email_error = NULL
            WHERE stripe_session_id = ?
            """,
            (core._iso(), session_id),
        )
    return True


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
        "installation_email_sent": bool(order.get("installation_email_sent_at")),
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


def _account_purchase_public(
    order: dict[str, Any],
    request: Request,
) -> dict[str, Any]:
    try:
        pack = _resolve_order_pack(order)
        public_pack: dict[str, Any] = _public_paid_pack(pack, request)
        pack_name = _purchase_pack_name(pack)
    except HTTPException:
        pack_id = str(order.get("pack_id") or "").strip()
        community_id = str(order.get("community_id") or "").strip()
        public_pack = {
            "id": pack_id,
            "community_id": community_id or None,
            "language": "Voice pack",
            "variant_name": community_id or pack_id or "Purchased voice",
            "access": "paid",
        }
        pack_name = community_id or pack_id or "Purchased voice"

    status = str(order.get("payment_status") or "").strip().casefold()
    session_id = str(order.get("stripe_session_id") or "").strip()
    scope = str(order.get("entitlement_scope") or "").strip().casefold()
    legacy_linked = not scope and bool(order.get("client_id"))
    attempted_at = int(order.get("installation_email_attempted_at") or 0)
    body: dict[str, Any] = {
        "stripe_session_id": session_id,
        "pack_id": order.get("pack_id"),
        "community_id": order.get("community_id"),
        "pack_name": pack_name,
        "pack": public_pack,
        "payment_status": status,
        "amount_total": order.get("amount_total"),
        "currency": order.get("currency"),
        "paid_at": order.get("paid_at"),
        "created_at": order.get("created_at"),
        "map_linked": scope == "map" or legacy_linked,
        "web_installer_linked": scope == "web",
        "installation_email_sent": bool(order.get("installation_email_sent_at")),
        "email_resend_available": (
            status == "paid"
            and attempted_at <= int(time.time()) - 60
        ),
        "installation_url": (
            f"{_PUBLIC_SITE_BASE_URL}/store/success"
            f"?session_id={quote(session_id)}"
        ),
    }
    if status == "paid":
        body["license_key"] = _license_for_order(order)
    return body


def _account_order_for_user(session_id: str, user_id: str) -> dict[str, Any]:
    if not _SESSION_RE.fullmatch(session_id):
        raise HTTPException(status_code=422, detail="invalid checkout session id")
    _init_store_tables()
    with core._db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM store_orders
            WHERE stripe_session_id = ? AND user_id = ?
            """,
            (session_id, user_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="purchase not found")
    return dict(row)


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


def _brand_asset_bytes(filename: str) -> bytes:
    path = Path(__file__).with_name("assets") / filename
    try:
        encoded = path.read_text(encoding="ascii").strip()
        return base64.b64decode(encoded, validate=True)
    except (OSError, ValueError) as err:
        raise HTTPException(status_code=503, detail="brand asset unavailable") from err


def _brand_link(*, css_class: str = "anthbot-brand") -> str:
    return (
        f'<a class="{css_class}" href="/" aria-label="ANTHBOT Map home">'
        f'{_BRAND_LOGO_IMG}</a>'
    )


def _apply_public_branding(name: str, html: str) -> str:
    brand = _brand_link()
    if name == "public_site.html":
        html = html.replace(
            '<a class="brand" href="/"><span class="logo">A</span><span>ANTHBOT Map</span></a>',
            _brand_link(css_class="brand anthbot-brand"),
            1,
        )
    elif name in {"public_terms.html", "public_refunds.html", "public_privacy.html"}:
        html = html.replace(
            '<a class="brand" href="/">ANTHBOT Map</a>',
            _brand_link(css_class="brand anthbot-brand"),
            1,
        )
    elif name == "store.html":
        html = html.replace(
            '<nav class="navlinks" aria-label="Site navigation">',
            '<nav class="navlinks" aria-label="Site navigation">' + brand,
            1,
        )
    elif name == "store_success.html":
        html = html.replace(
            '<div class="eyebrow">ANTHBOT Community</div>',
            brand,
            1,
        )
    return html


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

    html = _apply_public_branding(name, html)
    title = str(page["title"])
    description = str(page["description"])
    canonical = f"{_PUBLIC_SITE_BASE_URL}{page['path']}"
    indexed = bool(page["index"])

    # Keep the rendered English title/description aligned with the server-side
    # metadata after the page language helper runs in the browser.
    if name == "public_site.html":
        html = html.replace(
            "ANTHBOT Map | Home Assistant Integration & Digital Tools",
            title,
        )
        html = html.replace(
            "ANTHBOT Map develops the independent open-source ANTHBOT Map "
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
        _FAVICON_HEAD,
        _BRAND_STYLE,
        _google_site_verification_meta(),
        f'<link rel="canonical" href="{escape(canonical, quote=True)}">',
        f'<meta name="robots" content="{robots}">',
        '<meta property="og:type" content="website">',
        '<meta property="og:site_name" content="ANTHBOT Map">',
        f'<meta property="og:title" content="{escape(title, quote=True)}">',
        f'<meta property="og:description" content="{escape(description, quote=True)}">',
        f'<meta property="og:url" content="{escape(canonical, quote=True)}">',
        f'<meta property="og:image" content="{escape(_SOCIAL_IMAGE_URL, quote=True)}">',
        '<meta property="og:image:alt" content="ANTHBOT Map">',
        '<meta property="og:image:type" content="image/webp">',
        '<meta property="og:image:width" content="480">',
        '<meta property="og:image:height" content="160">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{escape(title, quote=True)}">',
        f'<meta name="twitter:description" content="{escape(description, quote=True)}">',
        f'<meta name="twitter:image" content="{escape(_SOCIAL_IMAGE_URL, quote=True)}">',
        '<meta name="twitter:image:alt" content="ANTHBOT Map">',
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
                    "name": "ANTHBOT Map",
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
    return _apply_public_accessibility(html)


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
{_FAVICON_HEAD}
{_BRAND_STYLE}
{_google_site_verification_meta()}
<link rel="canonical" href="{escape(canonical, quote=True)}">
<meta name="robots" content="index,follow,max-image-preview:large,max-snippet:-1,max-video-preview:-1">
<meta property="og:type" content="website">
<meta property="og:site_name" content="ANTHBOT Map">
<meta property="og:title" content="{escape(title, quote=True)}">
<meta property="og:description" content="{escape(description, quote=True)}">
<meta property="og:url" content="{escape(canonical, quote=True)}">
<meta property="og:image" content="{escape(_SOCIAL_IMAGE_URL, quote=True)}">
<meta property="og:image:alt" content="ANTHBOT Map">
<meta property="og:image:type" content="image/webp">
<meta property="og:image:width" content="480">
<meta property="og:image:height" content="160">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{escape(title, quote=True)}">
<meta name="twitter:description" content="{escape(description, quote=True)}">
<meta name="twitter:image" content="{escape(_SOCIAL_IMAGE_URL, quote=True)}">
<meta name="twitter:image:alt" content="ANTHBOT Map">
<script type="application/ld+json">{structured}</script>
{_PUBLIC_TYPOGRAPHY_STYLE}
{_PUBLIC_A11Y_STYLE}
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
.brand{{display:flex;align-items:center;text-decoration:none;font-weight:850}}
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
<a class="skip-link" href="#main-content">Skip to content</a>
<div class="bg-grid"></div>
<nav class="nav"><div class="wrap navin">
  <a class="brand anthbot-brand" href="/" aria-label="ANTHBOT Map home">{_BRAND_LOGO_IMG}</a>
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

<main id="main-content">
<section class="section"><div class="wrap">
  <span class="eyebrow">ANTHBOT Map</span>
  <h2 data-i18n="builtTitle">Built around the same project as the main site.</h2>
  <p class="lead" data-i18n="pageDescription">{escape(description)}</p>
  <div class="feature-grid">{sections}</div>
</div></section>

<section class="section"><div class="wrap">
  <div class="open-source glass">
    <div><strong data-i18n="noticeTitle">Independent project / trademark notice</strong><p data-i18n="noticeBody">ANTHBOT is a trademark of its respective owner. ANTHBOT Map is an independent community project and is not an official ANTHBOT product unless explicitly stated otherwise.</p></div>
    <a class="btn" href="https://github.com/Mqbretrofit/ha-anthbot-map-v2" target="_blank" rel="noopener" data-i18n="github">ANTHBOT Map GitHub</a>
  </div>
</div></section>
</main>

<footer class="footer"><div class="wrap">© 2026 ANTHBOT Map. <span data-i18n="rights">All rights reserved.</span><div class="foot"><a href="/" data-i18n="home">Home</a><a href="https://github.com/Mqbretrofit/ha-anthbot-map-v2" target="_blank" rel="noopener" data-i18n="github">ANTHBOT Map GitHub</a><a href="/store" data-i18n="voicePackStore">Voice Pack Store</a><a href="/refunds" data-i18n="refunds">Refunds</a><a href="/terms" data-i18n="terms">Terms</a><a href="/privacy" data-i18n="privacy">Privacy</a></div></div></footer>
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


@router.get("/favicon.png", include_in_schema=False)
def public_favicon_png() -> Response:
    return Response(
        content=_brand_asset_bytes("anthbot_map_icon.b64"),
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=604800, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/favicon.svg", include_in_schema=False)
def public_favicon_svg() -> Response:
    return RedirectResponse(
        url="/favicon.png?v=3",
        status_code=307,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/favicon.ico", include_in_schema=False)
def public_favicon_ico() -> Response:
    return RedirectResponse(
        url="/favicon.png?v=3",
        status_code=307,
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/site.webmanifest", include_in_schema=False)
def public_site_manifest() -> Response:
    manifest = {
        "id": "/",
        "name": "ANTHBOT Map",
        "short_name": "ANTHBOT Map",
        "description": (
            "ANTHBOT Map for Home Assistant: maps, zones, schedules, "
            "diagnostics and Community voice packs."
        ),
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#081017",
        "theme_color": "#081017",
        "icons": [
            {
                "src": "/favicon.png?v=3",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": "/app-icon-512.svg?v=1",
                "sizes": "512x512",
                "type": "image/svg+xml",
                "purpose": "any",
            },
        ],
        "shortcuts": [
            {
                "name": "Voice Store",
                "short_name": "Voice Store",
                "url": "/store",
                "icons": [
                    {
                        "src": "/favicon.png?v=3",
                        "sizes": "192x192",
                        "type": "image/png",
                    }
                ],
            }
        ],
    }
    return Response(
        content=json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
        media_type="application/manifest+json",
        headers={
            "Cache-Control": "public, max-age=604800",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/app-icon-512.svg", include_in_schema=False)
def public_app_icon_svg() -> Response:
    path = Path(__file__).with_name("assets") / "anthbot_map_icon.b64"
    try:
        encoded = path.read_text(encoding="ascii").strip()
        base64.b64decode(encoded, validate=True)
    except (OSError, ValueError) as err:
        raise HTTPException(status_code=503, detail="app icon unavailable") from err
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" '
        'viewBox="0 0 512 512" role="img" aria-label="ANTHBOT Map">'
        '<image width="512" height="512" preserveAspectRatio="xMidYMid slice" '
        f'href="data:image/png;base64,{encoded}"/>'
        "</svg>"
    )
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "public, max-age=604800, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/brand/anthbot-map-logo.webp", include_in_schema=False)
def public_brand_logo() -> Response:
    return Response(
        content=_brand_asset_bytes("anthbot_map_logo.b64"),
        media_type="image/webp",
        headers={
            "Cache-Control": "public, max-age=604800, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/assets/genie-mower.png", include_in_schema=False)
def public_genie_mower_image() -> Response:
    return Response(
        content=_brand_asset_bytes("public_genie_mower.b64"),
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=604800, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/assets/m-series-mower.png", include_in_schema=False)
def public_m_series_mower_image() -> Response:
    return Response(
        content=_brand_asset_bytes("public_m_series_mower.b64"),
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=604800, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/public-site-i18n.js", include_in_schema=False)
def public_site_i18n_script() -> Response:
    path = Path(__file__).with_name("public_site_i18n.js")
    if not path.is_file():
        raise HTTPException(
            status_code=503,
            detail="public site translation asset unavailable",
        )
    return FileResponse(
        path,
        media_type="application/javascript",
        headers={
            "Cache-Control": "public, max-age=604800, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


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
    user = store_accounts.current_user(request)
    if user is None:
        return catalog
    user_id = str(user["user_id"])

    packs: list[dict[str, Any]] = []
    for item in catalog.get("packs", []):
        if not isinstance(item, dict):
            continue
        public = dict(item)
        if str(public.get("access") or "free").casefold() == "paid":
            try:
                record = _find_uploaded_pack(str(public.get("id") or ""))
                order = _paid_order_for_user_pack(
                    user_id,
                    record,
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

    user = store_accounts.require_user(request)
    user_id = str(user["user_id"])
    client_id = _client_id_from_token(payload.client_token)
    existing_order = _paid_order_for_user_pack(
        user_id,
        record,
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
        user_id=user_id,
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


@router.post("/api/anthbot/store/account/link-map")
def link_store_account_map(
    payload: StoreAccountLinkPayload,
    request: Request,
) -> dict[str, Any]:
    user = store_accounts.require_user(request)
    client_id = _client_id_from_pairing(payload.pair_code)
    if client_id is None:
        raise HTTPException(status_code=404, detail="voice store pairing not found")
    store_accounts.link_client_to_user(client_id, str(user["user_id"]))
    return {
        "linked": True,
        "client_suffix": client_id[-10:],
    }


@router.get("/api/anthbot/store/account/map-link")
def store_account_map_link_status(
    request: Request,
    pair: str,
) -> dict[str, Any]:
    user = store_accounts.current_user(request)
    if user is None:
        return {"authenticated": False, "linked": False}
    client_id = _client_id_from_pairing(pair)
    linked_user_id = store_accounts.user_id_for_client(client_id) if client_id else None
    return {
        "authenticated": True,
        "linked": linked_user_id == str(user["user_id"]),
        "client_suffix": client_id[-10:] if client_id else None,
    }


@router.post("/api/anthbot/store/client/entitlements")
def store_client_entitlements(
    payload: StoreClientPayload,
    request: Request,
) -> dict[str, Any]:
    client_id = _client_id_from_token(payload.client_token)
    _init_store_tables()

    if _is_owner_client(client_id):
        base = core._public_base_url(request)
        owner_packs: list[dict[str, Any]] = []
        for pack in _uploaded_records():
            if not _is_paid(pack):
                continue
            pack_id = str(pack.get("id", "")).strip()
            if not pack_id:
                continue
            owner_token = _owner_access_for_pack(client_id, pack)
            public = _public_paid_pack(pack, request)
            public["music_url"] = (
                f"{base}/api/anthbot/store/voice-packs/{quote(pack_id)}/owner-download"
                f"?owner={quote(owner_token)}"
            )
            public["entitlement"] = "owner"
            public["owner_access"] = True
            owner_packs.append(public)

        return {
            "licensed": bool(owner_packs),
            "license_version": 1,
            "owner_access": True,
            "packs": owner_packs,
        }
    linked_user_id = store_accounts.user_id_for_client(client_id)
    with core._db() as conn:
        if linked_user_id:
            rows = conn.execute(
                """
                SELECT *
                FROM store_orders
                WHERE user_id = ?
                  AND payment_status = 'paid'
                ORDER BY COALESCE(paid_at, updated_at) DESC
                """,
                (linked_user_id,),
            ).fetchall()
        else:
            # Legacy fallback: keep previously purchased Map-linked voices
            # working until that Map installation is linked to a store account.
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
        "owner_access": False,
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

    user = store_accounts.require_user(request)
    user_id = str(user["user_id"])
    pair_client_id = _client_id_from_pairing(payload.pair_code)
    entitlement_scope = "map" if pair_client_id is not None else "web"
    if pair_client_id is not None:
        linked_user_id = store_accounts.user_id_for_client(pair_client_id)
        if linked_user_id != user_id:
            raise HTTPException(
                status_code=409,
                detail="Link this ANTHBOT Map to your Voice Store account before purchase",
            )
    client_id = pair_client_id or _browser_store_client_id(request)

    existing_order = _paid_order_for_user_pack(
        user_id,
        record,
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
        user_id=user_id,
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


@router.get("/api/anthbot/store/direct-checkout")
async def direct_map_store_checkout(
    request: Request,
    pair: str,
    voice_id: str | None = None,
    pack_id: str | None = None,
) -> Response:
    """Open Stripe Checkout directly for one paid voice selected in ANTHBOT Map.

    Prefer the stable Community voice ID so a Map selection keeps working when
    the uploaded pack/version ID changes. pack_id remains accepted for backward
    compatibility with an already-cached frontend.
    """
    pair_code = pair.strip()
    normalized_voice_id = str(voice_id or "").strip()
    normalized_pack_id = str(pack_id or "").strip()
    if not _PAIR_RE.fullmatch(pair_code):
        raise HTTPException(status_code=422, detail="invalid store pairing code")
    if normalized_voice_id:
        if len(normalized_voice_id) > 160:
            raise HTTPException(status_code=422, detail="invalid Community voice id")
        record = _find_uploaded_pack_by_community_id(normalized_voice_id)
        normalized_pack_id = str(record.get("id") or "").strip()
    if not normalized_pack_id or len(normalized_pack_id) > 160:
        raise HTTPException(status_code=422, detail="invalid voice pack id")

    pair_client_id = _client_id_from_pairing(pair_code)
    user = store_accounts.current_user(request)
    linked_user_id = (
        store_accounts.user_id_for_client(pair_client_id)
        if pair_client_id is not None
        else None
    )
    if user is None or linked_user_id != str(user["user_id"]):
        base = core._public_base_url(request)
        voice_query = (
            f"&voice_id={quote(normalized_voice_id)}"
            if normalized_voice_id
            else f"&pack_id={quote(normalized_pack_id)}"
        )
        return RedirectResponse(
            url=(
                f"{base}/store?pair={quote(pair_code)}"
                f"&checkout=1{voice_query}"
            ),
            status_code=303,
        )

    result = await create_store_checkout(
        CheckoutPayload(pack_id=normalized_pack_id, pair_code=pair_code),
        request,
    )
    session_id = str(result.get("session_id") or "").strip()
    if result.get("already_owned"):
        if not session_id:
            raise HTTPException(status_code=409, detail="voice pack is already owned")
        base = core._public_base_url(request)
        return RedirectResponse(
            url=f"{base}/store/success?session_id={quote(session_id)}",
            status_code=303,
        )

    checkout_url = result.get("checkout_url")
    if not isinstance(checkout_url, str) or not checkout_url.startswith("https://"):
        raise HTTPException(status_code=502, detail="Stripe did not return a checkout URL")
    return RedirectResponse(url=checkout_url, status_code=303)


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
        order = _upsert_order_from_session(session)
        if str(order.get("payment_status") or "").casefold() == "paid":
            await asyncio.to_thread(
                _send_purchase_installation_email_once,
                order,
            )
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


@router.get("/api/anthbot/store/account/purchases")
def account_purchases(request: Request) -> dict[str, Any]:
    user = store_accounts.require_user(request)
    user_id = str(user["user_id"])
    _init_store_tables()
    with core._db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM store_orders
            WHERE user_id = ?
              AND payment_status IN ('paid', 'refunded')
            ORDER BY COALESCE(paid_at, created_at) DESC, created_at DESC
            LIMIT 100
            """,
            (user_id,),
        ).fetchall()
    return {
        "purchases": [
            _account_purchase_public(dict(row), request)
            for row in rows
        ]
    }


@router.post(
    "/api/anthbot/store/account/purchases/{session_id}/resend-email"
)
async def resend_account_purchase_email(
    session_id: str,
    request: Request,
) -> dict[str, Any]:
    user = store_accounts.require_user(request)
    order = _account_order_for_user(session_id, str(user["user_id"]))
    if str(order.get("payment_status") or "").casefold() != "paid":
        raise HTTPException(
            status_code=409,
            detail="installation email is only available for active purchases",
        )

    now_epoch = int(time.time())
    attempted_at = int(order.get("installation_email_attempted_at") or 0)
    if attempted_at > now_epoch - 60:
        raise HTTPException(
            status_code=429,
            detail="Please wait before resending the installation email",
        )

    with core._db() as conn:
        conn.execute(
            """
            UPDATE store_orders
            SET installation_email_status = 'sending',
                installation_email_attempted_at = ?,
                installation_email_error = NULL
            WHERE stripe_session_id = ?
            """,
            (now_epoch, session_id),
        )

    try:
        await asyncio.to_thread(_deliver_purchase_installation_email, order)
    except Exception as err:
        safe_error = _purchase_email_error_detail(err)
        with core._db() as conn:
            conn.execute(
                """
                UPDATE store_orders
                SET installation_email_status = 'failed',
                    installation_email_error = ?
                WHERE stripe_session_id = ?
                """,
                (str(safe_error)[:240], session_id),
            )
        _LOGGER.warning(
            "Purchase installation email resend failed for %s: %s",
            session_id,
            safe_error,
        )
        raise HTTPException(status_code=424, detail=safe_error) from err

    with core._db() as conn:
        conn.execute(
            """
            UPDATE store_orders
            SET installation_email_status = 'sent',
                installation_email_sent_at = ?,
                installation_email_error = NULL
            WHERE stripe_session_id = ?
            """,
            (core._iso(), session_id),
        )
    return {"sent": True}


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

    if str(order.get("payment_status") or "").casefold() == "paid":
        await asyncio.to_thread(
            _send_purchase_installation_email_once,
            order,
        )
        refreshed = _order_by_session(session_id)
        if refreshed is not None:
            order = refreshed

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


def _voice_preview_bytes(pack: dict[str, Any], sample: int) -> bytes:
    if sample < 1 or sample > len(_VOICE_PREVIEW_FILES):
        raise HTTPException(status_code=404, detail="voice preview not found")

    filename = str(pack.get("filename", "")).strip()
    if (
        not filename
        or Path(filename).name != filename
        or not core._VOICE_PACK_SAFE_PART.fullmatch(filename)
    ):
        raise HTTPException(status_code=404, detail="voice preview not found")

    path = core._voice_pack_dir() / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="voice preview not found")

    wanted = _VOICE_PREVIEW_FILES[sample - 1].casefold()
    try:
        with tarfile.open(path, mode="r:*") as archive:
            member = next(
                (
                    entry
                    for entry in archive.getmembers()
                    if entry.isfile()
                    and Path(entry.name).name.casefold() == wanted
                ),
                None,
            )
            if (
                member is None
                or member.size <= 0
                or member.size > _VOICE_PREVIEW_MAX_BYTES
            ):
                raise HTTPException(
                    status_code=404,
                    detail="voice preview not found",
                )
            source = archive.extractfile(member)
            if source is None:
                raise HTTPException(
                    status_code=404,
                    detail="voice preview not found",
                )
            data = source.read(_VOICE_PREVIEW_MAX_BYTES + 1)
    except HTTPException:
        raise
    except (tarfile.TarError, OSError):
        raise HTTPException(status_code=404, detail="voice preview not found")

    if not data or len(data) > _VOICE_PREVIEW_MAX_BYTES:
        raise HTTPException(status_code=404, detail="voice preview not found")
    return data


@router.get("/api/anthbot/store/voice-packs/{pack_id}/preview/{sample}")
def voice_pack_preview(pack_id: str, sample: int) -> Response:
    """Expose two fixed, short audio samples without exposing the paid pack."""
    pack = _find_uploaded_pack(pack_id)
    if _is_store_hidden(pack):
        raise HTTPException(status_code=404, detail="voice preview not found")
    return Response(
        content=_voice_preview_bytes(pack, sample),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )


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


@router.get("/api/anthbot/store/voice-packs/{pack_id}/owner-download")
def download_owner_voice_pack(
    pack_id: str,
    owner: str,
) -> FileResponse:
    """Serve a paid voice to a Map client explicitly granted owner access."""
    pack = _find_uploaded_pack(pack_id)
    _owner_access_client_for_pack(owner, pack)
    if not _is_paid(pack):
        raise HTTPException(status_code=404, detail="paid voice pack not found")

    filename = str(pack.get("filename", "")).strip()
    if (
        not filename
        or Path(filename).name != filename
        or not core._VOICE_PACK_SAFE_PART.fullmatch(filename)
    ):
        raise HTTPException(status_code=404, detail="paid voice pack not found")
    path = core._voice_pack_dir() / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="paid voice pack not found")
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get(
    "/api/anthbot/admin/store/pairings",
    dependencies=[Depends(core.require_admin)],
)
def admin_store_pairings(limit: int = 50) -> dict[str, Any]:
    """List active ANTHBOT Map store pairings, newest client first."""
    _init_store_tables()
    limit = max(1, min(int(limit), 200))
    now_epoch = int(time.time())
    with core._db() as conn:
        conn.execute(
            "DELETE FROM store_client_pairings WHERE expires_at < ?",
            (now_epoch,),
        )
        rows = conn.execute(
            """
            SELECT pair_code, client_id, created_at, expires_at
            FROM store_client_pairings
            WHERE expires_at >= ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (now_epoch, max(limit * 4, limit)),
        ).fetchall()
        owner_rows = conn.execute(
            "SELECT client_id FROM store_owner_clients"
        ).fetchall()

    owners = {str(row["client_id"]) for row in owner_rows}
    items: list[dict[str, Any]] = []
    seen_clients: set[str] = set()
    for row in rows:
        client_id = str(row["client_id"])
        if client_id in seen_clients:
            continue
        seen_clients.add(client_id)
        items.append(
            {
                "pair_code": str(row["pair_code"]),
                "client_suffix": client_id[-10:],
                "created_at": row["created_at"],
                "expires_at": _iso_from_epoch(int(row["expires_at"])),
                "owner_access": client_id in owners,
            }
        )
        if len(items) >= limit:
            break
    return {"count": len(items), "items": items}


@router.post(
    "/api/anthbot/admin/store/owner-access",
    dependencies=[Depends(core.require_admin)],
)
def grant_owner_store_access(payload: OwnerPairPayload) -> dict[str, Any]:
    """Grant all paid voice packs to one Map install via its temporary pair code."""
    client_id = _client_id_from_pairing(payload.pair_code)
    if client_id is None:
        raise HTTPException(status_code=404, detail="voice store pairing not found")
    _grant_owner_client(client_id)
    return {
        "granted": True,
        "owner_access": True,
        "client_suffix": client_id[-10:],
    }


@router.delete(
    "/api/anthbot/admin/store/owner-access",
    dependencies=[Depends(core.require_admin)],
)
def revoke_owner_store_access(payload: OwnerPairPayload) -> dict[str, Any]:
    """Revoke maintainer-owner voice access from one Map install."""
    client_id = _client_id_from_pairing(payload.pair_code)
    if client_id is None:
        raise HTTPException(status_code=404, detail="voice store pairing not found")
    revoked = _revoke_owner_client(client_id)
    return {
        "revoked": revoked,
        "owner_access": False,
        "client_suffix": client_id[-10:],
    }


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


@router.delete(
    "/api/anthbot/admin/store/orders/{session_id}",
    dependencies=[Depends(core.require_admin)],
)
def admin_delete_test_order(
    session_id: str,
    confirm_live: bool = False,
) -> dict[str, Any]:
    """Delete one local Store order.

    Sandbox orders may be removed directly. Live orders require an explicit
    confirm_live flag so a future real purchase cannot be deleted accidentally.
    Removing an order also removes any local license / Map entitlement derived
    from it because entitlements are resolved from store_orders.
    """
    _init_store_tables()
    normalized = session_id.strip()
    is_test = normalized.startswith("cs_test_")
    is_live = normalized.startswith("cs_live_")
    if not is_test and not is_live:
        raise HTTPException(status_code=422, detail="invalid Stripe checkout session id")
    if is_live and not confirm_live:
        raise HTTPException(
            status_code=409,
            detail="live order deletion requires explicit confirmation",
        )
    with core._db() as conn:
        row = conn.execute(
            "SELECT stripe_session_id FROM store_orders WHERE stripe_session_id = ?",
            (normalized,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="store order not found")
        conn.execute(
            "DELETE FROM store_orders WHERE stripe_session_id = ?",
            (normalized,),
        )
    return {
        "deleted": True,
        "stripe_session_id": normalized,
        "entitlement_revoked": True,
    }


@router.delete(
    "/api/anthbot/admin/store/orders",
    dependencies=[Depends(core.require_admin)],
)
def admin_delete_all_test_orders(
    include_live: bool = False,
) -> dict[str, Any]:
    """Delete local test orders, optionally including live-mode test history."""
    _init_store_tables()
    with core._db() as conn:
        if include_live:
            cursor = conn.execute(
                "DELETE FROM store_orders "
                "WHERE stripe_session_id GLOB 'cs_test_*' "
                "OR stripe_session_id GLOB 'cs_live_*'"
            )
            scope = "all_local_stripe_orders"
        else:
            cursor = conn.execute(
                "DELETE FROM store_orders WHERE stripe_session_id GLOB 'cs_test_*'"
            )
            scope = "stripe_sandbox_test_orders"
        deleted = max(0, int(cursor.rowcount or 0))
    return {
        "deleted": deleted,
        "scope": scope,
    }


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


def _indexed_public_paths() -> tuple[str, ...]:
    paths: list[str] = []
    for page in _SEO_PAGES.values():
        if page.get("index"):
            paths.append(str(page["path"]))
    paths.extend(str(path) for path in _SEO_LANDING_PAGES)
    return tuple(dict.fromkeys(paths))


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
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/sitemap.xml")
def sitemap_xml() -> Response:
    entries = "".join(
        f"<url><loc>{_PUBLIC_SITE_BASE_URL}{path}</loc></url>"
        for path in _indexed_public_paths()
    )
    return Response(
        content=(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{entries}</urlset>"
        ),
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=3600"},
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
    pair_code = str(request.query_params.get("pair") or "").strip()
    owner_pairing = str(request.query_params.get("owner") or "").strip() == "1"
    # A normal ANTHBOT Map pairing must always stay on the public Voice Store,
    # even when this browser also has an active Reporting Server admin session.
    # Maintainer-owner pairing remains available only through the explicit
    # ?owner=1 opt-in so the admin cookie cannot hijack customer checkout.
    if pair_code and owner_pairing and core._request_is_admin(request):
        try:
            _client_id_from_pairing(pair_code)
        except HTTPException:
            pass
        else:
            return RedirectResponse(
                url=f"/dashboard/store?owner_pair={quote(pair_code)}",
                status_code=303,
            )

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
