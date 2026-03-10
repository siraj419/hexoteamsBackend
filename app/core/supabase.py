from supabase import create_client, Client
import logging

from .config import Settings

logger = logging.getLogger(__name__)
settings = Settings()

# Log key prefixes for debugging (first 10 chars only)
logger.info(f"SUPABASE_KEY starts with: {settings.SUPABASE_KEY[:10]}...")
logger.info(f"SUPABASE_SERVICE_ROLE_KEY starts with: {settings.SUPABASE_SERVICE_ROLE_KEY[:10]}...")

# Service role client: bypasses RLS for all DB operations
supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

# Anon client: used for auth operations (sign_up, sign_in, etc.)
supabase_auth_client: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)