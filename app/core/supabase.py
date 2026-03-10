from supabase import create_client, Client

from .config import Settings

settings = Settings()

supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
supabase_auth_client: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)