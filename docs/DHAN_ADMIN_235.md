# Admin → Dhan connection (2.3.5)

Adds a dedicated Admin navigation page. Trading screens remain public; Admin credential read/test/write requires the separate token-admin password. Client ID and daily hidden token can be entered in the page. Test connection verifies authentication and reported subscription status without saving. Save & check validates the candidate against the entered Client ID before saving. Raw token is never returned. Provider-reported expiry is displayed without invented countdown; authentication is not a claim of verified live market data.

Client ID and token are saved in one version of theta-dhan-connection in Secret Manager, preventing a half-written pair. Client ID remains saved between daily token updates. Other workers reload within 20 seconds. Legacy env/individual secrets remain as fallback until THETA_DHAN_CREDENTIALS_SECRET is configured; legacy token update preserves the paired Client ID when pair storage is active.

One-time configure-token-admin.sh: prompt for an admin password with retries; preserve existing Dhan Client ID; seed an atomic secret pair from existing credentials only if the pair secret does not exist; grant runtime accessor/version-adder only on the necessary credential secrets; attach admin hash and pair secret reference. No Dhan value is printed. The inherited token may be invalid: replace it in Admin after deployment. Google OAuth and Google Client IDs are not used.

Then run release-existing.sh from live-app; deploys 2.3.5 with exact revision health verification, public status checks, anonymous token-write denial and rollback on failure. Existing data are retained and broker execution remains disabled.

Validation: 36 tests pass, including no-auth denial on admin settings/test/save, no saved-token reflection, test without save, check-before-save, atomic pair cross-worker reads and failed write preservation, plus previous replay/freshness/rollout checks. JS and shell syntax pass. Production deployment, tablet UI interaction and actual credential save/test are pending; no live success is claimed.
