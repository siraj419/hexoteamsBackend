from supabase import create_client, Client

from .config import Settings

settings = Settings()

service_key = settings.SUPABASE_SERVICE_ROLE_KEY
anon_key = settings.SUPABASE_KEY

print("ANON_KEY: ", anon_key)
print("SERVICE_KEY: ", service_key)

supabase: Client = create_client(settings.SUPABASE_URL, service_key)
supabase_auth_client: Client = create_client(settings.SUPABASE_URL, anon_key)