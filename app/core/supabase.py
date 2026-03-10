from supabase import create_client, Client

from .config import Settings

settings = Settings()

# Service role client: bypasses RLS for all DB operations
supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

# Anon client: used for auth operations (sign_up, sign_in, etc.)
supabase_auth_client: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)