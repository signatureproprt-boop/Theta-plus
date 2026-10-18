# Token administration 2.3.3

Permanent DHAN_CLIENT_ID remains in existing server configuration. Public research API access remains unchanged. Credential changes now require a Google Identity Services ID token validated server-side for signature, issuer, expiry and exact web-client audience, plus verified authoritative email on an explicit admin allowlist. Gmail or hosted Workspace identity is required; consumer third-party-address Google accounts are rejected. Token stays in tab memory; no admin cookie or local storage. Google sign-out clears that tab credential. An expired assertion requires sign-in again.

Dhan candidate is checked using GET https://api.dhan.co/v2/profile before writing Secret Manager. A profile must match the fixed Client ID. Invalid credentials, mismatched ID, provider errors or unavailable checks never overwrite the existing token. Raw token/profile never returned. Provider-reported tokenValidity/dataPlan/dataValidity are returned as status; expiry timezone is not invented and market-data entitlement is not claimed from authentication alone. Connection checks and saving are separate from quote freshness.

## One-time setup

In Google Auth Platform for theta-pluse, create or reuse a Web application OAuth client. Add Authorized JavaScript origin https://theta-plus-jw4jpkebxa-el.a.run.app (no path or trailing slash). Configure branding/audience/test users as Google requires; add the chosen admin as a test user if the app is in Testing. Default identity scopes only. Client ID is public; no OAuth client secret is required or used by GIS. Never paste Dhan tokens into chat or git.

From the downloaded repository source:

```bash
bash live-app/deploy/configure-token-admin.sh
bash live-app/deploy/release-existing.sh
```

Setup prompts for the Google Web Client ID and authorised admin email. It resolves the existing Dhan token secret, grants only accessor and version-adder on that secret to the existing runtime service account, and adds the three token-admin environment variables. It does not alter the fixed Dhan Client ID or read/print the token. An existing token secret and Client ID must already be configured. These IAM grants are needed to save the daily token from the application.

After DEPLOY VERIFIED: Data connections → Google admin sign-in → paste Dhan token in password input → optionally Show/Hide → Save & check. The server enforces allowlist when saving; receiving a Google credential in the browser alone does not grant access. Token write is denied if setup is incomplete. Anonymous token writes are smoke tested in release script. Trading screens do not require Google sign-in.

Validation: 28 backend tests pass; JS syntax checks pass for app.js/token-admin.js; both shell scripts pass bash -n. Actual GIS popup/origin, Secret Manager write and real Dhan profile success require configured production credentials and remain unverified. No broker execution changes.

References: https://developers.google.com/identity/gsi/web/guides/verify-google-id-token ; https://developers.google.com/identity/gsi/web/guides/get-google-api-clientid ; https://dhanhq.co/docs/v2/authentication/
