from supabase import create_client, Client

from .config import Settings

settings = Settings()

_service_key = settings.SUPABASE_SERVICE_ROLE_KEY.strip().strip('"').strip("'")
_anon_key = settings.SUPABASE_KEY.strip().strip('"').strip("'")

supabase: Client = create_client(settings.SUPABASE_URL, _service_key)
supabase_auth_client: Client = create_client(settings.SUPABASE_URL, _anon_key)