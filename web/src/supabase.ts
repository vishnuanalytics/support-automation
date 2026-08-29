import { createClient } from "@supabase/supabase-js";

const url = import.meta.env.VITE_SUPABASE_URL as string;
const anon = import.meta.env.VITE_SUPABASE_ANON_KEY as string;

if (!url || !anon) {
  // eslint-disable-next-line no-console
  console.warn("VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY not set — see web/.env.example");
}

export const supabase = createClient(url ?? "", anon ?? "", {
  auth: { persistSession: true, autoRefreshToken: true },
});
