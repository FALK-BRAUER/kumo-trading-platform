# api/

The typed API client. `schema.ts` is generated from the backend's OpenAPI — never edited by hand.

- **Goes in:** `client.ts` and query hooks
- **Stays out:** hand-written types that duplicate the schema
