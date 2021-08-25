from mlflow import log_params as mlflow__log_params
from mlflow import *
import os
import hashlib

def maybe_shrink_param_repr(p, max_size=None):
	str_repr = str(p)
	if max_size is not None and len(str_repr) >= max_size:
		p_hash = hashlib.md5(str_repr.encode('utf-8')).hexdigest()
		return f"md5:{p_hash}"
	return p

def log_params(params):	
	corrected_params = { k: maybe_shrink_param_repr(p, max_size=250) for k, p in params.items()}
	mlflow__log_params(corrected_params)

