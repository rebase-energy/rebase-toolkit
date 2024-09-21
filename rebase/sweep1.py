# rebase.py
import modal

image = modal.Image.debian_slim().pip_install("requests", "pandas", "dill", "joblib", "PyYAML", "mlflow", "dvc")
app = modal.App('rebase', image=image)

# Define the modal function at global scope
@app.function()
def modal_function(user_defined_function, param_dict):
    return user_defined_function(param_dict)

def sweep1(user_defined_function, params):

    params_iterable = [dict(zip(params.keys(), values)) for values in zip(*params.values())]
    #results = list(create_app.starmap([(function, param) for param in params_iterable]))    
    # Generate all combinations of parameters
    #keys = list(params.keys())
    #values = list(params.values())
    #combinations = [dict(zip(keys, v)) for v in itertools.product(*values)]
    
    # Prepare arguments for starmap
    #args = [(user_defined_function, param_dict) for param_dict in combinations]
    
    # Run the Modal function in parallel
    with app.run():
        results = modal_function.starmap(params_iterable)
        results = list(results)
    return results
