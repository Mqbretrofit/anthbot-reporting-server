# Custom voice requests (Reporting Server 1.0.53)

The public /store form already creates entries in store_custom_voice_requests.
The original intake route and data are unchanged. Existing rows are migrated
in place the first time the admin workflow is opened; no deletion or reset.

## Admin workflow

Go to Dashboard → Hangbolt → Egyedi hang kérések → Kezelés.

* Open and edit request status, internal notes, a EUR price quote (cents in DB),
  a linked existing Voice Builder job ID, and a linked uploaded pack ID.
* Open the existing Voice Builder separately to choose a supported locale, TTS
  provider, actual voice, text style and preview. These choices are intentionally
  not inferred from free-form customer text and generation is **never** started
  by browsing or opening a custom request.
* Prepare an editable Czech or English reply. Sending requires explicit browser
  confirmation and configured SMTP settings. Only successful sends are logged
  in a per-request message history. No auto-sends or automatic charges.
* Status progression is an admin workflow marker, not a payment or a license.
  A request can be marked completed only after referencing an uploaded pack.
* The payment indicator is read from an actual store_orders paid session
  matching **both** the linked pack or voice ID and customer email.
  Admin notes, quotes and status updates cannot set payment or entitlement.
  Stripe refunds remove the verified payment indicator.

The existing public Store payment and licensing flow is **unchanged**:
checkout can only sell published non-hidden paid packs to authenticated users.
A custom request quote does not automatically generate a Stripe Checkout link;
the admin must finish a pack, configure its Store pricing/visibility and direct
the customer through the existing authenticated Store checkout. Do not describe
an admin workflow status as proof of settlement.

## Security and persistence

All endpoints are under /api/anthbot/admin/store/custom-voice-workflow;
they require the existing admin cookie or Bearer token, and writes additionally
require X-ANTHBOT-Builder: 1 plus same-origin checking. SMTP credentials are
never returned, and outbound messages are sent only to the original request
contact after server-side email-address validation. Failed sends are not logged
as successful. The original incoming records, public requests, Stripe orders,
voice packs, entitlements and worker jobs are not modified by this feature.

Regression tests: tests/test_custom_voice_admin.py.

## 1.0.56 – OpenAI HTTP 400, strukturált JSON válasz

Az egyedihang-kérés AI-válasza most a Responses API dokumentált `text.format: json_schema` formátumát használja a korábbi `json_object` mód helyett. A cseh ügyfél levelét és a magyar fordítását kötelező, ellenőrzött mezőkkel adja vissza; a módosított levél `translate` módja csak a három szükséges magyar mezőt kéri. Ha az OpenAI mégis hibás paraméter miatt utasítja el a kérést, a biztonságosan szűrt API-paraméter neve megjelenik a hibában, az API-kulcs és a szolgáltató nyers válasza nem. A kész hangok, levelek, Stripe és Companion értesítések változatlanok maradnak. A korábban sikertelen AI-feladatot az „Új AI választervezet” gombbal lehet újból indítani.

## 1.0.55 – AI fordítás Home Assistant 502 hibájának kezelése

Az AI-fordítás külön háttérfeladatot indít, így az oldalra ágyazott Home Assistant HTTP-kérése nem vár perceket egy külső OpenAI-válaszra. Az adminoldal rövid, külön API-hívásokkal figyeli a folyamatot, és valódi szolgáltatói hibánál kiírja a konkrét hibát. Korábbi eredmények titkosítottan megmaradnak; azonos tartalomnál nem indul új fizetős hívás. Sikertelen kérés nem ismétlődik automatikusan: az újrapróbálkozáshoz külön gombnyomás kell. A már működő Voice Builder, HA mobilértesítések, hangcsomagok és Stripe-folyamat nem módosul.

## 1.0.54 – magyar AI-fordítás és adminértesítések

A Hangbolt admin Egyedi hangkérések Kezelés ablakában a Voice Builderben
már megadott OpenAI API-kulccsal készülhet az ügyfél nyelvén emailtervezet
és a hozzá tartozó magyar jelentés. Az ügyfél eredeti igénye is lefordítható.
A fordítás az aktuális szöveghez kötött; manuális levélmódosításkor figyelmeztet,
és külön „Magyar fordítás frissítése AI-val” gombot biztosít. Ugyanarra a
szövegre titkosított cache-ből válaszol, ismételt AI-költség nélkül. Küldés
nem automatikus; a felhasználó minden levelet külön megerősít.

A Hangbolt admin Értesítések részében beállítható egy email cím, és
kiválasztható a Home Assistant Companionban regisztrált egy vagy több
notify.mobile_app_* céleszköz. A szerver a HAOS Supervisor belső
http://supervisor/core/api/ proxyját használja, amelyhez a
config.yaml homeassistant_api: true és SUPERVISOR_TOKEN szükséges.
Nem kell új hosszú életű HA token, böngészős push-előfizetés vagy
külső értesítési szolgáltatás.

