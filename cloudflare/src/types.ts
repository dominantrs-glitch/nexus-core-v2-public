export interface Env {
  DB: D1Database;
  BLOBS: R2Bucket;
  PROBE_ENABLED: string;
  AUTH_ISSUER: string;
  MCP_RESOURCE: string;
  PROJECT_ID: string;
}
export interface Identity {
  issuer: string;
  subject: string;
  clientId: string;
}
export class ProbeError extends Error {
  constructor(public code: "not_accessible" | "insufficient_context" | "budget_exhausted") {
    super(code);
  }
}
export const MAX_ORIGINAL_BYTES = 4 * 1024 * 1024;
export const DAILY_REQUESTS = 200;
export const DAILY_RESPONSE_BYTES = 16 * 1024 * 1024;
