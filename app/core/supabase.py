from supabase import create_client, Client

from .config import Settings

settings = Settings()

service_key = settings.SUPABASE_SERVICE_ROLE_KEY
anon_key = settings.SUPABASE_KEY

print("anon_key: ", anon_key)
print("service_key: ", service_key)

supabase: Client = create_client(settings.SUPABASE_URL, _service_key)
supabase_auth_client: Client = create_client(settings.SUPABASE_URL, _anon_key)