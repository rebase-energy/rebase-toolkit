# rebase.py
import modal
import cloudpickle

app = modal.App()
image = modal.Image.debian_slim().pip_install("cloudpickle==3.0.0", "requests", "pandas", "dill", "joblib", "PyYAML", "mlflow", "dvc")

# Define the Modal function
@app.function(image=image)
def modal_function(params, func_serialized):
    import cloudpickle
    user_function = cloudpickle.loads(func_serialized)
    return user_function(params)

def sweep_2(user_function, params):
    # Prepare the list of parameter dictionaries
    param_list = [{"a": a, "b": b} for a, b in zip(params["a"], params["b"])]

    # Serialize the user-defined function
    func_serialized = cloudpickle.dumps(user_function)
    
    with app.run():
        # Map the function over the parameter list
        results = list(
            modal_function.map(
                param_list,
                [func_serialized] * len(param_list)
            )
        )
    
    print(results)