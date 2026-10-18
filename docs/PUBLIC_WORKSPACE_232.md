# Public workspace release 2.3.2

The owner explicitly requested removal of workspace-key login. THETA_AUTH_MODE=public bypasses workspace bearer-key checks for research APIs. Anyone who can reach the URL can read stored research/journals and invoke imports, journal writes, experiments and data fetches; this is not private or read-only access. Cross-origin browser checks, allowed hosts, validation and size limits remain, but are not authentication or abuse prevention.

Dhan token rotation is rejected in public mode, and the web token form is hidden; update the token through existing Google Cloud Secret Manager administration. No broker execution is added. Existing secrets are neither returned nor removed. Default mode remains key protected.

The existing-service release script explicitly enables public mode and checks status is accessible without a key and token_screen_enabled is false. Exact revision health verification and rollback remain. Validation: 16 tests pass; JavaScript and shell syntax pass. Production remains pending user Cloud Shell rollout.
