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