Az email és a HA telefonos push egymástól függetlenül engedélyezhető.
Események: új egyedi hangkérés, sikeres Stripe hangvásárlás,
Stripe visszatérítés, GDPR kérelem. Van adminértesítési lista,
olvasottként jelölés és tesztüzenet a kiválasztott telefonra.
A Stripe session és a kérelemazonosító alapján egy esemény csak
egyszer jön létre; a kézbesítés csatornánként és telefononként
nyilvántartott, hiba esetén legfeljebb öt próbálkozással.
A beállítás előtt keletkezett események nem okoznak utólag
tömeges értesítésküldést.

Beüzemelés: frissítsd az ANTHBOT Reporting Server HA alkalmazást,
nyisd meg a Hangbolt admint, válaszd ki a notify.mobile_app_*
telefon(oka)t és mentsd el. A Tesztértesítés gombbal próbáld ki,
hogy megjön-e ugyanúgy, mint a hazaérkezési értesítés.
A tényleges kézbesítés teszteléséhez működő Home Assistant Core
és Companion telefon szükséges.


## 1.0.57 – kétirányú levelezés és automatikus magyar fordítás

A Hangbolt admin Egyedi hang kérések / Kezelés ablakában az ügyfélnek
küldött email és az ügyféltől beérkezett email ugyanabban az időrendi
beszélgetésben jelenik meg. A beérkező levelet az OpenAI automatikusan
magyarra fordítja, és a fordítás a levél alatt látszik. Ha a fordítás
nem elérhető, az eredeti levél megmarad, és külön újrapróbálható.
Ha új válasz érkezik, a Home Assistant Companion beállított telefonjai
„Új válasz egyedi hangkérésre” értesítést kaphatnak. A külföldi
ügyfélnek soha nem küldünk automatikusan választ.

### IMAP bekapcsolása

Home Assistant → Beállítások → Alkalmazások → ANTHBOT Reporting Server →
Konfiguráció:

- imap_enabled: true
- imap_host: a válaszokat fogadó email-postafiók IMAP-kiszolgálója,
  például Google-fióknál imap.gmail.com
- imap_port: 993 (kötelező TLS)
- imap_username: a válaszokat fogadó teljes email-cím
- imap_password: az email-szolgáltató IMAP-alkalmazásjelszava
  (titkos HA App mező)
- imap_mailbox: INBOX

Mentsd a HA App konfigurációt, majd indítsd újra a Reporting Servert.
A Hangbolt admin/egyedi hangkérés Kezelés ablakában
„Új ügyfélválaszok ellenőrzése (IMAP)” kézi szinkront indít.
A szerver 90 másodpercenként önállóan is ellenőrzi a beérkező leveleket.
Az IMAP TLS-sel csatlakozik, csak olvassa az INBOX-ot, az üzeneteket
nem jelöli olvasottnak, nem törli és nem mozgatja.

### Biztonság, adatkezelés, téves párosítás elkerülése

Az újabb kimenő levelek Message-ID és ANTHBOT:cvr_ kezdetű
tárgyazonosítót kapnak. A beérkező válaszok RFC referenciafejléce
vagy tárgya alapján hozzárendelhetők a megfelelő kérelemhez.
Csak akkor fogadunk el választ, ha a feladó email-címe pontosan
egyezik az eredeti kérés kapcsolatcímével. Régi, még azonosító
nélkül elküldött üzeneteknél csak egyértelmű ügyfél-visszaválasz
párosítható; kétes esetben inkább nem importálunk tévesen.

Az első bekapcsoláskor a közelmúlt levelei között keres, ezért
az első korábban elküldött cseh levélre érkező válaszokat is
megkísérli hozzárendelni. A beérkező levelek és fordítások a
Reporting Server meglévő /data adatbázisába kerülnek; az AI
fordításhoz a levél tartalma továbbításra kerül az OpenAI-nak
a Voice Builderben mentett API-kulccsal. Ez OpenAI API-költséggel
járhat. Az admin felület nem teszi elérhetővé az IMAP jelszavát.

Élő IMAP-jelszó és tényleges ügyfél-válasz nélkül a külső
kézbesítés nem ellenőrizhető automatikus tesztekből.

Az engedélyezett hangminta-mellékletek (.mp3, .wav, .m4a, .ogg, .opus,
.flac, .aac), képek (.jpg, .png, .webp) és PDF-ek adminjogosultsággal
letölthetők az érkező üzenet alatt. Egy üzenet legfeljebb 15 MiB,
egy fájl legfeljebb 8 MiB lehet, legfeljebb nyolc melléklet kerül
mentésre. A támogatott méretnél nagyobb levelet az eredeti
postafiókban kell megnyitni, mivel az ANTHBOT nem tölti le.
