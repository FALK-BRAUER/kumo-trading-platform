export function apiBase(fromEnv: string | undefined): string {
  return fromEnv ?? "http://127.0.0.1:8000";
}
