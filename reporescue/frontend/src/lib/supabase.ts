import { createClient, type SupabaseClient } from "@supabase/supabase-js";

const url = import.meta.env.VITE_SUPABASE_URL as string | undefined;
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined;

/** False when the developer has not configured Supabase env vars yet. */
export const isSupabaseConfigured = Boolean(url && anonKey);

let client: SupabaseClient | null = null;

/** Centralized Supabase client. Only the public anon key is ever used here. */
export function getSupabase(): SupabaseClient {
  if (!isSupabaseConfigured || !url || !anonKey) {
    throw new Error(
      "Supabase is not configured. Copy frontend/.env.example to .env.local " +
        "and fill in VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY."
    );
  }
  if (!client) {
    client = createClient(url, anonKey);
  }
  return client;
}
