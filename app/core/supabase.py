from supabase import create_client, Client

from .config import Settings

settings = Settings()

print(f"[DEBUG] SUPABASE_URL: {settings.SUPABASE_URL}")
print(f"[DEBUG] SUPABASE_KEY starts with: {settings.SUPABASE_KEY[:20]}...")
print(f"[DEBUG] SUPABASE_SERVICE_ROLE_KEY starts with: {settings.SUPABASE_SERVICE_ROLE_KEY[:20]}...")
print(f"[DEBUG] SUPABASE_KEY length: {len(settings.SUPABASE_KEY)}")
print(f"[DEBUG] SUPABASE_SERVICE_ROLE_KEY length: {len(settings.SUPABASE_SERVICE_ROLE_KEY)}")

# Service role client: bypasses RLS for all DB operations
supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

# Anon client: used for auth operations (sign_up, sign_in, etc.)
supabase_auth_client: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)