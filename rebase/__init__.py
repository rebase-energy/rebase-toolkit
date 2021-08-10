import os

api_key = os.environ.get('RB_API_KEY')
base_api_url = os.environ.get('RB_BASE_API_URL', 'https://api.rebase.energy/')
#base_api_url = 'https://dev-api.rebase.energy/'
cache_dir = './cache'

try:
	os.makedirs(cache_dir, exist_ok=True)
except Exception as e:
	print(f"Exception while initializing cache: {e}")

from rebase.api import *
from rebase.cli import *
