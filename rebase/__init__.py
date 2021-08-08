from os import makedirs

api_key = None
base_api_url = 'https://api.rebase.energy/'
#base_api_url = 'https://dev-api.rebase.energy/'
cache_dir = './cache'

try:
	makedirs(cache_dir, exist_ok=True)
except Exception as e:
	print(f"Exception while initializing cache: {e}")

from rebase.api import *
from rebase.cli import *
